from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from nightagents.contracts.common import ObservationStatus, VerificationStatus
from nightagents.contracts.infrastructure import InfrastructurePlan, InfrastructureResult, RouteHealth
from nightagents.contracts.mythic import RouteVerification
from nightagents.db.models import AssetRecord, AssetRelationshipRecord, InfrastructureOperationRecord


class DatabaseInfrastructure:
    """Persistent logical routing state for orchestration and safe lab execution."""

    def __init__(self, db: Session):
        self.db = db

    async def route_health(self, engagement_id: str, redirector_id: str) -> RouteHealth:
        asset = self._asset(engagement_id, "redirector", redirector_id)
        configured = asset is not None
        checks = dict(asset.attributes_json) if asset else {}
        healthy = bool(asset and asset.state == "active" and checks.get("health_verified", False))
        return RouteHealth(
            route_id=redirector_id,
            status=ObservationStatus.SUCCESS if configured else ObservationStatus.UNAVAILABLE,
            healthy=healthy,
            observed_at=datetime.now(UTC),
            checks={"registered": configured, "state": asset.state if asset else None, **checks},
            error=None if configured else "redirector is not registered",
        )

    async def plan_route_switch(
        self,
        engagement_id: str,
        logical_route_id: str,
        old_redirector_id: str,
        new_redirector_id: str,
    ) -> InfrastructurePlan:
        route = self._required_asset(engagement_id, "logical_route", logical_route_id)
        old = self._required_asset(engagement_id, "redirector", old_redirector_id)
        new = self._required_asset(engagement_id, "redirector", new_redirector_id)
        relationship = self._route_target(route.id)
        if relationship is None or relationship.target_asset_id != old.id:
            raise ValueError("logical route does not target the declared old redirector")
        if new.state != "active" or not new.attributes_json.get("health_verified", False):
            raise ValueError("replacement redirector lacks current verified health")
        return InfrastructurePlan(
            plan_id=str(uuid4()),
            engagement_id=engagement_id,
            logical_route_id=logical_route_id,
            old_redirector_id=old_redirector_id,
            new_redirector_id=new_redirector_id,
            preconditions=["replacement health verified", "operator approval current", "callback evidence current"],
            steps=["switch persistent logical route", "verify callback continuity", "drain old route"],
            rollback_steps=["restore previous logical route", "verify previous route"],
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )

    async def execute_route_switch(
        self, plan: InfrastructurePlan, idempotency_key: str
    ) -> InfrastructureResult:
        prior = self._prior_result(idempotency_key)
        if prior:
            return prior
        route = self._required_asset(plan.engagement_id, "logical_route", plan.logical_route_id)
        old = self._required_asset(plan.engagement_id, "redirector", plan.old_redirector_id)
        new = self._required_asset(plan.engagement_id, "redirector", plan.new_redirector_id)
        relationship = self._route_target(route.id)
        if relationship is None or relationship.target_asset_id != old.id:
            raise ValueError("route state changed after planning")
        relationship.target_asset_id = new.id
        result = InfrastructureResult(
            success=True,
            plan_id=plan.plan_id,
            idempotency_key=idempotency_key,
            previous_state={"target": old.external_id},
            current_state={"target": new.external_id},
            message="persistent logical route switched",
        )
        self._record(plan, "switch", result)
        return result

    async def rollback_route_switch(
        self, plan: InfrastructurePlan, idempotency_key: str
    ) -> InfrastructureResult:
        prior = self._prior_result(idempotency_key)
        if prior:
            return prior
        route = self._required_asset(plan.engagement_id, "logical_route", plan.logical_route_id)
        old = self._required_asset(plan.engagement_id, "redirector", plan.old_redirector_id)
        relationship = self._route_target(route.id)
        previous = relationship.target_asset_id if relationship else None
        if relationship is None:
            relationship = AssetRelationshipRecord(
                engagement_id=plan.engagement_id,
                source_asset_id=route.id,
                relationship="targets",
                target_asset_id=old.id,
            )
            self.db.add(relationship)
        else:
            relationship.target_asset_id = old.id
        result = InfrastructureResult(
            success=True,
            plan_id=plan.plan_id,
            idempotency_key=idempotency_key,
            previous_state={"target_asset_id": previous},
            current_state={"target": old.external_id},
            message="persistent logical route restored",
        )
        self._record(plan, "rollback", result)
        return result

    def _record(self, plan: InfrastructurePlan, operation: str, result: InfrastructureResult) -> None:
        self.db.add(
            InfrastructureOperationRecord(
                engagement_id=plan.engagement_id,
                plan_id=plan.plan_id,
                operation=operation,
                idempotency_key=result.idempotency_key,
                result_json=result.model_dump(mode="json"),
            )
        )
        self.db.commit()

    def _prior_result(self, idempotency_key: str) -> InfrastructureResult | None:
        record = self.db.scalar(
            select(InfrastructureOperationRecord).where(
                InfrastructureOperationRecord.idempotency_key == idempotency_key
            )
        )
        return InfrastructureResult.model_validate(record.result_json) if record else None

    def _asset(self, engagement_id: str, asset_type: str, external_id: str) -> AssetRecord | None:
        return self.db.scalar(
            select(AssetRecord).where(
                AssetRecord.engagement_id == engagement_id,
                AssetRecord.asset_type == asset_type,
                AssetRecord.external_id == external_id,
            )
        )

    def _required_asset(self, engagement_id: str, asset_type: str, external_id: str) -> AssetRecord:
        asset = self._asset(engagement_id, asset_type, external_id)
        if asset is None:
            raise ValueError(f"{asset_type} is not registered: {external_id}")
        return asset

    def _route_target(self, route_asset_id: str) -> AssetRelationshipRecord | None:
        return self.db.scalar(
            select(AssetRelationshipRecord).where(
                AssetRelationshipRecord.source_asset_id == route_asset_id,
                AssetRelationshipRecord.relationship == "targets",
                AssetRelationshipRecord.valid_until.is_(None),
            )
        )

    async def verify_callback_route(self, engagement_id: str, route_id: str, callback_ids: list[str]):
        # Database mode is fail-closed: with no real traffic movement there is
        # no edge evidence, so continuity is always UNKNOWN.
        return RouteVerification(
            status=VerificationStatus.UNKNOWN,
            route_id=route_id,
            missing_callback_ids=list(callback_ids),
            reason="database mode cannot prove route continuity",
        )
