from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from nightagents.db.models import OutboxEventRecord


class OutboxPublisher:
    def __init__(self, db: Session, worker_id: str | None = None, lease_seconds: int = 60):
        self.db = db
        self.worker_id = worker_id or str(uuid4())
        self.lease_seconds = lease_seconds

    async def publish_once(self, handler: Callable[[OutboxEventRecord], Awaitable[None]], limit: int = 20) -> int:
        now = datetime.now(UTC)
        lease_expired = now - timedelta(seconds=self.lease_seconds)
        query = (
            select(OutboxEventRecord)
            .where(
                OutboxEventRecord.published_at.is_(None),
                OutboxEventRecord.available_at <= now,
                (OutboxEventRecord.claimed_at.is_(None) | (OutboxEventRecord.claimed_at < lease_expired)),
            )
            .order_by(OutboxEventRecord.available_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        events = list(self.db.scalars(query))
        for event in events:
            event.claimed_at = now
            event.claimed_by = self.worker_id
        self.db.commit()
        published = 0
        for event in events:
            try:
                await handler(event)
                event.published_at = datetime.now(UTC)
                event.claimed_at = None
                event.claimed_by = None
                event.last_error = None
                published += 1
            except Exception as exc:
                event.attempts += 1
                event.last_error = str(exc)
                event.claimed_at = None
                event.claimed_by = None
        self.db.commit()
        return published
