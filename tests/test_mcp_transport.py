import httpx
from pydantic import BaseModel

from nightagents.mcp_client import RemoteMCPGateway
from nightagents.mcp_servers.compat import MCPServer, streamable_app
from nightagents.skills.registry import SkillManifest


async def test_remote_gateway_calls_real_streamable_http_mcp_session() -> None:
    server = MCPServer("transport-test")

    @server.tool()
    def echo(value: str) -> dict:
        return {"echo": value, "transport": "mcp"}

    app = streamable_app(server, json_response=True, stateless_http=True, host="test")
    transport = httpx.ASGITransport(app=app)
    gateway = RemoteMCPGateway(
        {"test": {"url": "http://test/mcp"}},
        transport=transport,
    )
    async with app.router.lifespan_context(app):
        result = await gateway.call_tool("test", "echo", {"value": "hello"})
    assert result == {"echo": "hello", "transport": "mcp"}


async def test_remote_gateway_discovers_and_validates_manifest_contract() -> None:
    server = MCPServer("contract-test")

    class EchoInput(BaseModel):
        value: str

    @server.tool()
    def echo(value: str) -> dict:
        EchoInput(value=value)
        return {"echo": value}

    app = streamable_app(server, json_response=True, stateless_http=True, host="test")
    gateway = RemoteMCPGateway(
        {"test": {"url": "http://test/mcp"}},
        transport=httpx.ASGITransport(app=app),
    )
    manifest = SkillManifest.model_validate(
        {
            "skill_id": "contract",
            "version": 1,
            "agent": "test",
            "tools": ["test.echo"],
            "phases": [{"node": "echo", "kind": "tool", "tool": "test.echo", "arguments": {"value": "ok"}}],
        }
    )
    async with app.router.lifespan_context(app):
        await gateway.validate_manifest(manifest)
        tools = await gateway.list_tools("test")
    assert "echo" in tools
