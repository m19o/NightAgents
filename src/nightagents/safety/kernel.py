from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel

from nightagents.contracts.common import ObservationStatus, VerificationStatus
from nightagents.contracts.mythic import CallbackInventory, ExecutionGrant, RouteVerification
from nightagents.security.signing import sign_hash


class Transition(StrEnum):
    PREPARE_FAILOVER = "prepare_failover"
    BEGIN_MIGRATION = "begin_migration"
    DRAIN_OLD_ROUTE = "drain_old_route"
    RETIRE_OLD_ROUTE = "retire_old_route"


class SafetyDecision(BaseModel):
    allowed: bool
    reason: str


class SafetyKernel:
    def authorize_transition(
        self,
        transition: Transition,
        inventory: CallbackInventory,
        replacement_verification: RouteVerification | None,
        approval_expires_at: datetime,
    ) -> SafetyDecision:
        if approval_expires_at.tzinfo is None:
            approval_expires_at = approval_expires_at.replace(tzinfo=UTC)
        if approval_expires_at <= datetime.now(UTC):
            return SafetyDecision(allowed=False, reason="approval expired")
        if inventory.status is not ObservationStatus.SUCCESS:
            return SafetyDecision(allowed=False, reason="callback inventory is not trustworthy")
        if transition in {Transition.DRAIN_OLD_ROUTE, Transition.RETIRE_OLD_ROUTE} and inventory.callbacks:
            if replacement_verification is None:
                return SafetyDecision(allowed=False, reason="replacement callback verification missing")
            if replacement_verification.status is not VerificationStatus.CONFIRMED:
                return SafetyDecision(allowed=False, reason="replacement callbacks not confirmed")
        return SafetyDecision(allowed=True, reason="safety preconditions satisfied")

    def issue_execution_grant(
        self,
        secret: str,
        proposal_hash: str,
        engagement_id: str,
        allowed_tool: str,
        allowed_plan_id: str,
        expires_at: datetime,
    ) -> ExecutionGrant:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= datetime.now(UTC):
            raise ValueError("cannot issue expired execution grant")
        return ExecutionGrant(
            grant_id=str(uuid4()),
            proposal_hash=proposal_hash,
            engagement_id=engagement_id,
            allowed_tool=allowed_tool,
            allowed_plan_id=allowed_plan_id,
            expires_at=expires_at,
            signature=sign_hash(secret, proposal_hash),
        )
