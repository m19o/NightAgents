
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from nightagents.db.base import Base
from nightagents.db.models import DisagreementRecord, EngagementRecord
from nightagents.db.session import build_engine
from nightagents.orchestration.runtime import DurableSkillOrchestrator
from nightagents.skills.registry import SkillRegistry


class DisagreeingModel:
    model_name = "disagreeing"
    model_version = "1.0"

    def __init__(self):
        self._calls = 0

    async def invoke(self, role: str, context: dict) -> dict:
        self._calls += 1
        if role == "verifier":
            return {"supported": True, "unknowns": []}
        if role == "critic":
            return {"supported": False, "unknowns": ["high confidence"]}
        return {"supported": True}


class StubGateway:
    async def call_tool(self, server: str, tool: str, arguments: dict):
        return {"status": "ok"}


@pytest.mark.asyncio
async def test_disagreement_detected_between_model_steps(tmp_path) -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    engagement = EngagementRecord(name="adjudication-engagement")
    db.add(engagement)
    db.commit()
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "test-agent").mkdir()
    (skills / "test-agent" / "test-skill").mkdir()
    (skills / "test-agent" / "test-skill" / "manifest.json").write_text(
        """
        {
          "skill_id": "test-skill",
          "version": 1,
          "agent": "test-agent",
          "tools": ["elastic.echo"],
          "phases": [
            {"node": "tool_phase", "kind": "tool", "tool": "elastic.echo", "arguments": {"value": "x"}},
            {"node": "verify", "kind": "model", "model_role": "verifier", "output_schema": "evidence-verifier-v1"},
            {"node": "critique", "kind": "model", "model_role": "critic", "output_schema": "evidence-verifier-v1"}
          ]
        }
        """
    )
    (skills / "schemas").mkdir()
    (skills / "schemas" / "evidence-verifier-v1.json").write_text(
        '{"type": "object", "properties": {"supported": {"type": "boolean"}}}'
    )
    (skills / "test-agent" / "test-skill" / "SKILL.md").write_text("# Test skill")
    orchestrator = DurableSkillOrchestrator(db, SkillRegistry(skills), StubGateway(), DisagreeingModel())
    result = await orchestrator.run(
        "test-agent", "test-skill", engagement.id, "test disagreement", {}
    )
    assert result.status == "completed"
    disagreements = list(db.scalars(select(DisagreementRecord).where(DisagreementRecord.run_id == result.run_id)))
    assert len(disagreements) == 1
    assert "verifier" in disagreements[0].roles_json
    assert "critic" in disagreements[0].roles_json


class TiedModel:
    model_name = "tied"
    model_version = "1.0"

    async def invoke(self, role: str, context: dict) -> dict:
        if role == "verifier":
            return {"supported": True, "unknowns": []}
        if role == "critic":
            return {"supported": False, "unknowns": ["high confidence"]}
        return {"resolution": "escalate_to_operator", "confidence": 0.0}


@pytest.mark.asyncio
async def test_adjudication_escalates_to_operator_review(tmp_path) -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    engagement = EngagementRecord(name="adjudication-escalation")
    db.add(engagement)
    db.commit()
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "test-agent").mkdir()
    (skills / "test-agent" / "test-skill").mkdir()
    (skills / "test-agent" / "test-skill" / "manifest.json").write_text(
        """
        {
          "skill_id": "test-skill",
          "version": 1,
          "agent": "test-agent",
          "tools": ["elastic.echo"],
          "phases": [
            {"node": "tool_phase", "kind": "tool", "tool": "elastic.echo", "arguments": {"value": "x"}},
            {"node": "verify", "kind": "model", "model_role": "verifier", "output_schema": "evidence-verifier-v1"},
            {"node": "critique", "kind": "model", "model_role": "critic", "output_schema": "evidence-verifier-v1"}
          ]
        }
        """
    )
    (skills / "schemas").mkdir()
    (skills / "schemas" / "evidence-verifier-v1.json").write_text(
        '{"type": "object", "properties": {"supported": {"type": "boolean"}}}'
    )
    (skills / "test-agent" / "test-skill" / "SKILL.md").write_text("# Test skill")
    orchestrator = DurableSkillOrchestrator(db, SkillRegistry(skills), StubGateway(), TiedModel())
    result = await orchestrator.run(
        "test-agent", "test-skill", engagement.id, "test escalation", {}
    )
    disagreement = db.scalar(select(DisagreementRecord).where(DisagreementRecord.run_id == result.run_id))
    assert disagreement is not None
    adjudication = await orchestrator.adjudicate_disagreement(disagreement.id)
    assert adjudication["status"] == "escalated"
    assert adjudication["resolution"] == "escalate_to_operator"
