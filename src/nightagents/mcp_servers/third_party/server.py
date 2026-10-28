from nightagents.contracts.common import Capability
from nightagents.mcp_servers.compat import Context, MCPServer
from nightagents.mcp_servers.runtime import run_server
from nightagents.security.authorization import CapabilityPolicy
from nightagents.security.mcp_auth_config import mcp_auth_kwargs
from nightagents.security.mcp_identity import principal_from_context
from nightagents.third_party.collector import ThirdPartyCollector


def build_server(collector=None) -> MCPServer:
    server = MCPServer(
        "nightagents-third-party",
        instructions="Read-only, provenance-preserving external evidence.",
        **mcp_auth_kwargs(),
    )
    source_collector = collector or ThirdPartyCollector()
    policy = CapabilityPolicy()

    @server.tool()
    async def collect_json(source_id: str, url: str, engagement_id: str, ctx: Context):
        """Collect one allowlisted HTTPS JSON observation; source registration is enforced by the caller."""
        policy.require(principal_from_context(ctx), Capability.READ)
        return await source_collector.collect_json(source_id, url, engagement_id)

    return server


def main() -> None:
    run_server(build_server())
