from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from nightagents.db.base import Base
from nightagents.db.models import (
    EngagementRecord,
    HandoffRecord,
    IncidentRecord,
    OperatorReviewTaskRecord,
    PredictionOutcomeRecord,
    PredictionRecord,
)
from nightagents.db.session import build_engine
from nightagents.predictions import PredictionHorizonService


class StubMCPGateway:
    def __init__(self, source_health=None, confirmation=None):
        self.source_health = source_health or {}
        self.confirmation = confirmation or {}

    async def call_tool(self, server: str, tool: str, arguments: dict):
        if server == "elastic" and tool == "get_source_health":
            return self.source_health
        if server == "elastic" and tool == "run_catalog_query":
            return self.confirmation
        return {}


def _setup():
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    engagement = EngagementRecord(name="horizon-engagement")
    db.add(engagement)
    db.flush()
    incident = IncidentRecord(
        engagement_id=engagement.id,
        external_id="horizon-alert",
        severity="high",
        rule_id="horizon-rule",
        occurred_at=datetime.now(UTC),
    )
    db.add(incident)
    db.flush()
    handoff = HandoffRecord(
        engagement_id=engagement.id,
        incident_id=incident.id,
        payload_json={"prediction": {"event": "profiling", "probability": 0.7}, "evidence": [{"source": "logs-edge"}]},
        payload_hash="a" * 64,
        signature="sig",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    db.add(handoff)
    db.flush()
    return db, engagement, handoff


@pytest.mark.asyncio
async def test_horizon_supported_outcome():
    db, engagement, handoff = _setup()
    prediction = PredictionRecord(
        handoff_id=handoff.id,
        engagement_id=engagement.id,
        predicted_event="profiling",
        predicted_probability=0.7,
        horizon_seconds=3600,
        confirmation_query_id="blue-chain-profiling-v1",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    db.add(prediction)
    db.commit()
    gateway = StubMCPGateway(
        source_health={"logs-edge": {"status": "healthy"}},
        confirmation={"events": [{"_id": "e1"}]},
    )
    result = await PredictionHorizonService(db, gateway).verify_at_expiry(prediction.id)
    assert result["status"] == "outcome_recorded"
    assert "outcome_id" in result
    db.refresh(prediction)
    assert prediction.status == "outcome_recorded"
    outcome = db.get(PredictionOutcomeRecord, result["outcome_id"])
    assert outcome is not None
    assert outcome.matched is True
    assert outcome.label_source == "horizon_auto"


@pytest.mark.asyncio
async def test_horizon_missing_evidence_creates_review():
    db, engagement, handoff = _setup()
    prediction = PredictionRecord(
        handoff_id=handoff.id,
        engagement_id=engagement.id,
        predicted_event="profiling",
        predicted_probability=0.7,
        horizon_seconds=3600,
        confirmation_query_id="blue-chain-profiling-v1",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    db.add(prediction)
    db.commit()
    gateway = StubMCPGateway(
        source_health={"logs-edge": {"status": "healthy"}},
        confirmation={"events": []},
    )
    result = await PredictionHorizonService(db, gateway).verify_at_expiry(prediction.id)
    assert result["status"] == "operator_review"
    db.refresh(prediction)
    assert prediction.status == "operator_review"
    review = db.get(OperatorReviewTaskRecord, result["review_task_id"])
    assert review is not None
    assert review.target_type == "prediction"


@pytest.mark.asyncio
async def test_horizon_unhealthy_source_creates_review():
    db, engagement, handoff = _setup()
    prediction = PredictionRecord(
        handoff_id=handoff.id,
        engagement_id=engagement.id,
        predicted_event="profiling",
        predicted_probability=0.7,
        horizon_seconds=3600,
        confirmation_query_id="blue-chain-profiling-v1",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    db.add(prediction)
    db.commit()
    gateway = StubMCPGateway(
        source_health={"logs-edge": {"status": "unhealthy"}},
        confirmation={"events": [{"_id": "e1"}]},
    )
    result = await PredictionHorizonService(db, gateway).verify_at_expiry(prediction.id)
    assert result["status"] == "operator_review"
    assert "unhealthy sources" in result["reason"]


@pytest.mark.asyncio
async def test_horizon_duplicate_closure_prevented():
    db, engagement, handoff = _setup()
    prediction = PredictionRecord(
        handoff_id=handoff.id,
        engagement_id=engagement.id,
        predicted_event="profiling",
        predicted_probability=0.7,
        horizon_seconds=3600,
        confirmation_query_id="blue-chain-profiling-v1",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        status="outcome_recorded",
    )
    db.add(prediction)
    db.commit()
    gateway = StubMCPGateway()
    with pytest.raises(ValueError, match="already outcome_recorded"):
        await PredictionHorizonService(db, gateway).verify_at_expiry(prediction.id)


@pytest.mark.asyncio
async def test_resolve_review_task():
    db, engagement, handoff = _setup()
    prediction = PredictionRecord(
        handoff_id=handoff.id,
        engagement_id=engagement.id,
        predicted_event="profiling",
        predicted_probability=0.7,
        horizon_seconds=3600,
        confirmation_query_id="blue-chain-profiling-v1",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        status="operator_review",
    )
    db.add(prediction)
    db.flush()
    review = OperatorReviewTaskRecord(
        engagement_id=engagement.id,
        target_type="prediction",
        target_id=prediction.id,
        reason="no evidence",
    )
    db.add(review)
    db.flush()
    prediction.review_task_id = review.id
    db.commit()
    service = PredictionHorizonService(db, StubMCPGateway())
    task = service.resolve_review_task(review.id, "operator-1", "reviewed and closed")
    assert task.status == "resolved"
    db.refresh(prediction)
    assert prediction.status == "review_resolved"
