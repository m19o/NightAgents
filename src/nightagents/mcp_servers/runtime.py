from nightagents.config import get_settings
from nightagents.mcp_servers.compat import run_transport


def run_server(server) -> None:
    settings = get_settings()
    run_transport(server, settings.mcp_transport)
