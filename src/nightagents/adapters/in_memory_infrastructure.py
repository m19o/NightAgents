from datetime import UTC, datetime, timedelta
from uuid import uuid4

from nightagents.contracts.common import ObservationStatus, VerificationStatus
from nightagents.contracts.infrastructure import InfrastructurePlan, InfrastructureResult, RouteHealth
from nightagents.contracts.mythic import RouteVerification


class InMemoryInfrastructure:
    def __init__(self, routes: dict[str, str] | None = None, health: dict[str, bool] | None = None):
        self.routes = routes or {}
        self.health = health or {}
        self.results: dict[str, InfrastructureResult] = {}

    async def route_health(self, engagement_id: str, redirector_id: str) -> RouteHealth:
        healthy = self.health.get(redirector_id, False)
        return RouteHealth(
            route_id=redirector_id,
            status=ObservationStatus.SUCCESS,
            healthy=healthy,
            observed_at=datetime.now(UTC),
            checks={"engagement_id": engagement_id, "configured": redirector_id in self.health},
        )

    async def plan_route_switch(
        self,
        engagement_id: str,
        logical_route_id: str,
        old_redirector_id: str,
        new_redirector_id: str,
    ) -> InfrastructurePlan:
        return InfrastructurePlan(
            plan_id=str(uuid4()),
            engagement_id=engagement_id,
            logical_route_id=logical_route_id,
            old_redirector_id=old_redirector_id,
            new_redirector_id=new_redirector_id,
            preconditions=["replacement healthy", "callback inventory current", "approval current"],
            steps=["enable replacement", "switch logical route", "observe callbacks", "drain prior route"],
            rollback_steps=["restore logical route", "verify prior route"],
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )

    async def execute_route_switch(
        self, plan: InfrastructurePlan, idempotency_key: str
    ) -> InfrastructureResult:
        if idempotency_key in self.results:
            return self.results[idempotency_key]
        previous = self.routes.get(plan.logical_route_id)
        if not self.health.get(plan.new_redirector_id, False):
            return InfrastructureResult(
                success=False,
                plan_id=plan.plan_id,
                idempotency_key=idempotency_key,
                previous_state={"target": previous},
                current_state={"target": previous},
                message="replacement redirector is unhealthy",
            )
        self.routes[plan.logical_route_id] = plan.new_redirector_id
        result = InfrastructureResult(
            success=True,
            plan_id=plan.plan_id,
            idempotency_key=idempotency_key,
            previous_state={"target": previous},
            current_state={"target": plan.new_redirector_id},
            message="logical route switched",
        )
        self.results[idempotency_key] = result
        return result

    async def rollback_route_switch(
        self, plan: InfrastructurePlan, idempotency_key: str
    ) -> InfrastructureResult:
        if idempotency_key in self.results:
            return self.results[idempotency_key]
        current = self.routes.get(plan.logical_route_id)
        self.routes[plan.logical_route_id] = plan.old_redirector_id
        result = InfrastructureResult(
            success=True,
            plan_id=plan.plan_id,
            idempotency_key=idempotency_key,
            previous_state={"target": current},
            current_state={"target": plan.old_redirector_id},
            message="logical route restored",
        )
        self.results[idempotency_key] = result
        return result


    async def verify_callback_route(self, engagement_id: str, route_id: str, callback_ids: list[str]):
        return RouteVerification(
            status=VerificationStatus.UNKNOWN,
            route_id=route_id,
            missing_callback_ids=list(callback_ids),
            reason="in-memory provider cannot prove route continuity",
        )
