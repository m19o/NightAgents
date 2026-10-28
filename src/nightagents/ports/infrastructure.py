from typing import Protocol

from nightagents.contracts.infrastructure import InfrastructurePlan, InfrastructureResult, RouteHealth


class InfrastructurePort(Protocol):
    async def route_health(self, engagement_id: str, redirector_id: str) -> RouteHealth: ...

    async def plan_route_switch(
        self,
        engagement_id: str,
        logical_route_id: str,
        old_redirector_id: str,
        new_redirector_id: str,
    ) -> InfrastructurePlan: ...

    async def execute_route_switch(
        self, plan: InfrastructurePlan, idempotency_key: str
    ) -> InfrastructureResult: ...

    async def rollback_route_switch(
        self, plan: InfrastructurePlan, idempotency_key: str
    ) -> InfrastructureResult: ...

