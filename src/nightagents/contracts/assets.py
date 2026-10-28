from typing import Literal

from pydantic import Field

from nightagents.contracts.common import StrictModel


class EngagementCreate(StrictModel):
    name: str = Field(min_length=1, max_length=255)


class RedirectorRegistration(StrictModel):
    redirector_id: str = Field(min_length=1, max_length=255)
    domain: str = Field(min_length=1, max_length=255)
    logical_route_id: str = Field(min_length=1, max_length=255)
    provider_resource_id: str = Field(min_length=1, max_length=255)
    mythic_profile: str = Field(min_length=1, max_length=255)
    failover_group: str = Field(min_length=1, max_length=255)


class RedirectorHealthUpdate(StrictModel):
    status: Literal["healthy", "unhealthy"]
    evidence: dict = Field(default_factory=dict)
