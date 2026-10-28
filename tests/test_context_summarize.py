from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from nightagents.db.base import Base
from nightagents.db.models import AgentStepRecord, EngagementRecord
from nightagents.db.session import build_engine
from nightagents.models.local import LocalStructuredModel
from nightagents.orchestration.runtime import DurableSkillOrchestrator
from nightagents.orchestration.summarize import summarize_context
from nightagents.skills.registry import SkillRegistry


def _events(count: int, message: str = "routine beacon") -> list[dict]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        {
            "@timestamp": (start + timedelta(seconds=30 * index)).isoformat(),
            "source": {"ip": f"10.0.0.{index % 3}"},
            "url": {"path": "/login" if index % 2 else "/"},
            "event": {"action": "http_request"},
            "message": message if index == count - 1 else "routine beacon",
        }
        for index in range(count)
    ]


def test_summarize_replaces_long_event_lists_with_counts_and_sample() -> None:
    events = _events(15)
    result = summarize_context({"elastic_evidence": {"events": events}}, max_raw_items=10)
    summary = result["elastic_evidence"]["events"]
    assert summary["total_count"] == 15
    assert summary["counts"]["event.action"] == {"http_request": 15}
    assert summary["counts"]["url.path"] == {"/login": 7, "/": 8}
    assert summary["counts"]["source.ip"] == {"10.0.0.0": 5, "10.0.0.1": 5, "10.0.0.2": 5}
    assert summary["sample"] == events[:3]
    assert events[0]["@timestamp"] == "2026-01-01T00:00:00+00:00"


def test_summarize_computes_time_bounds_and_interval_stats() -> None:
    events = _events(12)
    summary = summarize_context({"events": events})["events"]
    assert summary["time_bounds"] == {
        "min": "2026-01-01T00:00:00+00:00",
        "max": "2026-01-01T00:05:30+00:00",
    }
    assert summary["interval_seconds"] == {"min": 30.0, "max": 30.0, "mean": 30.0}


def test_summarize_preserves_local_model_keyword_signals() -> None:
    events = _events(20, message="JA4 fingerprint observed in certificate transparency correlation")
    summary = summarize_context({"events": events})["events"]
    for keyword in ("ja4", "fingerprint", "certificate transparency", "correlation"):
        assert keyword in summary["keyword_hits"]
    assert "takedown" not in summary["keyword_hits"]


def test_summarize_passes_small_lists_and_non_list_values_through() -> None:
    small = _events(3)
    strings = [f"note-{index}" for index in range(20)]
    context = {
        "incident": {"severity": "high", "events": small},
        "notes": strings,
        "count": 7,
        "label": "investigation",
    }
    result = summarize_context(context, max_raw_items=10)
    assert result["incident"]["events"] == small
    assert result["notes"] == strings
    assert result["count"] == 7
    assert result["label"] == "investigation"


def test_summarize_is_disabled_with_zero_max_raw_items() -> None:
    events = _events(50)
    context = {"elastic_evidence": {"events": events}}
    assert summarize_context(context, max_raw_items=0) == context


def test_summarize_does_not_mutate_the_input_context() -> None:
    events = _events(15)
    context = {"elastic_evidence": {"events": events}}
    summarize_context(context)
    assert context["elastic_evidence"]["events"] == events
    assert len(context["elastic_evidence"]["events"]) == 15


async def test_local_reasoner_phase_detection_survives_summarization() -> None:
    events = _events(25, message="passive dns correlation with jarm fingerprint")
    model = LocalStructuredModel()
    raw_context = {"incident": {"severity": "high"}, "elastic_evidence": {"events": events}}
    summarized_context = summarize_context(raw_context)
    raw_result = await model.invoke("reasoner", raw_context)
    summarized_result = await model.invoke("reasoner", summarized_context)
    assert raw_result["phase"] == "correlation"
    assert summarized_result == raw_result


class RecordingModel(LocalStructuredModel):
    def __init__(self):
        self.contexts: dict[str, dict] = {}

    async def invoke(self, role: str, context: dict) -> dict:
        self.contexts[role] = context
        return await super().invoke(role, context)


class BulkEvidenceGateway:
    def __init__(self, events: list[dict]):
        self.events = events
        self.calls = []

    async def call_tool(self, server: str, tool: str, arguments: dict):
        self.calls.append((server, tool, arguments))
        responses = {
            ("elastic", "get_source_health"): {"logs-edge": {"status": "healthy"}},
            ("elastic", "get_incident"): {"status": "success", "events": [{"_id": "alert"}]},
            ("elastic", "run_catalog_query"): {"status": "success", "events": self.events},
            ("mythic", "list_callbacks"): {"status": "success", "callbacks": []},
        }
        return responses[(server, tool)]


async def test_model_receives_summarized_context_while_run_state_keeps_raw_events() -> None:
    events = _events(25, message="certificate transparency correlation")
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        engagement = EngagementRecord(name="authorized-operation")
        db.add(engagement)
        db.commit()
        model = RecordingModel()
        orchestrator = DurableSkillOrchestrator(
            db,
            SkillRegistry(Path(__file__).parents[1] / "skills"),
            BulkEvidenceGateway(events),
            model,
            context_max_raw_items=10,
        )
        result = await orchestrator.run(
            "nightwatcher",
            "investigate-elastic-incident",
            engagement.id,
            "Investigate synthetic incident",
            {
                "engagement_id": engagement.id,
                "engagement_name": engagement.name,
                "external_alert_id": "alert-1",
                "redirector_id": "edge-1",
                "required_sources": ["logs-edge"],
            },
        )
        assert result.status == "completed"
        summary = model.contexts["evidence_verifier"]["elastic_evidence"]["events"]
        assert summary["total_count"] == 25
        assert summary["sample"] == events[:3]
        assert "correlation" in summary["keyword_hits"]
        assert result.state["elastic_evidence"]["events"] == events
        steps = list(db.scalars(select(AgentStepRecord).where(AgentStepRecord.run_id == result.run_id)))
        evidence_step = next(step for step in steps if step.node == "elastic_evidence")
        assert evidence_step.output_json["events"] == events
