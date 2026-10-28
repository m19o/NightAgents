from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from nightagents.db.base import Base
from nightagents.db.models import AuditEventRecord, EngagementRecord
from nightagents.db.repositories import AuditRepository, WorkflowRepository
from nightagents.db.session import build_engine


def test_durable_workflow_checkpoint_and_hash_chained_audit() -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        engagement = EngagementRecord(name="test")
        db.add(engagement)
        db.flush()
        workflow = WorkflowRepository(db)
        run = workflow.create_run(engagement.id, "nightwatcher", "investigate", {"tools": 20})
        workflow.checkpoint(run, "verify_evidence", {"incident_id": "inc-1"})
        workflow.add_step(run, "verify_evidence", "completed", {}, {"status": "confirmed"})
        audit = AuditRepository(db)
        first = audit.append("nightwatcher", "run.started", "run", run.id, {})
        second = audit.append("nightwatcher", "run.checkpoint", "run", run.id, {"node": "verify_evidence"})
        db.commit()

        restored = db.get(type(run), run.id)
        events = list(db.scalars(select(AuditEventRecord).order_by(AuditEventRecord.created_at)))
        assert restored.current_node == "verify_evidence"
        assert len(events) == 2
        assert second.previous_hash == first.event_hash
        assert second.event_hash != first.event_hash
        assert audit.verify_chain()["valid"] is True


def test_audit_verification_detects_tampering() -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        audit = AuditRepository(db)
        event = audit.append("operator", "test.event", "test", "target", {"value": "original"})
        db.commit()
        event.details_json = {"value": "tampered"}
        db.commit()
        report = audit.verify_chain()
        assert report["valid"] is False
        assert "hash mismatch" in report["reason"]
