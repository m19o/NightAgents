from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from nightagents.db.models import KnowledgeDocumentRecord, SourceReputationRecord
from nightagents.db.repositories import AuditRepository
from nightagents.knowledge.hub import KnowledgeHub


class SourceReputationService:
    """Tracks fetch success/failure and trust-tier history for knowledge sources."""

    def __init__(self, db: Session):
        self.db = db
        self.audit = AuditRepository(db)

    def record_outcome(
        self, source_id: str, publisher: str, success: bool, details: dict | None = None
    ) -> SourceReputationRecord:
        record = self.db.get(SourceReputationRecord, source_id)
        if record is None:
            record = SourceReputationRecord(source_id=source_id, publisher=publisher)
            self.db.add(record)
            self.db.flush()
        now = datetime.now(UTC)
        if success:
            record.success_count += 1
            record.last_success_at = now
        else:
            record.failure_count += 1
            record.last_failure_at = now
        tier = self._derive_tier(record.success_count, record.failure_count)
        if tier != record.current_trust_tier:
            record.trust_tier_history_json = [
                *record.trust_tier_history_json,
                {"from": record.current_trust_tier, "to": tier, "at": now.isoformat()},
            ]
            record.current_trust_tier = tier
        record.updated_at = now
        self.audit.append(
            "source-reputation",
            "source.reputation_updated",
            "knowledge_source",
            source_id,
            {"success": success, "tier": tier, **(details or {})},
        )
        self.db.commit()
        return record

    @staticmethod
    def _derive_tier(success: int, failure: int) -> str:
        total = success + failure
        if total == 0:
            return "unreviewed"
        ratio = success / total
        if ratio >= 0.95 and total >= 10:
            return "trusted"
        if ratio >= 0.8:
            return "reviewed"
        if ratio >= 0.5:
            return "unreviewed"
        return "low"


class StaleDocumentService:
    """Revalidates quarantined documents whose content is older than the freshness threshold."""

    def __init__(self, db: Session, freshness_days: int = 30):
        self.db = db
        self.freshness_days = freshness_days
        self.hub = KnowledgeHub(db)
        self.reputation = SourceReputationService(db)
        self.audit = AuditRepository(db)

    def stale_documents(self) -> list[KnowledgeDocumentRecord]:
        cutoff = datetime.now(UTC) - timedelta(days=self.freshness_days)
        return list(
            self.db.scalars(
                select(KnowledgeDocumentRecord).where(
                    KnowledgeDocumentRecord.retrieved_at < cutoff,
                    KnowledgeDocumentRecord.status.in_(["quarantined", "reviewed"]),
                )
            )
        )

    async def revalidate(self, document_id: str) -> dict:
        document = self.db.get(KnowledgeDocumentRecord, document_id)
        if document is None:
            raise ValueError("document not found")
        result = await self.hub.refresh(document.source_id)
        if result.document_id == document.id and result.changed:
            self.reputation.record_outcome(document.source_id, document.publisher, True)
            return {"document_id": document.id, "revalidated": True, "changed": True}
        if result.document_id != document.id:
            self.reputation.record_outcome(document.source_id, document.publisher, True)
            return {"document_id": document.id, "revalidated": True, "changed": False, "superseded": True}
        self.reputation.record_outcome(document.source_id, document.publisher, False)
        return {"document_id": document.id, "revalidated": False, "changed": False}
