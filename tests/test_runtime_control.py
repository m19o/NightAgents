from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from nightagents.db.base import Base
from nightagents.db.models import AgentRunRecord, EngagementRecord
from nightagents.db.session import build_engine
from nightagents.models.local import LocalStructuredModel
from nightagents.orchestration.runtime import DurableSkillOrchestrator
from nightagents.skills.registry import SkillRegistry


class SlowGateway:
    def __init__(self):
        self.calls = []

    async def call_tool(self, server: str, tool: str, arguments: dict):
        self.calls.append((server, tool, arguments))
        return {"status": "ok"}


@pytest.mark.asyncio
async def test_orchestrator_cancellation_stops_run(tmp_path) -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    engagement = EngagementRecord(name="cancel-engagement")
    db.add(engagement)
    db.commit()
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "nightwatcher").mkdir()
    (skills / "nightwatcher" / "slow-skill").mkdir()
    (skills / "nightwatcher" / "slow-skill" / "manifest.json").write_text(
        """
        {
          "skill_id": "slow-skill",
          "version": 1,
          "agent": "nightwatcher",
          "tools": ["elastic.echo"],
          "phases": [
            {"node": "one", "kind": "tool", "tool": "elastic.echo", "arguments": {"value": "1"}},
            {"node": "two", "kind": "tool", "tool": "elastic.echo", "arguments": {"value": "2"}},
            {"node": "three", "kind": "tool", "tool": "elastic.echo", "arguments": {"value": "3"}}
          ]
        }
        """
    )
    (skills / "nightwatcher" / "slow-skill" / "SKILL.md").write_text("# Slow skill")
    (skills / "schemas").mkdir()
    orchestrator = DurableSkillOrchestrator(db, SkillRegistry(skills), SlowGateway(), LocalStructuredModel())
    # Create run then cancel it before execution completes
    run = AgentRunRecord(
        engagement_id=engagement.id,
        agent="nightwatcher",
        objective="cancel test",
        status="running",
        deadline_at=datetime.now(UTC) + timedelta(seconds=10),
        budget_json={"skill_id": "slow-skill"},
    )
    db.add(run)
    db.commit()
    # Cancel immediately; orchestrator will check status on next phase
    run.status = "cancelled"
    run.cancelled_at = datetime.now(UTC)
    db.commit()
    with pytest.raises(RuntimeError, match="cancelled"):
        await orchestrator.run(
            "nightwatcher",
            "slow-skill",
            engagement.id,
            "cancel test",
            {},
            run_id=run.id,
        )
    db.refresh(run)
    assert run.status == "cancelled"


@pytest.mark.asyncio
async def test_orchestrator_pause_and_resume(tmp_path) -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    engagement = EngagementRecord(name="pause-engagement")
    db.add(engagement)
    db.commit()
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "nightwatcher").mkdir()
    (skills / "nightwatcher" / "pause-skill").mkdir()
    (skills / "nightwatcher" / "pause-skill" / "manifest.json").write_text(
        """
        {
          "skill_id": "pause-skill",
          "version": 1,
          "agent": "nightwatcher",
          "tools": ["elastic.echo"],
          "phases": [
            {"node": "one", "kind": "tool", "tool": "elastic.echo", "arguments": {"value": "1"}},
            {"node": "two", "kind": "tool", "tool": "elastic.echo", "arguments": {"value": "2"}}
          ]
        }
        """
    )
    (skills / "nightwatcher" / "pause-skill" / "SKILL.md").write_text("# Pause skill")
    (skills / "schemas").mkdir()
    orchestrator = DurableSkillOrchestrator(db, SkillRegistry(skills), SlowGateway(), LocalStructuredModel())
    run = AgentRunRecord(
        engagement_id=engagement.id,
        agent="nightwatcher",
        objective="pause test",
        status="paused",
        wait_reason="operator pause",
        deadline_at=datetime.now(UTC) + timedelta(seconds=10),
        budget_json={"skill_id": "pause-skill"},
    )
    db.add(run)
    db.commit()
    with pytest.raises(RuntimeError, match="pause"):
        await orchestrator.run(
            "nightwatcher",
            "pause-skill",
            engagement.id,
            "pause test",
            {},
            run_id=run.id,
        )
    db.refresh(run)
    assert run.status == "paused"
    run.status = "running"
    db.commit()
    result = await orchestrator.run(
        "nightwatcher",
        "pause-skill",
        engagement.id,
        "pause test",
        {},
        run_id=run.id,
    )
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_orchestrator_loop_exhaustion_hits_iteration_budget(tmp_path) -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    engagement = EngagementRecord(name="loop-engagement")
    db.add(engagement)
    db.commit()
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "nightwatcher").mkdir()
    (skills / "nightwatcher" / "loop-skill").mkdir()
    (skills / "nightwatcher" / "loop-skill" / "manifest.json").write_text(
        """
        {
          "skill_id": "loop-skill",
          "version": 1,
          "agent": "nightwatcher",
          "tools": ["elastic.echo"],
          "maximum_iterations": 2,
          "phases": [
            {"node": "loop", "kind": "tool", "tool": "elastic.echo", "arguments": {"value": "x"}, "next_node": "loop"}
          ]
        }
        """
    )
    (skills / "nightwatcher" / "loop-skill" / "SKILL.md").write_text("# Loop skill")
    (skills / "schemas").mkdir()
    orchestrator = DurableSkillOrchestrator(db, SkillRegistry(skills), SlowGateway(), LocalStructuredModel())
    with pytest.raises(RuntimeError, match="iteration budget exceeded"):
        await orchestrator.run(
            "nightwatcher",
            "loop-skill",
            engagement.id,
            "loop test",
            {},
        )
