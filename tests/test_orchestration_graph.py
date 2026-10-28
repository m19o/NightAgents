import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from nightagents.db.base import Base
from nightagents.db.models import AgentRunRecord, AgentStepRecord, EngagementRecord
from nightagents.db.session import build_engine
from nightagents.models.local import LocalStructuredModel
from nightagents.orchestration.runtime import DurableSkillOrchestrator
from nightagents.skills.registry import SkillRegistry


class SequenceGateway:
    def __init__(self):
        self.calls = 0

    async def call_tool(self, server: str, tool: str, arguments: dict):
        self.calls += 1
        return {"retry": self.calls == 1}


async def test_conditional_graph_repeats_within_iteration_budget(tmp_path) -> None:
    skill = tmp_path / "test-agent" / "bounded-loop"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("Perform a bounded deterministic check.")
    (skill / "manifest.json").write_text(
        json.dumps(
            {
                "skill_id": "bounded-loop",
                "version": 1,
                "agent": "test-agent",
                "tools": ["test.check"],
                "maximum_iterations": 3,
                "phases": [
                    {
                        "node": "check",
                        "kind": "tool",
                        "tool": "test.check",
                        "transitions": [{"path": "retry", "operator": "truthy", "target": "check"}],
                        "next_node": "$end"
                    }
                ]
            }
        )
    )
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        engagement = EngagementRecord(name="graph-test")
        db.add(engagement)
        db.commit()
        gateway = SequenceGateway()
        result = await DurableSkillOrchestrator(
            db, SkillRegistry(tmp_path), gateway, LocalStructuredModel()
        ).run("test-agent", "bounded-loop", engagement.id, "bounded loop", {})
        steps = list(db.scalars(select(AgentStepRecord).where(AgentStepRecord.run_id == result.run_id)))
        assert result.status == "completed"
        assert gateway.calls == 2
        assert [step.sequence for step in steps] == [1, 2]


async def test_schema_violation_fails_run_and_persists_status(tmp_path) -> None:
    skill = tmp_path / "test-agent" / "bad-output"
    schemas = tmp_path / "schemas"
    skill.mkdir(parents=True)
    schemas.mkdir()
    (skill / "SKILL.md").write_text("Return structured output.")
    (schemas / "required-v1.json").write_text(
        json.dumps({"type": "object", "required": ["required"], "properties": {"required": {"type": "boolean"}}})
    )
    (skill / "manifest.json").write_text(
        json.dumps(
            {
                "skill_id": "bad-output",
                "version": 1,
                "agent": "test-agent",
                "phases": [
                    {"node": "model", "kind": "model", "model_role": "skill_selector", "output_schema": "required-v1"}
                ]
            }
        )
    )
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        engagement = EngagementRecord(name="schema-test")
        db.add(engagement)
        db.commit()
        with pytest.raises(ValueError, match="violates required-v1"):
            await DurableSkillOrchestrator(
                db, SkillRegistry(tmp_path), LocalStructuredModel(), LocalStructuredModel()
            ).run("test-agent", "bad-output", engagement.id, "bad schema", {"eligible_skills": [{"skill_id": "x"}]})
        run = db.scalar(select(AgentRunRecord))
        assert run.status == "failed"
