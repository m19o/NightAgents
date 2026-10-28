from datetime import datetime
from enum import StrEnum

from pydantic import Field

from nightagents.contracts.common import EvidenceRef, StrictModel


class Disposition(StrEnum):
    PROPOSE = "propose"
    ABSTAIN = "abstain"
    REQUEST_EVIDENCE = "request_evidence"


class Prediction(StrictModel):
    event: str
    probability: float = Field(ge=0, le=1)
    horizon_seconds: int = Field(ge=1)
    confirmation_query_id: str


class ActionRecommendation(StrictModel):
    skill: str
    redirector_id: str
    parameters: dict = Field(default_factory=dict)


class AgentHandoff(StrictModel):
    handoff_version: str = "1.0"
    handoff_id: str
    from_agent: str = "nightwatcher"
    to_agent: str = "nightrouter"
    engagement_id: str
    incident_id: str
    phase: str
    confidence: float = Field(ge=0, le=1)
    disposition: Disposition
    prediction: Prediction | None
    recommendation: ActionRecommendation | None
    evidence: list[EvidenceRef]
    unknowns: list[str]
    created_at: datetime
    expires_at: datetime
    payload_hash: str
    signature: str


class ApprovedHandoff(StrictModel):
    handoff: AgentHandoff
    operator_id: str
    approved_at: datetime
    approval_signature: str

