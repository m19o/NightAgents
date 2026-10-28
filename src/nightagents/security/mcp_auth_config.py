from mcp.server.auth.settings import AuthSettings

from nightagents.config import Settings, get_settings
from nightagents.security.mcp_auth import HMACTokenVerifier


def mcp_auth_kwargs(settings: Settings | None = None) -> dict:
    current = settings or get_settings()
    if current.mcp_tokens_json in {"", "{}"}:
        return {}
    return {
        "token_verifier": HMACTokenVerifier(current.mcp_tokens_json),
        "auth": AuthSettings(
            issuer_url="https://identity.nightagents.invalid",
            resource_server_url="https://nightagents.invalid",
            required_scopes=["nightagents"],
        ),
    }

