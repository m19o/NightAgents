from datetime import UTC, datetime

import httpx

from nightagents.contracts.common import ObservationStatus, VerificationStatus
from nightagents.contracts.mythic import (
    Callback,
    CallbackInventory,
    ExecutionResult,
    RoutePlan,
    RouteVerification,
)

CALLBACK_QUERY = """
query NightAgentsCallbacks {
  callback(where: {active: {_eq: true}}) {
    agent_callback_id
    operation { name }
    payload {
      payloadtype { name }
      payloadc2profiles { c2profile { name } }
    }
    last_checkin
    active
    dead
    sleep_info
  }
}
"""


class MythicAdapter:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        verify_tls: bool = True,
        timeout_seconds: float = 10,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            verify=verify_tls,
            timeout=timeout_seconds,
            transport=transport,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def callback_inventory(self, engagement_id: str, route_id: str | None = None) -> CallbackInventory:
        observed_at = datetime.now(UTC)
        if route_id is not None:
            return CallbackInventory(
                status=ObservationStatus.PARTIAL,
                observed_at=observed_at,
                error="Mythic callback records do not prove redirector route attribution",
            )
        try:
            response = await self.client.post("/graphql/", json={"query": CALLBACK_QUERY})
            response.raise_for_status()
            payload = response.json()
            if payload.get("errors"):
                return CallbackInventory(
                    status=ObservationStatus.UNAVAILABLE,
                    observed_at=observed_at,
                    error=str(payload["errors"]),
                )
            callbacks = []
            for raw in payload.get("data", {}).get("callback", []):
                operation = (raw.get("operation") or {}).get("name", "")
                if operation != engagement_id:
                    continue
                payload_data = raw.get("payload") or {}
                profiles = payload_data.get("payloadc2profiles") or []
                profile = "unknown"
                if profiles:
                    profile = (profiles[0].get("c2profile") or {}).get("name", "unknown")
                last_seen = raw.get("last_checkin")
                callbacks.append(
                    Callback(
                        callback_id=raw.get("agent_callback_id", ""),
                        operation_id=operation,
                        payload_type=(payload_data.get("payloadtype") or {}).get("name", "unknown"),
                        c2_profile=profile,
                        route_id=None,
                        last_seen=(
                            datetime.fromisoformat(last_seen.replace("Z", "+00:00")) if last_seen else observed_at
                        ),
                        expected_checkin_seconds=None,
                        active=bool(raw.get("active", True) and not raw.get("dead", False)),
                    )
                )
            return CallbackInventory(status=ObservationStatus.SUCCESS, callbacks=callbacks, observed_at=observed_at)
        except Exception as exc:
            return CallbackInventory(status=ObservationStatus.UNAVAILABLE, observed_at=observed_at, error=str(exc))

    async def verify_route(self, engagement_id: str, route_id: str, callback_ids: list[str]) -> RouteVerification:
        return RouteVerification(
            status=VerificationStatus.UNKNOWN,
            route_id=route_id,
            missing_callback_ids=callback_ids,
            reason="route attribution requires edge correlation and is not available from Mythic alone",
        )

    async def plan_migration(
        self, engagement_id: str, from_route_id: str, to_route_id: str, callback_ids: list[str]
    ) -> RoutePlan:
        raise NotImplementedError("live migration planning requires payload/profile capability discovery")

    async def execute_plan(self, plan: RoutePlan, idempotency_key: str) -> ExecutionResult:
        raise NotImplementedError("live Mythic mutation remains disabled until callback migration is supported")
