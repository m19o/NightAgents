from datetime import datetime

from pydantic import Field

from nightagents.contracts.common import ObservationStatus, StrictModel


class RouteHealth(StrictModel):
    route_id: str
    status: ObservationStatus
    healthy: bool
    observed_at: datetime
    checks: dict = Field(default_factory=dict)
    error: str | None = None


class InfrastructurePlan(StrictModel):
    plan_id: str
    engagement_id: str
    logical_route_id: str
    old_redirector_id: str
    new_redirector_id: str
    preconditions: list[str]
    steps: list[str]
    rollback_steps: list[str]
    expires_at: datetime


class InfrastructureResult(StrictModel):
    success: bool
    plan_id: str
    idempotency_key: str
    previous_state: dict = Field(default_factory=dict)
    current_state: dict = Field(default_factory=dict)
    message: str

