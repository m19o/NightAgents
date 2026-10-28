from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from nightagents.adapters.database_infrastructure import DatabaseInfrastructure
from nightagents.adapters.in_memory_infrastructure import InMemoryInfrastructure
from nightagents.assets.lifecycle import RedirectorLifecycleService
from nightagents.db.base import Base
from nightagents.db.models import AssetRecord, AssetRelationshipRecord, EngagementRecord
from nightagents.db.session import build_engine


def test_redirector_registration_builds_asset_graph_and_failover() -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        engagement = EngagementRecord(name="eng")
        db.add(engagement)
        db.commit()
        lifecycle = RedirectorLifecycleService(db)
        first = lifecycle.register(engagement.id, "r1", "a.test", "route", "provider-a", "http", "fg")
        second = lifecycle.register(engagement.id, "r2", "b.test", "route-b", "provider-b", "http", "fg")
        first.state = "active"
        second.state = "active"
        db.commit()
        failover = lifecycle.healthy_failover(engagement.id, "r1")
        assert failover.external_id == "r2"
        assert len(list(db.scalars(select(AssetRelationshipRecord)))) == 10
        assert len(list(db.scalars(select(AssetRecord)))) == 10


async def test_infrastructure_switch_is_idempotent_and_rollbackable() -> None:
    infrastructure = InMemoryInfrastructure(
        routes={"logical": "old"},
        health={"old": True, "new": True},
    )
    plan = await infrastructure.plan_route_switch("eng", "logical", "old", "new")
    first = await infrastructure.execute_route_switch(plan, "switch-1")
    duplicate = await infrastructure.execute_route_switch(plan, "switch-1")
    rollback = await infrastructure.rollback_route_switch(plan, "rollback-1")
    assert first == duplicate
    assert first.current_state["target"] == "new"
    assert rollback.current_state["target"] == "old"


async def test_database_infrastructure_persists_switch_and_idempotency() -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        engagement = EngagementRecord(name="persistent-eng")
        db.add(engagement)
        db.commit()
        lifecycle = RedirectorLifecycleService(db)
        old = lifecycle.register(engagement.id, "old", "old.test", "edge", "p-old", "http", "fg")
        new = lifecycle.register(engagement.id, "new", "new.test", "standby", "p-new", "http", "fg")
        lifecycle.record_health(engagement.id, old.external_id, True, {"probe": "ok"})
        lifecycle.record_health(engagement.id, new.external_id, True, {"probe": "ok"})
        infrastructure = DatabaseInfrastructure(db)
        plan = await infrastructure.plan_route_switch(engagement.id, "edge", "old", "new")
        first = await infrastructure.execute_route_switch(plan, "persistent-switch")
        duplicate = await infrastructure.execute_route_switch(plan, "persistent-switch")
        assert first == duplicate
        assert first.current_state["target"] == "new"
        rollback = await infrastructure.rollback_route_switch(plan, "persistent-rollback")
        assert rollback.current_state["target"] == "old"
