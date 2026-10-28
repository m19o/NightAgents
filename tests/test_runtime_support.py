import httpx

from nightagents.skills.registry import SkillRegistry
from nightagents.third_party.collector import ThirdPartyCollector


def test_skill_manifests_are_versioned() -> None:
    registry = SkillRegistry(__import__("pathlib").Path(__file__).parents[1] / "skills")
    skills = registry.list()
    assert len(skills) == 6
    assert all(item.version == 1 for item in skills)


async def test_third_party_collector_rejects_plain_http() -> None:
    result = await ThirdPartyCollector().collect_json("source", "http://example.test", "eng")
    assert result["status"] == "unavailable"


async def test_third_party_collector_preserves_source_and_engagement() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "clear"})

    result = await ThirdPartyCollector(httpx.MockTransport(handler)).collect_json(
        "source", "https://example.test/status", "eng"
    )
    assert result["source_id"] == "source"
    assert result["engagement_id"] == "eng"
    assert result["data"]["status"] == "clear"
