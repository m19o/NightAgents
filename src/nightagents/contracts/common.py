from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Capability(StrEnum):
    READ = "read"
    VERIFY = "verify"
    PLAN = "plan"
    EXECUTE = "execute"


class ObservationStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class VerificationStatus(StrEnum):
    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    UNKNOWN = "unknown"


class EvidenceRef(StrictModel):
    evidence_id: str
    source: str
    reference: str
    observed_at: datetime
    confidence: float = Field(ge=0, le=1)

