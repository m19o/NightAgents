from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from nightagents.workflows.budget import WorkflowBudget


@dataclass(frozen=True)
class InvestigationRequest:
    incident_id: str
    engagement_id: str
    required_sources: tuple[str, ...]


class NightWatcherWorkflow:
    def __init__(
        self,
        source_health: Callable[[tuple[str, ...]], Awaitable[dict]],
        investigate: Callable[[InvestigationRequest], Awaitable[dict]],
        reason: Callable[[dict], Awaitable[dict]],
        critique: Callable[[dict], Awaitable[dict]],
    ):
        self.source_health = source_health
        self.investigate = investigate
        self.reason = reason
        self.critique = critique

    async def run(self, request: InvestigationRequest, budget: WorkflowBudget | None = None) -> dict:
        limits = budget or WorkflowBudget()
        limits.consume_iteration()
        limits.consume_tool()
        health = await self.source_health(request.required_sources)
        unhealthy = [source for source, state in health.items() if state.get("status") != "healthy"]
        if unhealthy:
            return {"disposition": "request_evidence", "unknowns": unhealthy, "reason": "required sources unhealthy"}
        limits.consume_tool()
        evidence = await self.investigate(request)
        limits.set_context_tokens(int(evidence.get("estimated_tokens", 0)))
        assessment = await self.reason(evidence)
        critique = await self.critique({"evidence": evidence, "assessment": assessment})
        if critique.get("unsupported_claims"):
            return {"disposition": "abstain", "assessment": assessment, "critique": critique}
        return {"disposition": "propose", "assessment": assessment, "critique": critique}

