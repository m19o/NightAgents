from datetime import UTC, datetime

from sqlalchemy.orm import Session

from nightagents.config import Settings
from nightagents.db.models import ApprovalRecord, HandoffRecord
from nightagents.db.repositories import AuditRepository, OutboxRepository
from nightagents.security.signing import sign_hash, verify_hash_signature


class ApprovalService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.audit = AuditRepository(db)
        self.outbox = OutboxRepository(db)

    def approve(self, handoff_id: str, operator_id: str) -> ApprovalRecord:
        handoff = self.db.get(HandoffRecord, handoff_id)
        if handoff is None:
            raise ValueError("handoff not found")
        if handoff.status != "proposed":
            raise ValueError(f"handoff is not awaiting decision: {handoff.status}")
        expires_at = handoff.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= datetime.now(UTC):
            raise ValueError("handoff expired")
        if not verify_hash_signature(
            self.settings.handoff_signing_secret,
            handoff.payload_hash,
            handoff.signature,
        ):
            raise ValueError("invalid handoff signature")
        approval = ApprovalRecord(
            handoff_id=handoff.id,
            operator_id=operator_id,
            decision="approved",
            handoff_hash=handoff.payload_hash,
            signature=sign_hash(self.settings.approval_signing_secret, handoff.payload_hash),
        )
        handoff.status = "approved"
        self.db.add(approval)
        self.db.flush()
        self.outbox.add("approval", approval.id, "handoff.approved", {"handoff_id": handoff.id})
        self.audit.append(
            operator_id,
            "handoff.approved",
            "handoff",
            handoff.id,
            {"handoff_hash": handoff.payload_hash},
            handoff.engagement_id,
        )
        self.db.commit()
        return approval

    def reject(self, handoff_id: str, operator_id: str) -> ApprovalRecord:
        handoff = self.db.get(HandoffRecord, handoff_id)
        if handoff is None:
            raise ValueError("handoff not found")
        if handoff.status != "proposed":
            raise ValueError(f"handoff is not awaiting decision: {handoff.status}")
        approval = ApprovalRecord(
            handoff_id=handoff.id,
            operator_id=operator_id,
            decision="rejected",
            handoff_hash=handoff.payload_hash,
            signature=sign_hash(self.settings.approval_signing_secret, handoff.payload_hash),
        )
        handoff.status = "rejected"
        self.db.add(approval)
        self.db.flush()
        self.outbox.add("approval", approval.id, "handoff.rejected", {"handoff_id": handoff.id})
        self.audit.append(
            operator_id,
            "handoff.rejected",
            "handoff",
            handoff.id,
            {"handoff_hash": handoff.payload_hash},
            handoff.engagement_id,
        )
        self.db.commit()
        return approval
