from datetime import datetime

from pydantic import Field

from nightagents.contracts.common import ObservationStatus, StrictModel, VerificationStatus


class Callback(StrictModel):
    callback_id: str
    operation_id: str
    payload_type: str
    c2_profile: str
    route_id: str | None = None
    last_seen: datetime
    expected_checkin_seconds: int | None = Field(default=None, ge=1)
    active: bool


class CallbackInventory(StrictModel):
    status: ObservationStatus
    callbacks: list[Callback] = Field(default_factory=list)
    observed_at: datetime
    error: str | None = None


class RouteVerification(StrictModel):
    status: VerificationStatus
    route_id: str
    observed_callback_ids: list[str] = Field(default_factory=list)
    missing_callback_ids: list[str] = Field(default_factory=list)
    reason: str


class RoutePlan(StrictModel):
    plan_id: str
    engagement_id: str
    from_route_id: str
    to_route_id: str
    callback_ids: list[str]
    preconditions: list[str]
    rollback_steps: list[str]
    expires_at: datetime


class ExecutionGrant(StrictModel):
    grant_id: str
    proposal_hash: str
    engagement_id: str
    allowed_tool: str
    allowed_plan_id: str
    expires_at: datetime
    signature: str


class ExecutionResult(StrictModel):
    success: bool
    plan_id: str
    idempotency_key: str
    message: str

