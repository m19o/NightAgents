from typing import Protocol

from nightagents.contracts.mythic import CallbackInventory, ExecutionResult, RoutePlan, RouteVerification


class MythicPort(Protocol):
    async def callback_inventory(self, engagement_id: str, route_id: str | None = None) -> CallbackInventory: ...

    async def verify_route(self, engagement_id: str, route_id: str, callback_ids: list[str]) -> RouteVerification: ...

    async def plan_migration(
        self, engagement_id: str, from_route_id: str, to_route_id: str, callback_ids: list[str]
    ) -> RoutePlan: ...

    async def execute_plan(self, plan: RoutePlan, idempotency_key: str) -> ExecutionResult: ...

