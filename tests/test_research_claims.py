from sqlalchemy.orm import sessionmaker

from nightagents.db.base import Base
from nightagents.db.models import (
    EngagementRecord,
    KnowledgeCandidateRecord,
    KnowledgeDocumentRecord,
)
from nightagents.db.session import build_engine
from nightagents.knowledge.claims import ClaimAnalysisService
from nightagents.knowledge.memory import MemoryService


def test_claim_extraction_labels_contradiction_and_support() -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    engagement = EngagementRecord(name="claim-engagement")
    db.add(engagement)
    db.flush()
    MemoryService(db).remember_episode(
        engagement.id,
        "203.0.113.5 is an active C2 endpoint",
        "internal-intel",
        memory_type="semantic",
    )
    document = KnowledgeDocumentRecord(
        source_id="src-1",
        source_uri="https://example.com/ioc",
        title="IOC update",
        publisher="vendor",
        content_hash="hash1",
        raw_text="203.0.113.5 is not an active C2 endpoint.",
    )
    db.add(document)
    db.flush()
    candidate = KnowledgeCandidateRecord(
        document_id=document.id,
        candidate_type="KNOWLEDGE_CLAIM",
        title="IOC update",
        content_json={"normalized_excerpt": "203.0.113.5 is not an active C2 endpoint."},
    )
    db.add(candidate)
    db.flush()
    service = ClaimAnalysisService(db)
    records = service.analyze(candidate.id, candidate.content_json["normalized_excerpt"], engagement.id)
    assert len(records) == 1
    assert records[0].relationship == "contradiction"
    assert records[0].semantic_diff != ""


def test_claim_without_overlap_is_unknown() -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    engagement = EngagementRecord(name="claim-engagement-2")
    db.add(engagement)
    db.flush()
    document = KnowledgeDocumentRecord(
        source_id="src-2",
        source_uri="https://example.com/tip",
        title="TIP",
        publisher="vendor",
        content_hash="hash2",
        raw_text="A totally unrelated defensive recommendation.",
    )
    db.add(document)
    db.flush()
    candidate = KnowledgeCandidateRecord(
        document_id=document.id,
        candidate_type="KNOWLEDGE_CLAIM",
        title="TIP",
        content_json={"normalized_excerpt": "A totally unrelated defensive recommendation."},
    )
    db.add(candidate)
    db.flush()
    MemoryService(db).remember_episode(
        engagement.id,
        "Threat actor uses DNS tunneling",
        "internal-intel",
        memory_type="semantic",
    )
    records = ClaimAnalysisService(db).analyze(
        candidate.id, candidate.content_json["normalized_excerpt"], engagement.id
    )
    assert all(record.relationship == "unknown" for record in records)


def test_claim_extraction_splits_sentences() -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    text = "First long claim. Second long claim follows! Third longer claim here?"
    claims = ClaimAnalysisService(db).extract_claims(text)
    assert len(claims) == 3
