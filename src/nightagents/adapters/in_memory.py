from datetime import UTC, datetime, timedelta
from uuid import uuid4

from nightagents.contracts.common import ObservationStatus, VerificationStatus
from nightagents.contracts.mythic import (
    Callback,
    CallbackInventory,
    ExecutionResult,
    RoutePlan,
    RouteVerification,
)


class InMemoryMythic:
    def __init__(self, callbacks: list[Callback] | None = None, available: bool = True):
        self.callbacks = callbacks or []
        self.available = available
        self.executed: dict[str, ExecutionResult] = {}

    async def callback_inventory(self, engagement_id: str, route_id: str | None = None) -> CallbackInventory:
        now = datetime.now(UTC)
        if not self.available:
            return CallbackInventory(status=ObservationStatus.UNAVAILABLE, observed_at=now, error="Mythic unavailable")
        found = [item for item in self.callbacks if item.operation_id == engagement_id]
        if route_id is not None:
            found = [item for item in found if item.route_id == route_id]
        return CallbackInventory(status=ObservationStatus.SUCCESS, callbacks=found, observed_at=now)

    async def verify_route(self, engagement_id: str, route_id: str, callback_ids: list[str]) -> RouteVerification:
        inventory = await self.callback_inventory(engagement_id, route_id)
        if inventory.status is not ObservationStatus.SUCCESS:
            return RouteVerification(
                status=VerificationStatus.UNKNOWN,
                route_id=route_id,
                missing_callback_ids=callback_ids,
                reason=inventory.error or "inventory unavailable",
            )
        observed = [
            item.callback_id for item in inventory.callbacks if item.callback_id in callback_ids and item.active
        ]
        missing = sorted(set(callback_ids) - set(observed))
        status = VerificationStatus.CONFIRMED if not missing else VerificationStatus.REFUTED
        return RouteVerification(
            status=status,
            route_id=route_id,
            observed_callback_ids=observed,
            missing_callback_ids=missing,
            reason="all callbacks observed" if not missing else "callbacks missing from route",
        )

    async def plan_migration(
        self, engagement_id: str, from_route_id: str, to_route_id: str, callback_ids: list[str]
    ) -> RoutePlan:
        return RoutePlan(
            plan_id=str(uuid4()),
            engagement_id=engagement_id,
            from_route_id=from_route_id,
            to_route_id=to_route_id,
            callback_ids=callback_ids,
            preconditions=["replacement route healthy", "callback inventory current", "operator approval current"],
            rollback_steps=["restore prior route", "verify callbacks on prior route"],
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )

    async def execute_plan(self, plan: RoutePlan, idempotency_key: str) -> ExecutionResult:
        if idempotency_key in self.executed:
            return self.executed[idempotency_key]
        result = ExecutionResult(
            success=True,
            plan_id=plan.plan_id,
            idempotency_key=idempotency_key,
            message="migration plan accepted by Mythic adapter",
        )
        self.executed[idempotency_key] = result
        return result
