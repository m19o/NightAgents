from datetime import datetime

from pydantic import Field

from nightagents.contracts.common import StrictModel


class PredictionOutcomeCreate(StrictModel):
    observed_event: str = Field(min_length=1, max_length=1024)
    matched: bool
    observed_at: datetime
    notes: str = Field(default="", max_length=4000)
