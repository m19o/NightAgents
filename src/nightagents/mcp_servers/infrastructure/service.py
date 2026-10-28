from datetime import UTC, datetime

from nightagents.contracts.common import Capability
from nightagents.contracts.infrastructure import InfrastructurePlan
from nightagents.contracts.mythic import ExecutionGrant
from nightagents.ports.infrastructure import InfrastructurePort
from nightagents.security.authorization import CapabilityPolicy, Principal
from nightagents.security.signing import verify_hash_signature


class InfrastructureMCPService:
    def __init__(self, infrastructure: InfrastructurePort, grant_secret: str):
        self.infrastructure = infrastructure
        self.grant_secret = grant_secret
        self.policy = CapabilityPolicy()

    async def route_health(self, principal: Principal, engagement_id: str, redirector_id: str):
        self.policy.require(principal, Capability.VERIFY)
        return await self.infrastructure.route_health(engagement_id, redirector_id)

    async def plan_route_switch(
        self,
        principal: Principal,
        engagement_id: str,
        logical_route_id: str,
        old_redirector_id: str,
        new_redirector_id: str,
    ):
        self.policy.require(principal, Capability.PLAN)
        return await self.infrastructure.plan_route_switch(
            engagement_id, logical_route_id, old_redirector_id, new_redirector_id
        )

    async def execute_route_switch(
        self,
        principal: Principal,
        plan: InfrastructurePlan,
        grant: ExecutionGrant,
        idempotency_key: str,
    ):
        self.policy.require(principal, Capability.EXECUTE)
        self._validate_grant(plan, grant, "infrastructure.execute_route_switch")
        return await self.infrastructure.execute_route_switch(plan, idempotency_key)

    async def rollback_route_switch(
        self,
        principal: Principal,
        plan: InfrastructurePlan,
        grant: ExecutionGrant,
        idempotency_key: str,
    ):
        self.policy.require(principal, Capability.EXECUTE)
        self._validate_grant(plan, grant, "infrastructure.rollback_route_switch")
        return await self.infrastructure.rollback_route_switch(plan, idempotency_key)

    async def verify_callback_route(
        self, principal: Principal, engagement_id: str, route_id: str, callback_ids: list[str]
    ):
        self.policy.require(principal, Capability.VERIFY)
        return await self.infrastructure.verify_callback_route(engagement_id, route_id, callback_ids)

    def _validate_grant(self, plan: InfrastructurePlan, grant: ExecutionGrant, tool: str) -> None:
        expires_at = grant.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= datetime.now(UTC):
            raise PermissionError("execution grant expired")
        if grant.allowed_tool != tool or grant.allowed_plan_id != plan.plan_id:
            raise PermissionError("execution grant scope mismatch")
        if grant.engagement_id != plan.engagement_id:
            raise PermissionError("execution grant engagement mismatch")
        if not verify_hash_signature(self.grant_secret, grant.proposal_hash, grant.signature):
            raise PermissionError("invalid execution grant signature")
