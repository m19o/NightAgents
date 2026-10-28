from datetime import UTC, datetime

import httpx

from nightagents.contracts.common import ObservationStatus


class ThirdPartyCollector:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self.transport = transport

    async def collect_json(
        self,
        source_id: str,
        url: str,
        engagement_id: str,
        request_timeout: float = 10,
        verify_tls: bool = True,
    ) -> dict:
        now = datetime.now(UTC)
        try:
            parsed = httpx.URL(url)
            if parsed.scheme != "https":
                return {
                    "source_id": source_id,
                    "status": ObservationStatus.UNAVAILABLE.value,
                    "error": "HTTPS required",
                }
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=request_timeout,
                follow_redirects=False,
                verify=verify_tls,
            ) as client:
                response = await client.get(url, headers={"Accept": "application/json"})
                response.raise_for_status()
                return {
                    "source_id": source_id,
                    "engagement_id": engagement_id,
                    "status": ObservationStatus.SUCCESS.value,
                    "observed_at": now.isoformat(),
                    "data": response.json(),
                }
        except Exception as exc:
            return {
                "source_id": source_id,
                "engagement_id": engagement_id,
                "status": ObservationStatus.UNAVAILABLE.value,
                "observed_at": now.isoformat(),
                "error": str(exc),
            }
