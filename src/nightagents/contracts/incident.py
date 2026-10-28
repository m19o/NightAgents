from datetime import datetime
from typing import Literal

from pydantic import Field

from nightagents.contracts.common import StrictModel


class IncidentWebhook(StrictModel):
    schema_version: Literal["1.0"]
    external_alert_id: str = Field(min_length=1, max_length=255)
    engagement_id: str = Field(min_length=1, max_length=255)
    redirector_asset_id: str | None = None
    severity: Literal["low", "medium", "high", "critical"]
    rule_id: str = Field(min_length=1, max_length=255)
    summary: str = Field(default="", max_length=4000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=500)
    source_health: dict = Field(default_factory=dict)
    occurred_at: datetime


class IncidentAccepted(StrictModel):
    incident_id: str
    duplicate: bool


class ApprovalRequest(StrictModel):
    operator_id: str = Field(min_length=1, max_length=255)


class ExecuteRequest(StrictModel):
    logical_route_id: str = Field(min_length=1, max_length=255)
    old_redirector_id: str = Field(min_length=1, max_length=255)
    new_redirector_id: str = Field(min_length=1, max_length=255)

