import json

import httpx

from nightagents.adapters.lab_infrastructure import LabInfrastructureProvider
from nightagents.contracts.common import VerificationStatus

MAP = {"burned": "redirector-pass", "standby": "redirector-filter"}


def make_docker(exec_log: list, running: bool = True):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/json") and "/containers/" in path:
            return httpx.Response(
                200,
                json={
                    "State": {"Running": running},
                    "NetworkSettings": {"Networks": {"demo": {"IPAddress": "172.21.0.9"}}},
                },
            )
        if path.endswith("/exec"):
            exec_log.append(json.loads(request.content))
            return httpx.Response(201, json={"Id": "exec-1"})
        if path.endswith("/start"):
            return httpx.Response(200, text="ok")
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://docker")


def make_elastic(total: int):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hits": {"total": {"value": total}, "hits": []}})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def make_provider(exec_log=None, elastic_total=3, running=True):
    return LabInfrastructureProvider(
        victim_container="victim",
        hostname="redirector-pass.demo",
        redirector_map=MAP,
        elastic_url="http://elastic:9200",
        docker_client=make_docker(exec_log if exec_log is not None else [], running),
        elastic_client=make_elastic(elastic_total),
    )


async def test_route_health_uses_container_state():
    provider = make_provider()
    health = await provider.route_health("eng", "standby")
    assert health.healthy is True
    assert health.checks["container"] == "redirector-filter"
    unhealthy = await make_provider(running=False).route_health("eng", "standby")
    assert unhealthy.healthy is False
    unknown = await provider.route_health("eng", "not-mapped")
    assert unknown.healthy is False


async def test_execute_repoints_victim_resolver_idempotently():
    exec_log = []
    provider = make_provider(exec_log=exec_log)
    plan = await provider.plan_route_switch("eng", "route-1", "burned", "standby")
    result = await provider.execute_route_switch(plan, "exec-1:switch")
    assert result.success is True
    assert result.current_state["resolves_to"] == "172.21.0.9"
    command = exec_log[0]["Cmd"][2]
    assert 'grep -q "exec-1:switch" /etc/hosts' in command
    assert 'echo "172.21.0.9 redirector-pass.demo # nightagents-route-switch exec-1:switch"' in command


async def test_rollback_removes_marker():
    exec_log = []
    provider = make_provider(exec_log=exec_log)
    plan = await provider.plan_route_switch("eng", "route-1", "burned", "standby")
    result = await provider.rollback_route_switch(plan, "exec-1:rollback")
    assert result.success is True
    assert "sed -i '/# nightagents-route-switch/d' /etc/hosts" in exec_log[0]["Cmd"][2]


async def test_verify_confirms_from_edge_telemetry():
    provider = make_provider(elastic_total=4)
    plan = await provider.plan_route_switch("eng", "route-1", "burned", "standby")
    await provider.execute_route_switch(plan, "exec-1:switch")
    verification = await provider.verify_callback_route("eng", "standby", ["cb-1"])
    assert verification.status == VerificationStatus.CONFIRMED
    assert verification.observed_callback_ids == ["cb-1"]


async def test_verify_unknown_without_switch_or_evidence():
    provider = make_provider(elastic_total=0)
    missing = await provider.verify_callback_route("eng", "standby", ["cb-1"])
    assert missing.status == VerificationStatus.UNKNOWN
    assert "no lab route switch" in missing.reason
    plan = await provider.plan_route_switch("eng", "route-1", "burned", "standby")
    await provider.execute_route_switch(plan, "exec-1:switch")
    silent = await provider.verify_callback_route("eng", "standby", ["cb-1"])
    assert silent.status == VerificationStatus.UNKNOWN
    assert silent.missing_callback_ids == ["cb-1"]
