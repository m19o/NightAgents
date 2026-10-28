import json
from datetime import UTC, datetime, timedelta

import httpx

from nightagents.adapters.elastic import ElasticAdapter
from nightagents.adapters.mythic import MythicAdapter
from nightagents.contracts.common import ObservationStatus, VerificationStatus


async def test_mythic_failure_is_unavailable_not_empty_success() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    adapter = MythicAdapter("https://mythic", "token", transport=httpx.MockTransport(handler))
    result = await adapter.callback_inventory("operation-1")
    await adapter.close()
    assert result.status is ObservationStatus.UNAVAILABLE
    assert result.callbacks == []


async def test_mythic_reads_callbacks_but_does_not_claim_route_attribution() -> None:
    payload = {
        "data": {
            "callback": [
                {
                    "agent_callback_id": "cb-1",
                    "operation": {"name": "eng-1"},
                    "payload": {
                        "payloadtype": {"name": "poseidon"},
                        "payloadc2profiles": [{"c2profile": {"name": "http"}}],
                    },
                    "last_seen": "2026-07-17T10:00:00Z",
                    "active": True,
                    "dead": False,
                    "sleep_info": "",
                }
            ]
        }
    }

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    adapter = MythicAdapter("https://mythic", "token", transport=httpx.MockTransport(handler))
    inventory = await adapter.callback_inventory("eng-1")
    verification = await adapter.verify_route("eng-1", "route-a", ["cb-1"])
    await adapter.close()
    assert inventory.status is ObservationStatus.SUCCESS
    assert inventory.callbacks[0].route_id is None
    assert verification.status is VerificationStatus.UNKNOWN


async def test_elastic_catalog_query_is_bounded_and_engagement_scoped() -> None:
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"hits": {"total": {"value": 1}, "hits": [{"_id": "1", "_source": {"x": 1}}]}})

    adapter = ElasticAdapter("https://elastic", "key", transport=httpx.MockTransport(handler))
    result = await adapter.run_catalog_query(
        "incident-evidence-v1",
        {"engagement_id": "eng-1", "redirector_id": "r-1", "lookback_seconds": 60, "size": 10},
    )
    await adapter.close()
    filters = captured["body"]["query"]["bool"]["filter"]
    assert result["status"] == "success"
    assert {"term": {"nightagents.engagement_id": "eng-1"}} in filters
    assert {"term": {"nightagents.redirector_id": "r-1"}} in filters


async def test_elastic_source_health_requires_recent_events() -> None:
    recent = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/_cluster/health":
            return httpx.Response(200, json={"status": "green"})
        if request.url.path.startswith("/_data_stream/"):
            return httpx.Response(200, json={"data_streams": [{"name": "logs-edge"}]})
        return httpx.Response(
            200,
            json={"hits": {"hits": [{"_source": {"@timestamp": recent}}]}},
        )

    adapter = ElasticAdapter("https://elastic", "key", transport=httpx.MockTransport(handler))
    result = await adapter.source_health(["logs-edge"])
    await adapter.close()
    assert result["logs-edge"]["status"] == "healthy"


async def test_elastic_source_health_marks_empty_stream_stale() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/_cluster/health":
            return httpx.Response(200, json={"status": "green"})
        if request.url.path.startswith("/_data_stream/"):
            return httpx.Response(200, json={"data_streams": [{"name": "logs-edge"}]})
        return httpx.Response(200, json={"hits": {"hits": []}})

    adapter = ElasticAdapter("https://elastic", "key", transport=httpx.MockTransport(handler))
    result = await adapter.source_health(["logs-edge"])
    await adapter.close()
    assert result["logs-edge"]["status"] == "stale"
