"""Thin async client for the Cloudflare API v4 + GraphQL analytics.

Auth is a scoped API *token* (Bearer), never the Global API Key. A custom
httpx transport can be injected for the offline teaching sandbox; the same
request shaping then runs unchanged against api.cloudflare.com.

Ported from the NightCTL control plane (`app/redirector_providers/cloudflare`).
"""

import httpx


class CloudflareError(Exception):
    def __init__(self, message: str, errors: list | None = None, status: int | None = None):
        super().__init__(message)
        self.errors = errors or []
        self.status = status


class CloudflareClient:
    BASE_URL = "https://api.cloudflare.com/client/v4/"

    def __init__(self, token: str, transport: httpx.BaseTransport | None = None, timeout: float = 15.0):
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _unwrap(self, resp: httpx.Response) -> dict:
        try:
            body = resp.json()
        except Exception:
            resp.raise_for_status()
            raise CloudflareError("non-JSON response from Cloudflare", status=resp.status_code) from None
        if not body.get("success", False):
            raise CloudflareError("Cloudflare API error", errors=body.get("errors"), status=resp.status_code)
        return body

    async def verify_token(self) -> dict:
        resp = await self._client.get("user/tokens/verify")
        return self._unwrap(resp)["result"]

    async def graphql(self, query: str, variables: dict) -> dict:
        resp = await self._client.post("graphql", json={"query": query, "variables": variables})
        body = resp.json()
        if body.get("errors"):
            raise CloudflareError("Cloudflare GraphQL error", errors=body["errors"], status=resp.status_code)
        return body.get("data") or {}

    async def get_zone(self, zone_id: str) -> dict:
        resp = await self._client.get(f"zones/{zone_id}")
        return self._unwrap(resp)["result"]

    async def get_dns_record(self, zone_id: str, record_id: str) -> dict:
        resp = await self._client.get(f"zones/{zone_id}/dns_records/{record_id}")
        return self._unwrap(resp)["result"]

    async def patch_dns_record(self, zone_id: str, record_id: str, payload: dict) -> dict:
        resp = await self._client.patch(f"zones/{zone_id}/dns_records/{record_id}", json=payload)
        return self._unwrap(resp)["result"]

    async def get_zone_setting(self, zone_id: str, setting_id: str) -> dict:
        resp = await self._client.get(f"zones/{zone_id}/settings/{setting_id}")
        return self._unwrap(resp)["result"]

    async def patch_zone_setting(self, zone_id: str, setting_id: str, value) -> dict:
        resp = await self._client.patch(f"zones/{zone_id}/settings/{setting_id}", json={"value": value})
        return self._unwrap(resp)["result"]
