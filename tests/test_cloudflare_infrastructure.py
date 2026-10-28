import httpx
import pytest

from nightagents.adapters.cloudflare.client import CloudflareClient
from nightagents.adapters.cloudflare.provider import CloudflareInfrastructure
from nightagents.adapters.cloudflare.sandbox import STATE, sandbox_transport, set_scenario
from nightagents.contracts.common import ObservationStatus, VerificationStatus

REDIRECTOR_MAP = {
    "r-old": {"zone_id": "zone_sandbox", "dns_record_id": "rec_teach_1", "target": "203.0.113.10"},
    "r-new": {"zone_id": "zone_sandbox", "dns_record_id": "rec_teach_2", "target": "203.0.113.20"},
}
ROUTE_MAP = {"route-1": {"zone_id": "zone_sandbox", "dns_record_id": "rec_teach_1"}}


@pytest.fixture(autouse=True)
def reset_sandbox():
    set_scenario("healthy")
    yield
    STATE.reset()


def _provider() -> CloudflareInfrastructure:
    return CloudflareInfrastructure(
        token="sandbox-token",
        redirector_map=REDIRECTOR_MAP,
        route_map=ROUTE_MAP,
        client=CloudflareClient(token="sandbox-token", transport=sandbox_transport()),
    )


def _broken_provider() -> CloudflareInfrastructure:
    def fail(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"success": False, "errors": [{"message": "boom"}], "result": None})

    return CloudflareInfrastructure(
        token="broken",
        redirector_map=REDIRECTOR_MAP,
        route_map=ROUTE_MAP,
        client=CloudflareClient(token="broken", transport=httpx.MockTransport(fail)),
    )


async def test_route_health_healthy_zone() -> None:
    health = await _provider().route_health("eng-1", "r-old")
    assert health.status == ObservationStatus.SUCCESS
    assert health.healthy is True
    assert health.checks["total_requests"] == 5000
    assert health.checks["blocked_events"] == 4


async def test_route_health_takedown_scenario_marks_unhealthy() -> None:
    set_scenario("takedown")
    health = await _provider().route_health("eng-1", "r-old")
    assert health.status == ObservationStatus.SUCCESS
    assert health.healthy is False  # origin 5xx rate 0.64 > threshold
    assert health.checks["origin_5xx_rate"] > 0.5


async def test_route_health_unknown_redirector_is_unavailable() -> None:
    health = await _provider().route_health("eng-1", "ghost")
    assert health.status == ObservationStatus.UNAVAILABLE
    assert health.healthy is False


async def test_route_health_api_error_is_unavailable_never_raises() -> None:
    health = await _broken_provider().route_health("eng-1", "r-old")
    assert health.status == ObservationStatus.UNAVAILABLE
    assert "cloudflare API error" in (health.error or "")


async def test_plan_validates_maps() -> None:
    provider = _provider()
    with pytest.raises(ValueError, match="logical route"):
        await provider.plan_route_switch("eng-1", "ghost-route", "r-old", "r-new")
    with pytest.raises(ValueError, match="not in the cloudflare map"):
        await provider.plan_route_switch("eng-1", "route-1", "r-old", "ghost")
    plan = await provider.plan_route_switch("eng-1", "route-1", "r-old", "r-new")
    assert "203.0.113.20" in plan.steps[0]


async def test_execute_repoints_record_and_rollback_restores() -> None:
    provider = _provider()
    plan = await provider.plan_route_switch("eng-1", "route-1", "r-old", "r-new")
    result = await provider.execute_route_switch(plan, "idem-1")
    assert result.success is True
    assert result.previous_state["content"] == "203.0.113.10"
    assert STATE.dns["content"] == "203.0.113.20"
    assert "idem-1" in STATE.dns["comment"]

    rolled_back = await provider.rollback_route_switch(plan, "idem-1")
    assert rolled_back.success is True
    assert STATE.dns["content"] == "203.0.113.10"


async def test_execute_is_idempotent_when_already_switched() -> None:
    provider = _provider()
    plan = await provider.plan_route_switch("eng-1", "route-1", "r-old", "r-new")
    first = await provider.execute_route_switch(plan, "idem-2")
    second = await provider.execute_route_switch(plan, "idem-2")
    assert first.success and second.success
    assert "idempotent" in second.message
    assert STATE.dns["content"] == "203.0.113.20"


async def test_rollback_without_state_refuses_to_guess() -> None:
    provider = _provider()
    plan = await provider.plan_route_switch("eng-1", "route-1", "r-old", "r-new")
    result = await provider.rollback_route_switch(plan, "never-executed")
    assert result.success is False
    assert "refusing to guess" in result.message


async def test_execute_api_error_returns_failure_not_exception() -> None:
    provider = _broken_provider()
    plan = await provider.plan_route_switch("eng-1", "route-1", "r-old", "r-new")
    result = await provider.execute_route_switch(plan, "idem-3")
    assert result.success is False
    assert "cloudflare API error" in result.message


async def test_verify_callback_route_confirmed_after_switch() -> None:
    provider = _provider()
    plan = await provider.plan_route_switch("eng-1", "route-1", "r-old", "r-new")
    await provider.execute_route_switch(plan, "idem-4")
    verification = await provider.verify_callback_route("eng-1", "route-1", ["cb-1"])
    assert verification.status == VerificationStatus.CONFIRMED
    assert "cb-1" in verification.observed_callback_ids


async def test_verify_callback_route_unknown_without_switch() -> None:
    verification = await _provider().verify_callback_route("eng-1", "route-1", ["cb-1"])
    assert verification.status == VerificationStatus.UNKNOWN


async def test_verify_callback_route_unknown_on_api_error() -> None:
    provider = _broken_provider()
    provider._switches["idem-5"] = {
        "route_id": "route-1",
        "zone_id": "zone_sandbox",
        "record_id": "rec_teach_1",
        "previous": {"content": "203.0.113.10"},
        "switched_at": "2026-01-01T00:00:00+00:00",
    }
    verification = await provider.verify_callback_route("eng-1", "route-1", ["cb-1"])
    assert verification.status == VerificationStatus.UNKNOWN
    assert "edge telemetry unavailable" in verification.reason
