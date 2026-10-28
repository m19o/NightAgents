from nightagents.adapters.mythic import MythicAdapter
from nightagents.config import get_settings
from nightagents.contracts.mythic import ExecutionGrant, RoutePlan
from nightagents.mcp_servers.compat import Context, MCPServer
from nightagents.mcp_servers.mythic.service import MythicMCPService
from nightagents.mcp_servers.runtime import run_server
from nightagents.security.mcp_auth_config import mcp_auth_kwargs
from nightagents.security.mcp_identity import principal_from_context


def build_server(service: MythicMCPService | None = None) -> MCPServer:
    server = MCPServer(
        "nightagents-mythic",
        instructions="One Mythic capability server. Identity scopes are enforced server-side.",
        **mcp_auth_kwargs(),
    )
    settings = get_settings()
    tools = service or MythicMCPService(
        MythicAdapter(settings.mythic_url, settings.mythic_api_key, settings.mythic_verify_tls),
        grant_secret=settings.execution_grant_secret,
    )

    @server.tool()
    async def list_callbacks(engagement_id: str, ctx: Context, route_id: str | None = None):
        """List current Mythic callbacks for an engagement and optional route."""
        return await tools.list_callbacks(principal_from_context(ctx), engagement_id, route_id)

    @server.tool()
    async def verify_callback_route(engagement_id: str, route_id: str, callback_ids: list[str], ctx: Context):
        """Verify callbacks are observed through a specific route."""
        return await tools.verify_callback_route(principal_from_context(ctx), engagement_id, route_id, callback_ids)

    @server.tool()
    async def plan_callback_migration(
        engagement_id: str,
        from_route_id: str,
        to_route_id: str,
        callback_ids: list[str],
        ctx: Context,
    ):
        """Create a non-mutating callback migration plan."""
        return await tools.plan_callback_migration(
            principal_from_context(ctx), engagement_id, from_route_id, to_route_id, callback_ids
        )

    @server.tool()
    async def apply_route_plan(
        plan: RoutePlan,
        grant: ExecutionGrant,
        idempotency_key: str,
        ctx: Context,
    ):
        """Apply exactly one approved migration plan through the Safety Kernel identity."""
        return await tools.apply_route_plan(principal_from_context(ctx), plan, grant, idempotency_key)

    return server


def main() -> None:
    run_server(build_server())
