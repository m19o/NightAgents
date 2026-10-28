from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from nightagents.db.base import Base
from nightagents.db.models import KnowledgeDocumentRecord, SourceReputationRecord
from nightagents.db.session import build_engine
from nightagents.knowledge.revalidation import SourceReputationService, StaleDocumentService


def test_source_reputation_tracks_success_failure() -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    service = SourceReputationService(db)
    for _ in range(9):
        service.record_outcome("src-1", "vendor", True)
    service.record_outcome("src-1", "vendor", False)
    record = db.get(SourceReputationRecord, "src-1")
    assert record.success_count == 9
    assert record.failure_count == 1
    assert record.current_trust_tier == "reviewed"
    assert len(record.trust_tier_history_json) >= 1


def test_stale_document_service_finds_old_documents() -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    old = KnowledgeDocumentRecord(
        source_id="src-old",
        source_uri="https://example.com/old",
        title="Old",
        publisher="vendor",
        content_hash="old",
        raw_text="old content",
        retrieved_at=datetime.now(UTC) - timedelta(days=45),
    )
    recent = KnowledgeDocumentRecord(
        source_id="src-recent",
        source_uri="https://example.com/recent",
        title="Recent",
        publisher="vendor",
        content_hash="recent",
        raw_text="recent content",
        retrieved_at=datetime.now(UTC) - timedelta(days=1),
    )
    db.add(old)
    db.add(recent)
    db.commit()
    service = StaleDocumentService(db, freshness_days=30)
    stale = service.stale_documents()
    assert len(stale) == 1
    assert stale[0].id == old.id


@pytest.mark.asyncio
async def test_stale_document_revalidation_updates_reputation() -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    document = KnowledgeDocumentRecord(
        source_id="src-reval",
        source_uri="https://example.com/reval",
        title="Revalidate",
        publisher="vendor",
        content_hash="reval",
        raw_text="content",
        retrieved_at=datetime.now(UTC) - timedelta(days=40),
    )
    db.add(document)
    db.commit()
    service = StaleDocumentService(db, freshness_days=30)
    result = await service.revalidate(document.id)
    assert result["document_id"] == document.id
    assert db.get(SourceReputationRecord, "src-reval") is not None
