from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from nightagents.agents.agentic_nightwatcher import AgenticNightWatcher
from nightagents.config import Settings
from nightagents.db.base import Base
from nightagents.db.models import EngagementRecord, IncidentRecord
from nightagents.db.session import build_engine


class HuntGateway:
    def __init__(self, healthy: bool = True):
        self.healthy = healthy
        self.calls: list[tuple[str, str, dict]] = []

    async def call_tool(self, server: str, tool: str, arguments: dict):
        self.calls.append((server, tool, arguments))
        status = "healthy" if self.healthy else "unhealthy"
        responses = {
            ("elastic", "get_source_health"): {"logs-nginx.access-*": {"status": status}},
            ("elastic", "list_query_catalog"): [{"query_id": "beacon-fingerprint-v1"}],
            ("elastic", "run_hunt_queries"): {
                "status": "success",
                "results": [
                    {
                        "query_id": "beacon-fingerprint-v1",
                        "status": "success",
                        "result": {"status": "success", "events": [{"_id": "event-1"}, {"_id": "event-2"}]},
                    }
                ],
            },
        }
        return responses[(server, tool)]


class HuntModel:
    """Deterministic stand-in for the hunting agent's model roles."""

    model_name = "hunt-stub"

    def __init__(self, evidence_ids: list[str] | None = None, garbage: bool = False):
        self.invocations: list[str] = []
        self.evidence_ids = ["event-1", "event-2"] if evidence_ids is None else evidence_ids
        self.garbage = garbage

    async def invoke(self, role: str, context: dict) -> dict:
        self.invocations.append(role)
        if role == "skill_selector":
            return {"skill_id": context["eligible_skills"][0]["skill_id"], "reason": "allowlisted"}
        if self.garbage:
            return {"not_a_valid_output": True}
        if "hunt_results" in context:
            return {
                "findings": [
                    {
                        "title": "scanner fingerprint on redirector-pass",
                        "severity": "high",
                        "confidence": 0.8,
                        "evidence_ids": self.evidence_ids,
                        "reasoning": "repeated shallow fetches with tooling user agents",
                    }
                ],
                "notes": "correlated across two events",
            }
        return {
            "queries": [
                {
                    "query_id": "beacon-fingerprint-v1",
                    "parameters": {"lookback_seconds": 900},
                    "rationale": "playbook: rhythm anomalies",
                }
            ],
            "notes": "starting with rhythm",
        }


def _setup(model, healthy: bool = True):
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    engagement = EngagementRecord(name="hunt-ops", status="active")
    db.add(engagement)
    db.commit()
    settings = Settings(
        detection_source="logs-nginx.access-*",
        handoff_signing_secret="test-handoff-signing-secret-32-chars!!",
        approval_signing_secret="test-approval-signing-secret-32chars!",
    )
    gateway = HuntGateway(healthy=healthy)
    watcher = AgenticNightWatcher(db, gateway, model, settings, skills_root=Path("skills"))
    return db, engagement, gateway, watcher


async def test_hunt_opens_incident_with_evidence() -> None:
    db, engagement, gateway, watcher = _setup(HuntModel())
    result = await watcher.hunt(engagement.id)
    assert result["opened"] == 1
    assert result["findings"] == 1
    incident = db.scalar(select(IncidentRecord).where(IncidentRecord.engagement_id == engagement.id))
    assert incident is not None
    assert incident.external_id.startswith("hunt-")
    assert incident.rule_id == "agentic-hunt"
    assert incident.status == "received"
    assert len(incident.evidence_refs_json) == 2
    assert any(tool == "run_hunt_queries" for _, tool, _ in gateway.calls)


async def test_hunt_is_idempotent_across_runs() -> None:
    db, engagement, _, watcher = _setup(HuntModel())
    first = await watcher.hunt(engagement.id)
    second = await watcher.hunt(engagement.id)
    assert first["opened"] == 1
    assert second["opened"] == 0
    assert second["duplicates"] == 1
    incidents = list(db.scalars(select(IncidentRecord).where(IncidentRecord.engagement_id == engagement.id)))
    assert len(incidents) == 1


async def test_hunt_skips_unhealthy_source_without_spending_tokens() -> None:
    model = HuntModel()
    db, engagement, gateway, watcher = _setup(model, healthy=False)
    result = await watcher.hunt(engagement.id)
    assert result["skipped"] == "unhealthy_source"
    assert result["opened"] == 0
    assert model.invocations == []  # absence of evidence is never a negative outcome
    assert db.scalar(select(IncidentRecord).where(IncidentRecord.engagement_id == engagement.id)) is None


async def test_hunt_discards_findings_without_evidence() -> None:
    db, engagement, _, watcher = _setup(HuntModel(evidence_ids=[]))
    result = await watcher.hunt(engagement.id)
    assert result["opened"] == 0
    assert result["discarded"] == 1
    assert db.scalar(select(IncidentRecord).where(IncidentRecord.engagement_id == engagement.id)) is None


async def test_hunt_rejects_schema_violating_model_output() -> None:
    db, engagement, _, watcher = _setup(HuntModel(garbage=True))
    with pytest.raises((ValueError, RuntimeError)):
        await watcher.hunt(engagement.id)
    assert db.scalar(select(IncidentRecord).where(IncidentRecord.engagement_id == engagement.id)) is None
