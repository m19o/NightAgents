from nightagents.adapters.infrastructure_factory import build_infrastructure
from nightagents.config import get_settings
from nightagents.contracts.infrastructure import InfrastructurePlan
from nightagents.contracts.mythic import ExecutionGrant
from nightagents.db.session import SessionLocal
from nightagents.mcp_servers.compat import Context, MCPServer
from nightagents.mcp_servers.infrastructure.service import InfrastructureMCPService
from nightagents.mcp_servers.runtime import run_server
from nightagents.security.mcp_auth_config import mcp_auth_kwargs
from nightagents.security.mcp_identity import principal_from_context


def build_server(adapter=None) -> MCPServer:
    server = MCPServer(
        "nightagents-infrastructure",
        instructions="Provider and logical-route capability server.",
        **mcp_auth_kwargs(),
    )
    settings = get_settings()
    db = SessionLocal() if adapter is None else None
    infrastructure = adapter if adapter is not None else build_infrastructure(db, settings)
    service = InfrastructureMCPService(infrastructure, settings.execution_grant_secret)

    @server.tool()
    async def route_health(engagement_id: str, redirector_id: str, ctx: Context):
        """Verify replacement redirector route health."""
        return await service.route_health(principal_from_context(ctx), engagement_id, redirector_id)

    @server.tool()
    async def plan_route_switch(
        engagement_id: str,
        logical_route_id: str,
        old_redirector_id: str,
        new_redirector_id: str,
        ctx: Context,
    ):
        """Create a non-mutating logical-route switch plan."""
        return await service.plan_route_switch(
            principal_from_context(ctx), engagement_id, logical_route_id, old_redirector_id, new_redirector_id
        )

    @server.tool()
    async def execute_route_switch(
        plan: InfrastructurePlan,
        grant: ExecutionGrant,
        idempotency_key: str,
        ctx: Context,
    ):
        """Execute an already Safety-Kernel-authorized route switch."""
        return await service.execute_route_switch(principal_from_context(ctx), plan, grant, idempotency_key)

    @server.tool()
    async def verify_callback_route(engagement_id: str, route_id: str, callback_ids: list[str], ctx: Context):
        """Verify callback continuity on a route from independent edge evidence."""
        return await service.verify_callback_route(principal_from_context(ctx), engagement_id, route_id, callback_ids)

    @server.tool()
    async def rollback_route_switch(
        plan: InfrastructurePlan,
        grant: ExecutionGrant,
        idempotency_key: str,
        ctx: Context,
    ):
        """Restore the prior logical route."""
        return await service.rollback_route_switch(principal_from_context(ctx), plan, grant, idempotency_key)

    return server


def main() -> None:
    run_server(build_server())
