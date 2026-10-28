from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from nightagents.db.base import Base
from nightagents.db.models import AuditEventRecord, ToolCallRecord
from nightagents.db.session import build_engine
from nightagents.operational import OperationalMetrics, RetentionPolicy


def test_metrics_record_and_summarize() -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    metrics = OperationalMetrics(db)
    metrics.record("mcp.failure", 1.0, {"server": "elastic"})
    metrics.record("mcp.failure", 1.0, {"server": "elastic"})
    metrics.record("circuit.open", 1.0)
    summary = metrics.summary(["mcp.failure", "circuit.open"])
    assert summary["mcp.failure"]["count"] == 2
    assert summary["circuit.open"]["count"] == 1
    assert metrics.latest("mcp.failure", {"server": "elastic"}) == 1.0


def test_retention_archives_old_tool_calls() -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    old = ToolCallRecord(
        run_id="run-1",
        server="elastic",
        tool="echo",
        request_json={},
        response_json={},
        status="completed",
        created_at=datetime.now(UTC) - timedelta(days=31),
    )
    recent = ToolCallRecord(
        run_id="run-1",
        server="elastic",
        tool="echo",
        request_json={},
        response_json={},
        status="completed",
        created_at=datetime.now(UTC) - timedelta(days=1),
    )
    db.add(old)
    db.add(recent)
    db.commit()
    retention = RetentionPolicy(db)
    assert retention.archive_tool_call_payloads(older_than_days=30) == 1
    assert db.scalar(select(ToolCallRecord).where(ToolCallRecord.id == old.id)) is None
    assert db.scalar(select(ToolCallRecord).where(ToolCallRecord.id == recent.id)) is not None


def test_retention_archives_old_audit_events() -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    old = AuditEventRecord(
        actor="test",
        event_type="test.event",
        target_type="test",
        target_id="t1",
        event_hash="h" * 64,
        created_at=datetime.now(UTC) - timedelta(days=366),
    )
    db.add(old)
    db.commit()
    retention = RetentionPolicy(db)
    assert retention.archive_audit_events(older_than_days=365) == 1
    assert db.scalar(select(AuditEventRecord).where(AuditEventRecord.id == old.id)) is None
