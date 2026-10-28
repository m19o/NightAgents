from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from nightagents.contracts.feedback import PredictionOutcomeCreate
from nightagents.db.base import Base
from nightagents.db.models import (
    CalibrationBucketRecord,
    EngagementRecord,
    HandoffRecord,
    IncidentRecord,
    MemoryRecord,
)
from nightagents.db.session import build_engine
from nightagents.feedback import PredictionFeedbackService
from nightagents.knowledge.memory import MemoryService


@pytest.fixture
def feedback_db():
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    engagement = EngagementRecord(name="feedback-engagement")
    db.add(engagement)
    db.flush()
    incident = IncidentRecord(
        engagement_id=engagement.id,
        external_id="feedback-alert",
        severity="medium",
        rule_id="feedback-rule",
        occurred_at=datetime.now(UTC),
    )
    db.add(incident)
    db.flush()
    handoff = HandoffRecord(
        engagement_id=engagement.id,
        incident_id=incident.id,
        payload_json={"prediction": {"event": "profiling", "probability": 0.8}},
        payload_hash="f" * 64,
        signature="signature",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    db.add(handoff)
    db.commit()
    return db, engagement, handoff


def _add_handoff(db, engagement):
    incident = db.scalar(select(IncidentRecord).where(IncidentRecord.engagement_id == engagement.id))
    handoff = HandoffRecord(
        engagement_id=engagement.id,
        incident_id=incident.id,
        payload_json={"prediction": {"event": "profiling", "probability": 0.8}},
        payload_hash=uuid4().hex * 2,
        signature="signature",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    db.add(handoff)
    db.commit()
    return handoff


def test_prediction_outcome_creates_calibration_and_scoped_memory(feedback_db) -> None:
    db, engagement, handoff = feedback_db
    service = PredictionFeedbackService(db)
    outcome = service.record(
        handoff.id,
        "reviewer",
        PredictionOutcomeCreate(
            observed_event="profiling",
            matched=True,
            observed_at=datetime.now(UTC),
            notes="confirmed by incident review",
        ),
    )
    report = service.calibration(engagement.id)
    assert round(outcome.brier_score, 2) == 0.04
    assert report["evaluated_predictions"] == 1
    assert report["mean_brier_score"] == 0.04
    assert MemoryService(db).search("confirmed profiling", engagement.id)


def test_calibration_buckets_and_minimum_sample_warning(feedback_db) -> None:
    db, engagement, handoff = feedback_db
    service = PredictionFeedbackService(db)
    service.record(
        handoff.id,
        "reviewer",
        PredictionOutcomeCreate(
            observed_event="profiling",
            matched=True,
            observed_at=datetime.now(UTC),
            notes="confirmed",
        ),
        provenance={"model_version": "local-structured", "skill_version": "predict-blue-team-chain/1"},
    )
    report = service.calibration(engagement.id)
    assert len(report["buckets"]) == 1
    bucket = report["buckets"][0]
    assert bucket["count"] == 1
    assert bucket["observed_frequency"] == 1.0
    assert bucket["model_version"] == "local-structured"
    assert bucket["skill_version"] == "predict-blue-team-chain/1"
    assert any("has 1 samples" in warning for warning in report["warnings"])
    assert (
        db.scalar(select(CalibrationBucketRecord).where(CalibrationBucketRecord.engagement_id == engagement.id))
        is not None
    )


def test_weak_label_uses_reduced_trust_and_stays_out_of_calibration(feedback_db) -> None:
    db, engagement, handoff = feedback_db
    service = PredictionFeedbackService(db)
    outcome = service.record(
        handoff.id,
        "scheduler",
        PredictionOutcomeCreate(
            observed_event="profiling",
            matched=True,
            observed_at=datetime.now(UTC),
            notes="horizon closure via confirmation query",
        ),
        label_source="horizon_auto",
    )
    assert outcome.label_source == "horizon_auto"
    memory = db.scalar(select(MemoryRecord).where(MemoryRecord.source_reference == f"prediction-outcome:{outcome.id}"))
    assert memory is not None
    assert memory.trust_score == 0.5
    report = service.calibration(engagement.id)
    assert report["evaluated_predictions"] == 0
    assert report["mean_brier_score"] is None
    assert report["buckets"] == []
    assert report["warnings"] == []
    weak = report["weak_labels"]
    assert weak["total"] == 1
    assert weak["by_source"] == {"horizon_auto": 1}
    assert weak["buckets"] == [
        {"bucket_low": 0.75, "bucket_high": 0.85, "count": 1, "observed_frequency": 1.0}
    ]


def test_operator_calibration_unaffected_by_weak_labels(feedback_db) -> None:
    db, engagement, handoff = feedback_db
    service = PredictionFeedbackService(db)
    service.record(
        handoff.id,
        "reviewer",
        PredictionOutcomeCreate(
            observed_event="profiling",
            matched=True,
            observed_at=datetime.now(UTC),
            notes="confirmed",
        ),
    )
    weak_handoff = _add_handoff(db, engagement)
    service.record(
        weak_handoff.id,
        "scheduler",
        PredictionOutcomeCreate(
            observed_event="other",
            matched=False,
            observed_at=datetime.now(UTC),
            notes="horizon closure via confirmation query",
        ),
        label_source="horizon_auto",
    )
    report = service.calibration(engagement.id)
    assert report["evaluated_predictions"] == 1
    assert report["mean_brier_score"] == 0.04
    assert len(report["buckets"]) == 1
    assert report["buckets"][0]["count"] == 1
    assert report["buckets"][0]["observed_frequency"] == 1.0
    assert any("has 1 samples" in warning for warning in report["warnings"])
    assert report["weak_labels"]["total"] == 1
    assert report["weak_labels"]["by_source"] == {"horizon_auto": 1}
    assert report["weak_labels"]["buckets"][0]["observed_frequency"] == 0.0


def test_invalid_label_source_rejected(feedback_db) -> None:
    db, engagement, handoff = feedback_db
    service = PredictionFeedbackService(db)
    with pytest.raises(ValueError, match="label_source"):
        service.record(
            handoff.id,
            "reviewer",
            PredictionOutcomeCreate(
                observed_event="profiling",
                matched=True,
                observed_at=datetime.now(UTC),
                notes="confirmed",
            ),
            label_source="guessed",
        )
