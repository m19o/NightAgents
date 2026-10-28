import hashlib
import hmac
import json
import time

from mcp.server.auth.provider import AccessToken, TokenVerifier


class HMACTokenVerifier(TokenVerifier):
    def __init__(self, tokens_json: str):
        raw = json.loads(tokens_json or "{}")
        if not isinstance(raw, dict):
            raise ValueError("MCP_TOKENS_JSON must be an object")
        self._tokens = {str(token): str(identity) for token, identity in raw.items()}

    async def verify_token(self, token: str) -> AccessToken | None:
        identity = self._tokens.get(token)
        if identity is None:
            return None
        return AccessToken(token=token, client_id=identity, scopes=["nightagents"], expires_at=int(time.time()) + 300)


def token_fingerprint(token: str) -> str:
    return hmac.new(b"nightagents-token-log", token.encode(), hashlib.sha256).hexdigest()[:16]

