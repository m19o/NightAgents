from nightagents.mcp_servers.compat import Context
from nightagents.security.authorization import PRINCIPALS, AuthorizationError, Principal


def _access_token_client_id(ctx: Context) -> str | None:
    # Released FastMCP populates an auth contextvar for bearer-authenticated calls.
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
    except ImportError:
        get_access_token = None
    if get_access_token is not None:
        token = get_access_token()
        if token is not None:
            return token.client_id
    getter = getattr(ctx, "get_access_token", None)
    if callable(getter):
        token = getter()
        if token is not None:
            return token.client_id
    return None


def principal_from_context(ctx: Context) -> Principal:
    identity = _access_token_client_id(ctx)
    if identity is None:
        # Snapshot API: the authenticated identity is exposed as client_id.
        identity = getattr(ctx, "client_id", None)
    if identity not in PRINCIPALS:
        raise AuthorizationError("authenticated MCP identity is required")
    return PRINCIPALS[identity]
