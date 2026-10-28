from pathlib import Path

import pytest

from nightagents.skills.loader import SkillLoader, SkillNotFoundError


def test_loads_versioned_skill() -> None:
    loader = SkillLoader(Path(__file__).parents[1] / "skills")
    content = loader.load("nightwatcher", "investigate-elastic-incident")
    assert "Version: 1" in content
    assert "elastic.run_catalog_query" in content


def test_rejects_missing_or_escaping_skill() -> None:
    loader = SkillLoader(Path(__file__).parents[1] / "skills")
    with pytest.raises(SkillNotFoundError):
        loader.load("nightwatcher", "../../README")

