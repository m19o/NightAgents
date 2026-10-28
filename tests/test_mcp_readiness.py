import asyncio

import httpx

from nightagents.mcp_client import RemoteMCPGateway
from nightagents.mcp_servers.compat import MCPServer, streamable_app


async def test_gateway_validates_required_tools() -> None:
    server = MCPServer("readiness-test")

    @server.tool()
    def echo(value: str) -> dict:
        return {"echo": value}

    app = streamable_app(server, json_response=True, stateless_http=True, host="test")
    gateway = RemoteMCPGateway(
        {"test": {"url": "http://test/mcp"}},
        transport=httpx.ASGITransport(app=app),
        rate_per_second=100,
    )
    async with app.router.lifespan_context(app):
        result = await gateway.validate_required_tools(["test.echo", "test.missing"])
        assert result["valid"] is False
        assert "test.missing" in result["missing"]
        assert "test.echo" not in result["missing"]
        result_ok = await gateway.validate_required_tools(["test.echo"])
        assert result_ok["valid"] is True
        health = gateway.health_snapshot()
        assert "test" in health


async def test_gateway_rate_limiting_bucket() -> None:
    from nightagents.mcp_client import _TokenBucket

    bucket = _TokenBucket(rate_per_second=10)
    await bucket.acquire()
    assert bucket._tokens <= 10
    before = asyncio.get_event_loop().time()
    bucket._tokens = 0
    await bucket.acquire()
    elapsed = asyncio.get_event_loop().time() - before
    assert elapsed >= 0.08


async def test_gateway_reconnects_on_stale_client() -> None:
    server = MCPServer("reconnect-test")

    @server.tool()
    def echo(value: str) -> dict:
        return {"echo": value}

    app = streamable_app(server, json_response=True, stateless_http=True, host="test")
    gateway = RemoteMCPGateway(
        {"test": {"url": "http://test/mcp"}},
        transport=httpx.ASGITransport(app=app),
        client_ttl_seconds=0.001,
        rate_per_second=100,
    )
    async with app.router.lifespan_context(app):
        result1 = await gateway.call_tool("test", "echo", {"value": "a"})
        assert result1 == {"echo": "a"}
        await asyncio.sleep(0.01)
        result2 = await gateway.call_tool("test", "echo", {"value": "b"})
        assert result2 == {"echo": "b"}


async def test_gateway_ping_and_backpressure() -> None:
    server = MCPServer("ping-test")

    @server.tool()
    def echo(value: str) -> dict:
        return {"echo": value}

    app = streamable_app(server, json_response=True, stateless_http=True, host="test")
    gateway = RemoteMCPGateway(
        {"test": {"url": "http://test/mcp"}},
        transport=httpx.ASGITransport(app=app),
        rate_per_second=100,
    )
    async with app.router.lifespan_context(app):
        assert await gateway.ping("test") is True
        health = await gateway.check_health()
        assert health["test"]["healthy"] is True
        assert "active_calls" in gateway.backpressure_metrics()["test"]
