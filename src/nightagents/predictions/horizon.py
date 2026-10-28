from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from nightagents.db.models import (
    HandoffRecord,
    OperatorReviewTaskRecord,
    PredictionOutcomeRecord,
    PredictionRecord,
)
from nightagents.db.repositories import AuditRepository
from nightagents.feedback import PredictionFeedbackService
from nightagents.mcp_client import MCPGateway


class PredictionHorizonService:
    """At horizon expiry, verify evidence-source health and run the declared confirmation query.

    A durable outcome is recorded only when the confirmation query returns supporting evidence
    and the required sources are independently healthy. Otherwise an operator-review task is
    created. Silence or missing data is never treated as a negative outcome.
    """

    def __init__(self, db: Session, gateway: MCPGateway):
        self.db = db
        self.gateway = gateway
        self.audit = AuditRepository(db)

    async def verify_at_expiry(self, prediction_id: str, operator_id: str = "scheduler") -> dict:
        prediction = self.db.get(PredictionRecord, prediction_id)
        if prediction is None:
            raise ValueError("prediction not found")
        if prediction.status not in {"pending", "expired"}:
            raise ValueError(f"prediction is already {prediction.status}")

        handoff = self.db.get(HandoffRecord, prediction.handoff_id)
        if handoff is None:
            raise ValueError("handoff not found")

        required_sources = list((handoff.payload_json or {}).get("evidence", []))
        required_source_ids = sorted({src.get("source", "logs-nginx.access-*") for src in required_sources})
        if not required_source_ids:
            required_source_ids = ["logs-nginx.access-*"]

        source_health = await self._check_source_health(required_source_ids)
        unhealthy = [src for src, state in source_health.items() if state.get("status") != "healthy"]

        confirmation = await self._run_confirmation_query(prediction, handoff)
        supported = confirmation.get("supported", False) and not unhealthy

        if supported:
            observed = confirmation.get("observed_event") or prediction.predicted_event
            matched = bool(confirmation.get("matched", False))
            outcome = PredictionOutcomeRecord(
                prediction_id=prediction.id,
                handoff_id=prediction.handoff_id,
                engagement_id=prediction.engagement_id,
                predicted_event=prediction.predicted_event,
                predicted_probability=prediction.predicted_probability,
                observed_event=observed,
                matched=matched,
                brier_score=(prediction.predicted_probability - (1.0 if matched else 0.0)) ** 2,
                label_source=PredictionFeedbackService.LABEL_SOURCE_HORIZON_AUTO,
                operator_id=operator_id,
                notes="horizon closure via confirmation query",
                observed_at=datetime.now(UTC),
            )
            self.db.add(outcome)
            self.db.flush()
            prediction.outcome_id = outcome.id
            prediction.status = "outcome_recorded"
            self.audit.append(
                operator_id,
                "prediction.horizon_outcome_recorded",
                "prediction",
                prediction.id,
                {"outcome_id": outcome.id, "matched": matched},
                prediction.engagement_id,
            )
            self.db.commit()
            return {"status": "outcome_recorded", "outcome_id": outcome.id, "matched": matched}

        reason_parts = []
        if unhealthy:
            reason_parts.append(f"unhealthy sources: {', '.join(unhealthy)}")
        if not confirmation.get("supported"):
            reason_parts.append("confirmation query did not return supporting evidence")
        review = OperatorReviewTaskRecord(
            engagement_id=prediction.engagement_id,
            target_type="prediction",
            target_id=prediction.id,
            reason="; ".join(reason_parts) or "horizon expired without supporting evidence",
        )
        self.db.add(review)
        self.db.flush()
        prediction.review_task_id = review.id
        prediction.status = "operator_review"
        self.audit.append(
            operator_id,
            "prediction.horizon_review_created",
            "prediction",
            prediction.id,
            {"review_task_id": review.id, "reason": review.reason},
            prediction.engagement_id,
        )
        self.db.commit()
        return {"status": "operator_review", "review_task_id": review.id, "reason": review.reason}

    async def _check_source_health(self, source_ids: list[str]) -> dict[str, dict]:
        try:
            result = await self.gateway.call_tool("elastic", "get_source_health", {"sources": source_ids})
        except Exception:
            return {src: {"status": "unknown"} for src in source_ids}
        return result if isinstance(result, dict) else {}

    async def _run_confirmation_query(self, prediction: PredictionRecord, handoff: HandoffRecord) -> dict:
        try:
            result = await self.gateway.call_tool(
                "elastic",
                "run_catalog_query",
                {
                    "query_id": prediction.confirmation_query_id,
                    "engagement_id": handoff.engagement_id,
                    "lookback_seconds": prediction.horizon_seconds,
                },
            )
        except Exception as exc:
            return {"supported": False, "error": str(exc)}
        if not isinstance(result, dict):
            return {"supported": False}
        events = result.get("events", [])
        return {
            "supported": bool(events),
            "matched": bool(events),
            "observed_event": prediction.predicted_event if events else None,
            "event_count": len(events),
        }

    def due_predictions(self, limit: int = 50) -> list[PredictionRecord]:
        now = datetime.now(UTC)
        return list(
            self.db.scalars(
                select(PredictionRecord)
                .where(PredictionRecord.status == "pending")
                .where(PredictionRecord.expires_at <= now)
                .order_by(PredictionRecord.expires_at)
                .limit(limit)
            )
        )

    def resolve_review_task(self, task_id: str, operator_id: str, resolution: str) -> OperatorReviewTaskRecord:
        task = self.db.get(OperatorReviewTaskRecord, task_id)
        if task is None:
            raise ValueError("review task not found")
        if task.status != "open":
            raise ValueError("review task is already resolved")
        task.status = "resolved"
        task.resolution = resolution
        task.resolved_by = operator_id
        task.resolved_at = datetime.now(UTC)
        prediction = self.db.scalar(
            select(PredictionRecord).where(PredictionRecord.review_task_id == task.id)
        )
        if prediction is not None and prediction.status == "operator_review":
            prediction.status = "review_resolved"
        self.audit.append(
            operator_id,
            "operator_review.resolved",
            "operator_review_task",
            task.id,
            {"resolution": resolution},
            task.engagement_id,
        )
        self.db.commit()
        return task
