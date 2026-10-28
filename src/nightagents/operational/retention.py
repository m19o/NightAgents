from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session

from nightagents.db.models import AuditEventRecord, ToolCallRecord


class RetentionPolicy:
    """Applies retention and archival policies for evidence and tool-call payloads."""

    def __init__(self, db: Session):
        self.db = db

    def archive_tool_call_payloads(self, older_than_days: int = 30) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        rows = self.db.execute(
            delete(ToolCallRecord)
            .where(ToolCallRecord.created_at < cutoff)
            .where(ToolCallRecord.status.in_(["completed", "failed"]))
        ).rowcount
        self.db.commit()
        return int(rows or 0)

    def archive_audit_events(self, older_than_days: int = 365) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        rows = self.db.execute(delete(AuditEventRecord).where(AuditEventRecord.created_at < cutoff)).rowcount
        self.db.commit()
        return int(rows or 0)
