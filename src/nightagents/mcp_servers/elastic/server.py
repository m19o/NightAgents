from nightagents.adapters.elastic import ElasticAdapter
from nightagents.config import get_settings
from nightagents.contracts.common import Capability
from nightagents.mcp_servers.compat import Context, MCPServer
from nightagents.mcp_servers.elastic.catalog import QUERY_CATALOG
from nightagents.mcp_servers.runtime import run_server
from nightagents.security.authorization import CapabilityPolicy
from nightagents.security.mcp_auth_config import mcp_auth_kwargs
from nightagents.security.mcp_identity import principal_from_context


def build_server(adapter=None) -> MCPServer:
    server = MCPServer(
        "nightagents-elastic",
        instructions="Read-only cataloged Elastic evidence tools.",
        **mcp_auth_kwargs(),
    )
    settings = get_settings()
    elastic = adapter or ElasticAdapter(
        settings.elastic_url,
        settings.elastic_api_key,
        settings.elastic_verify_tls,
        source_max_age_seconds=settings.elastic_source_max_age_seconds,
    )

    @server.tool()
    def list_query_catalog(ctx: Context) -> list[dict]:
        """List approved evidence and correlation queries."""
        CapabilityPolicy().require(principal_from_context(ctx), Capability.READ)
        return [item.__dict__ for item in QUERY_CATALOG.values()]

    @server.tool()
    async def run_catalog_query(query_id: str, parameters: dict, ctx: Context) -> dict:
        """Run an approved bounded evidence query."""
        CapabilityPolicy().require(principal_from_context(ctx), Capability.READ)
        return await elastic.run_catalog_query(query_id, parameters)

    @server.tool()
    async def run_hunt_queries(queries: list, engagement_id: str, ctx: Context) -> dict:
        """Run up to four catalog-allowlisted queries chosen by the hunting agent."""
        CapabilityPolicy().require(principal_from_context(ctx), Capability.READ)
        return await elastic.run_hunt_queries(queries, engagement_id)

    @server.tool()
    async def get_source_health(source_ids: list[str], ctx: Context) -> dict:
        """Return source and cluster health without converting failures to healthy states."""
        CapabilityPolicy().require(principal_from_context(ctx), Capability.VERIFY)
        return await elastic.source_health(source_ids)

    @server.tool()
    async def get_incident(alert_id: str, engagement_id: str, ctx: Context) -> dict:
        """Retrieve one engagement-scoped Elastic Security incident."""
        CapabilityPolicy().require(principal_from_context(ctx), Capability.READ)
        return await elastic.get_incident(alert_id, engagement_id)

    return server


def main() -> None:
    run_server(build_server())
