from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from nightagents.assets.reconcile import MythicInventoryReconciler
from nightagents.contracts.common import ObservationStatus
from nightagents.contracts.mythic import Callback, CallbackInventory
from nightagents.db.base import Base
from nightagents.db.models import AssetRecord, AssetRelationshipRecord, EngagementRecord
from nightagents.db.session import build_engine


def test_mythic_reconciliation_registers_callbacks_without_inventing_routes() -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        engagement = EngagementRecord(name="authorized-operation")
        db.add(engagement)
        db.commit()
        inventory = CallbackInventory(
            status=ObservationStatus.SUCCESS,
            observed_at=datetime.now(UTC),
            callbacks=[
                Callback(
                    callback_id="callback-1",
                    operation_id="authorized-operation",
                    payload_type="lab-agent",
                    c2_profile="http",
                    last_seen=datetime.now(UTC),
                    active=True,
                )
            ],
        )
        result = MythicInventoryReconciler(db).reconcile(engagement.id, inventory)
        callback = db.scalar(
            select(AssetRecord).where(
                AssetRecord.asset_type == "mythic_callback",
                AssetRecord.external_id == "callback-1",
            )
        )
        relationships = list(db.scalars(select(AssetRelationshipRecord)))
        assert result["callbacks"] == 1
        assert callback.attributes_json["route_attribution"] == "unverified"
        assert callback.attributes_json["observed_route_id"] is None
        assert [item.relationship for item in relationships] == ["uses_profile"]
