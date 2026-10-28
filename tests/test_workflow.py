from nightagents.workflows.budget import WorkflowBudget
from nightagents.workflows.nightwatcher import InvestigationRequest, NightWatcherWorkflow


async def test_unhealthy_source_requests_evidence_without_reasoning() -> None:
    called = False

    async def health(_):
        return {"elastic": {"status": "unknown"}}

    async def should_not_run(_):
        nonlocal called
        called = True
        return {}

    workflow = NightWatcherWorkflow(health, should_not_run, should_not_run, should_not_run)
    result = await workflow.run(InvestigationRequest("inc-1", "eng-1", ("elastic",)))
    assert result["disposition"] == "request_evidence"
    assert not called


async def test_bounded_workflow_proposes_after_independent_critique() -> None:
    async def health(_):
        return {"elastic": {"status": "healthy"}}

    async def investigate(_):
        return {"estimated_tokens": 1000, "facts": ["fact-1"]}

    async def reason(_):
        return {"phase": "profiling", "prediction": "correlation"}

    async def critique(_):
        return {"unsupported_claims": []}

    budget = WorkflowBudget(maximum_tool_calls=2, maximum_iterations=1, maximum_context_tokens=2000)
    workflow = NightWatcherWorkflow(health, investigate, reason, critique)
    result = await workflow.run(InvestigationRequest("inc-1", "eng-1", ("elastic",)), budget)
    assert result["disposition"] == "propose"
    assert budget.tool_calls == 2

