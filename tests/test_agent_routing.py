from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from nightagents.db.base import Base
from nightagents.db.models import EngagementRecord
from nightagents.db.session import build_engine
from nightagents.models.router import RoleModelRouter
from nightagents.orchestration.selection import SkillSelector
from nightagents.skills.registry import SkillRegistry


class FixedModel:
    def __init__(self, value: dict, name: str = "fixed"):
        self.value = value
        self.model_name = name

    async def invoke(self, role: str, context: dict) -> dict:
        return self.value | {"invoked_role": role}


async def test_role_model_router_dispatches_roles_independently() -> None:
    router = RoleModelRouter(FixedModel({"source": "default"}), {"critic": FixedModel({"source": "critic"})})
    assert (await router.invoke("critic", {}))["source"] == "critic"
    assert (await router.invoke("predictor", {}))["source"] == "default"


async def test_skill_selector_rejects_selection_outside_node_allowlist() -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        engagement = EngagementRecord(name="selection-test")
        db.add(engagement)
        db.commit()
        selector = SkillSelector(
            db,
            SkillRegistry(Path(__file__).parents[1] / "skills"),
            FixedModel({"skill_id": "plan-callback-migration"}),
        )
        with pytest.raises(PermissionError, match="outside"):
            await selector.select(
                "nightwatcher",
                ["investigate-elastic-incident"],
                engagement.id,
                "test",
                {},
            )
