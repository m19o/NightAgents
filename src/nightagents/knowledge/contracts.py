from datetime import datetime
from typing import Literal

from pydantic import Field, HttpUrl

from nightagents.contracts.common import StrictModel


class KnowledgeSource(StrictModel):
    source_id: str
    publisher: str
    source_uri: HttpUrl
    source_type: str
    trust_tier: str
    license_policy: str = "review"
    poll_interval_seconds: int = Field(default=86400, ge=300)
    allowed_host: str
    enabled: bool = True


class FetchResult(StrictModel):
    source_id: str
    status: str
    content_hash: str | None = None
    document_id: str | None = None
    retrieved_at: datetime
    changed: bool = False
    error: str | None = None


class MemorySearchResult(StrictModel):
    memory_id: str
    memory_type: str
    content: str
    source_reference: str
    score: float
    metadata: dict = Field(default_factory=dict)


class KnowledgeDocumentReview(StrictModel):
    decision: Literal["approve", "reject"]
    risk_acceptance: bool = False
    notes: str = Field(default="", max_length=4000)


class KnowledgeCandidateReview(StrictModel):
    decision: Literal["approve", "reject"]
    notes: str = Field(default="", max_length=4000)
