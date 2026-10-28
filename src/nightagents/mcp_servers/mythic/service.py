from datetime import UTC, datetime

from nightagents.contracts.common import Capability
from nightagents.contracts.mythic import ExecutionGrant, ExecutionResult, RoutePlan
from nightagents.ports.mythic import MythicPort
from nightagents.security.authorization import CapabilityPolicy, Principal
from nightagents.security.signing import verify_hash_signature


class MythicMCPService:
    def __init__(self, mythic: MythicPort, grant_secret: str = "development-grant-secret"):
        self.mythic = mythic
        self.policy = CapabilityPolicy()
        self.grant_secret = grant_secret

    async def list_callbacks(self, principal: Principal, engagement_id: str, route_id: str | None = None):
        self.policy.require(principal, Capability.READ)
        return await self.mythic.callback_inventory(engagement_id, route_id)

    async def verify_callback_route(
        self, principal: Principal, engagement_id: str, route_id: str, callback_ids: list[str]
    ):
        self.policy.require(principal, Capability.VERIFY)
        return await self.mythic.verify_route(engagement_id, route_id, callback_ids)

    async def plan_callback_migration(
        self,
        principal: Principal,
        engagement_id: str,
        from_route_id: str,
        to_route_id: str,
        callback_ids: list[str],
    ):
        self.policy.require(principal, Capability.PLAN)
        return await self.mythic.plan_migration(engagement_id, from_route_id, to_route_id, callback_ids)

    async def apply_route_plan(
        self,
        principal: Principal,
        plan: RoutePlan,
        grant: ExecutionGrant,
        idempotency_key: str,
    ) -> ExecutionResult:
        self.policy.require(principal, Capability.EXECUTE)
        expires_at = grant.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= datetime.now(UTC):
            raise PermissionError("execution grant expired")
        if grant.allowed_tool != "mythic.apply_route_plan" or grant.allowed_plan_id != plan.plan_id:
            raise PermissionError("execution grant scope mismatch")
        if grant.engagement_id != plan.engagement_id:
            raise PermissionError("execution grant engagement mismatch")
        if not verify_hash_signature(self.grant_secret, grant.proposal_hash, grant.signature):
            raise PermissionError("invalid execution grant signature")
        return await self.mythic.execute_plan(plan, idempotency_key)
