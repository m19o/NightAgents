from sqlalchemy.orm import sessionmaker

from nightagents.db.base import Base
from nightagents.db.models import OutboxEventRecord
from nightagents.db.session import build_engine
from nightagents.workers.outbox import OutboxPublisher


async def test_outbox_delivery_is_published_and_not_repeated() -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    delivered = []

    async def handler(event: OutboxEventRecord) -> None:
        delivered.append(event.id)

    with factory() as db:
        event = OutboxEventRecord(
            aggregate_type="test",
            aggregate_id="aggregate",
            event_type="test.created",
            payload_json={"ok": True},
        )
        db.add(event)
        db.commit()
        publisher = OutboxPublisher(db, worker_id="worker-a")
        assert await publisher.publish_once(handler) == 1
        assert await publisher.publish_once(handler) == 0
        assert delivered == [event.id]


async def test_outbox_failure_releases_claim_for_retry() -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    async def failing(_event: OutboxEventRecord) -> None:
        raise RuntimeError("receiver unavailable")

    with factory() as db:
        event = OutboxEventRecord(
            aggregate_type="test",
            aggregate_id="aggregate",
            event_type="test.created",
            payload_json={},
        )
        db.add(event)
        db.commit()
        assert await OutboxPublisher(db, worker_id="worker-a").publish_once(failing) == 0
        db.refresh(event)
        assert event.claimed_at is None
        assert event.attempts == 1
