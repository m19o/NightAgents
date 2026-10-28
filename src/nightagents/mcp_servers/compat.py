"""Compatibility layer over the MCP Python SDK server APIs.

The project was developed against an SDK snapshot exposing
``mcp.server.MCPServer`` and ``mcp.server.mcpserver.Context``. Released SDK
versions expose the equivalent high-level server as
``mcp.server.fastmcp.FastMCP``. This module lets the domain servers run on
either API without changes to their tool definitions.
"""

from typing import Any

from nightagents.config import get_settings

try:  # SDK snapshot API
    from mcp.server import MCPServer as _SnapshotMCPServer
    from mcp.server.mcpserver import Context  # noqa: F401

    _SNAPSHOT_API = True
except ImportError:  # released SDK API
    from mcp.server.fastmcp import Context, FastMCP  # noqa: F401

    _SNAPSHOT_API = False


def MCPServer(name: str, instructions: str = "", **auth_kwargs: Any) -> Any:  # noqa: N802
    """Construct the high-level MCP server available in the installed SDK."""
    if _SNAPSHOT_API:
        return _SnapshotMCPServer(name, instructions=instructions, **auth_kwargs)
    import inspect

    settings = get_settings()
    kwargs: dict[str, Any] = {}
    if "token_verifier" in auth_kwargs:
        kwargs["token_verifier"] = auth_kwargs["token_verifier"]
    if "token_verifier" in kwargs and "auth" in auth_kwargs:
        # FastMCP requires auth settings whenever a token verifier is supplied.
        kwargs["auth"] = auth_kwargs["auth"]
    base: dict[str, Any] = {
        "instructions": instructions,
        "host": settings.mcp_host,
        "port": settings.mcp_port,
        "streamable_http_path": "/mcp",
    }
    # json_response moved around across released SDKs: pass it at
    # construction only when the installed FastMCP accepts it there.
    try:
        init_sig = inspect.signature(FastMCP.__init__)
        supported = "json_response" in init_sig.parameters or any(
            param.kind == inspect.Parameter.VAR_KEYWORD for param in init_sig.parameters.values()
        )
    except (TypeError, ValueError):
        supported = False
    if supported:
        base["json_response"] = True
    try:
        # Released SDKs (>=1.28) reject non-localhost Host headers by default.
        # These are internal token-authenticated services on private networks,
        # so DNS rebinding protection is disabled explicitly, not by accident.
        from mcp.server.transport_security import TransportSecuritySettings

        base["transport_security"] = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    except ImportError:
        pass
    try:
        return FastMCP(name, **base, **kwargs)
    except TypeError:
        base.pop("json_response", None)
        return FastMCP(name, **base, **kwargs)


def streamable_app(server: Any, **kwargs: Any) -> Any:
    """Build the server's streamable HTTP ASGI app across SDK generations.

    Snapshot SDKs take json_response/stateless_http/host here; released SDKs
    (FastMCP) take them at construction and expose a no-argument
    ``streamable_http_app()``. Prefer kwargs, fall back to the bare call."""
    try:
        return server.streamable_http_app(**kwargs)
    except TypeError:
        return server.streamable_http_app()


def run_transport(server: Any, transport: str) -> None:
    """Run the server on the configured transport."""
    if _SNAPSHOT_API:
        settings = get_settings()
        server.run(
            transport=transport,
            host=settings.mcp_host,
            port=settings.mcp_port,
            streamable_http_path="/mcp",
            json_response=True,
        )
    else:
        server.run(transport=transport)
