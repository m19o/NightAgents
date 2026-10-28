from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nightagents.contracts.feedback import PredictionOutcomeCreate
from nightagents.db.models import (
    CalibrationBucketRecord,
    HandoffRecord,
    PredictionOutcomeRecord,
    PredictionRecord,
)
from nightagents.db.repositories import AuditRepository
from nightagents.knowledge.memory import MemoryService


class PredictionFeedbackService:
    """Records prediction outcomes, updates calibration buckets, and persists provenance versions.

    Outcomes carry a ``label_source``: ``operator`` labels are human ground truth (full trust),
    while ``horizon_auto``/``adjudicator`` labels are weak labels (trust 0.5) that never feed
    calibration buckets or operator calibration statistics.
    """

    MINIMUM_BUCKET_SAMPLES = 30
    LABEL_SOURCE_OPERATOR = "operator"
    LABEL_SOURCE_HORIZON_AUTO = "horizon_auto"
    LABEL_SOURCE_ADJUDICATOR = "adjudicator"
    ALLOWED_LABEL_SOURCES = frozenset(
        {LABEL_SOURCE_OPERATOR, LABEL_SOURCE_HORIZON_AUTO, LABEL_SOURCE_ADJUDICATOR}
    )
    WEAK_LABEL_TRUST_SCORE = 0.5

    def __init__(self, db: Session):
        self.db = db
        self.audit = AuditRepository(db)

    def record(
        self,
        handoff_id: str,
        operator_id: str,
        outcome: PredictionOutcomeCreate,
        prediction_id: str | None = None,
        provenance: dict | None = None,
        label_source: str = "operator",
    ) -> PredictionOutcomeRecord:
        if label_source not in self.ALLOWED_LABEL_SOURCES:
            raise ValueError(f"label_source must be one of {sorted(self.ALLOWED_LABEL_SOURCES)}")
        existing = self.db.scalar(
            select(PredictionOutcomeRecord).where(PredictionOutcomeRecord.handoff_id == handoff_id)
        )
        if existing:
            return existing
        handoff = self.db.get(HandoffRecord, handoff_id)
        if handoff is None:
            raise ValueError("handoff not found")
        prediction = handoff.payload_json.get("prediction")
        if not prediction:
            raise ValueError("handoff has no prediction")
        probability = float(prediction["probability"])
        observed_value = 1.0 if outcome.matched else 0.0
        provenance = provenance or {}
        record = PredictionOutcomeRecord(
            prediction_id=prediction_id,
            handoff_id=handoff.id,
            engagement_id=handoff.engagement_id,
            predicted_event=str(prediction["event"]),
            predicted_probability=probability,
            observed_event=outcome.observed_event,
            matched=outcome.matched,
            brier_score=(probability - observed_value) ** 2,
            label_source=label_source,
            operator_id=operator_id,
            notes=outcome.notes,
            observed_at=outcome.observed_at,
            model_version=provenance.get("model_version", ""),
            prompt_version=provenance.get("prompt_version", ""),
            skill_version=provenance.get("skill_version", ""),
            dataset_version=provenance.get("dataset_version", ""),
        )
        self.db.add(record)
        self.db.flush()
        if label_source == self.LABEL_SOURCE_OPERATOR:
            self._update_bucket(handoff.engagement_id, probability, outcome.matched, provenance)
        if prediction_id:
            pred = self.db.get(PredictionRecord, prediction_id)
            if pred is not None and pred.status == "pending":
                pred.outcome_id = record.id
                pred.status = "outcome_recorded"
        trust_score = 1.0 if label_source == self.LABEL_SOURCE_OPERATOR else self.WEAK_LABEL_TRUST_SCORE
        MemoryService(self.db).remember_episode(
            handoff.engagement_id,
            (
                f"Prediction '{record.predicted_event}' with probability {probability:.2f}; "
                f"observed '{record.observed_event}'; matched={record.matched}. {record.notes}"
            ),
            f"prediction-outcome:{record.id}",
            {
                "handoff_id": handoff.id,
                "brier_score": record.brier_score,
                "reviewed_by": operator_id,
                "label_source": label_source,
            },
            commit=False,
            trust_score=trust_score,
        )
        self.audit.append(
            operator_id,
            "prediction.outcome_recorded",
            "handoff",
            handoff.id,
            {
                "outcome_id": record.id,
                "matched": record.matched,
                "brier_score": record.brier_score,
                "label_source": label_source,
            },
            handoff.engagement_id,
        )
        self.db.commit()
        return record

    @staticmethod
    def _bucket_bounds(probability: float) -> tuple[float, float]:
        bucket_low = max(0.0, round(probability - 0.05, 2))
        bucket_high = min(1.0, round(probability + 0.05, 2))
        return bucket_low, bucket_high

    def _update_bucket(
        self,
        engagement_id: str,
        probability: float,
        matched: bool,
        provenance: dict,
    ) -> None:
        bucket_low, bucket_high = self._bucket_bounds(probability)
        model_version = provenance.get("model_version", "")
        skill_version = provenance.get("skill_version", "")
        bucket = self.db.scalar(
            select(CalibrationBucketRecord).where(
                CalibrationBucketRecord.engagement_id == engagement_id,
                CalibrationBucketRecord.bucket_low == bucket_low,
                CalibrationBucketRecord.bucket_high == bucket_high,
                CalibrationBucketRecord.model_version == model_version,
                CalibrationBucketRecord.skill_version == skill_version,
            )
        )
        if bucket is None:
            bucket = CalibrationBucketRecord(
                engagement_id=engagement_id,
                bucket_low=bucket_low,
                bucket_high=bucket_high,
                model_version=model_version,
                prompt_version=provenance.get("prompt_version", ""),
                skill_version=skill_version,
                dataset_version=provenance.get("dataset_version", ""),
            )
            self.db.add(bucket)
            self.db.flush()
        bucket.count += 1
        total = bucket.count
        previous = bucket.observed_frequency or 0.0
        bucket.observed_frequency = round(((previous * (total - 1)) + (1.0 if matched else 0.0)) / total, 4)

    def calibration(self, engagement_id: str) -> dict:
        """Operator-label calibration report; weak labels are reported separately, never mixed in."""
        count, average = self.db.execute(
            select(func.count(PredictionOutcomeRecord.id), func.avg(PredictionOutcomeRecord.brier_score)).where(
                PredictionOutcomeRecord.engagement_id == engagement_id,
                PredictionOutcomeRecord.label_source == self.LABEL_SOURCE_OPERATOR,
            )
        ).one()
        buckets = list(
            self.db.scalars(
                select(CalibrationBucketRecord).where(
                    CalibrationBucketRecord.engagement_id == engagement_id
                ).order_by(CalibrationBucketRecord.bucket_low)
            )
        )
        warnings = []
        for bucket in buckets:
            if bucket.count < self.MINIMUM_BUCKET_SAMPLES:
                warnings.append(
                    f"bucket {bucket.bucket_low:.2f}-{bucket.bucket_high:.2f} has {bucket.count} samples"
                )
        return {
            "engagement_id": engagement_id,
            "evaluated_predictions": int(count),
            "mean_brier_score": round(float(average), 4) if average is not None else None,
            "buckets": [
                {
                    "bucket_low": bucket.bucket_low,
                    "bucket_high": bucket.bucket_high,
                    "count": bucket.count,
                    "observed_frequency": bucket.observed_frequency,
                    "expected_frequency": round((bucket.bucket_low + bucket.bucket_high) / 2, 4),
                    "model_version": bucket.model_version,
                    "skill_version": bucket.skill_version,
                }
                for bucket in buckets
            ],
            "weak_labels": self._weak_label_summary(engagement_id),
            "warnings": warnings,
        }

    def _weak_label_summary(self, engagement_id: str) -> dict:
        """Counts of non-operator (weak) labels per label_source and per probability bucket."""
        weak_outcomes = list(
            self.db.scalars(
                select(PredictionOutcomeRecord).where(
                    PredictionOutcomeRecord.engagement_id == engagement_id,
                    PredictionOutcomeRecord.label_source != self.LABEL_SOURCE_OPERATOR,
                )
            )
        )
        by_source: dict[str, int] = {}
        bucket_stats: dict[tuple[float, float], dict] = {}
        for outcome in weak_outcomes:
            by_source[outcome.label_source] = by_source.get(outcome.label_source, 0) + 1
            bounds = self._bucket_bounds(outcome.predicted_probability)
            stats = bucket_stats.setdefault(bounds, {"count": 0, "matched": 0})
            stats["count"] += 1
            stats["matched"] += 1 if outcome.matched else 0
        return {
            "total": len(weak_outcomes),
            "by_source": by_source,
            "buckets": [
                {
                    "bucket_low": bucket_low,
                    "bucket_high": bucket_high,
                    "count": stats["count"],
                    "observed_frequency": round(stats["matched"] / stats["count"], 4),
                }
                for (bucket_low, bucket_high), stats in sorted(bucket_stats.items())
            ],
        }
