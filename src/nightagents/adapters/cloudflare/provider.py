"""Cloudflare `InfrastructurePort`: real edge control behind the Safety Kernel.

Models a Cloudflare-fronted redirector fleet:

- each redirector id maps to a DNS record whose *content* is that redirector's
  origin target (``NIGHTAGENTS_CLOUDFLARE_REDIRECTOR_MAP_JSON``);
- each logical route id maps to the callback hostname's DNS record
  (``NIGHTAGENTS_CLOUDFLARE_ROUTE_MAP_JSON``) - the record the Safety Kernel
  repoints from the burned redirector's target to the standby's.

Health comes from Cloudflare Analytics (edge/origin 5xx, firewall events) plus
zone status - never from touching the origin. Route switches are reversible
DNS repoints carrying the idempotency key in the record comment. Rollback
restores the previous record content. Callback continuity is verified from
edge request analytics after the switch: independent telemetry, and unknown is
never reported as success.

The real API path requires the operator's own scoped token (DNS:Edit,
Analytics:Read); it is contract-tested against the offline sandbox, not yet
credential-verified by the project.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from nightagents.adapters.cloudflare.client import CloudflareClient, CloudflareError
from nightagents.contracts.common import ObservationStatus, VerificationStatus
from nightagents.contracts.infrastructure import InfrastructurePlan, InfrastructureResult, RouteHealth
from nightagents.contracts.mythic import RouteVerification

MARKER = "nightagents"
ORIGIN_UNHEALTHY_RATE = 0.5
DEFAULT_SINKHOLE = "192.0.2.1"

_ANALYTICS_QUERY = """
query ($zone: String!, $since: Time!, $until: Time!) {
  viewer {
    zones(filter: {zoneTag: $zone}) {
      total: httpRequestsAdaptiveGroups(
        limit: 1, filter: {datetime_geq: $since, datetime_leq: $until}) { count }
      edge5xx: httpRequestsAdaptiveGroups(
        limit: 1, filter: {datetime_geq: $since, datetime_leq: $until,
          edgeResponseStatus_geq: 500}) { count }
      origin5xx: httpRequestsAdaptiveGroups(
        limit: 1, filter: {datetime_geq: $since, datetime_leq: $until,
          originResponseStatus_geq: 500}) { count }
      firewall: firewallEventsAdaptiveGroups(
        limit: 1, filter: {datetime_geq: $since, datetime_leq: $until}) { count }
    }
  }
}
"""


class CloudflareInfrastructure:
    def __init__(
        self,
        token: str,
        redirector_map: dict[str, dict],
        route_map: dict[str, dict],
        sandbox: bool = False,
        window_minutes: int = 15,
        client: CloudflareClient | None = None,
    ):
        self.redirector_map = redirector_map
        self.route_map = route_map
        self.window_minutes = window_minutes
        if client is not None:
            self._client = client
        else:
            transport = None
            if sandbox:
                from nightagents.adapters.cloudflare.sandbox import sandbox_transport

                transport = sandbox_transport()
            self._client = CloudflareClient(token=token, transport=transport)
        self._switches: dict[str, dict] = {}

    @staticmethod
    def _count(zone: dict, key: str) -> int:
        rows = zone.get(key) or []
        if not rows:
            return 0
        return int(rows[0].get("count", 0) or 0)

    async def _analytics(self, zone_id: str, since: datetime, until: datetime) -> dict | None:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        data = await self._client.graphql(
            _ANALYTICS_QUERY,
            {"zone": zone_id, "since": since.strftime(fmt), "until": until.strftime(fmt)},
        )
        zones = ((data.get("viewer") or {}).get("zones")) or []
        return zones[0] if zones else None

    # ------------------------------------------------------------------ port

    async def route_health(self, engagement_id: str, redirector_id: str) -> RouteHealth:
        cfg = self.redirector_map.get(redirector_id)
        if not cfg:
            return RouteHealth(
                route_id=redirector_id,
                status=ObservationStatus.UNAVAILABLE,
                healthy=False,
                observed_at=datetime.now(UTC),
                checks={},
                error=f"redirector {redirector_id} not in the cloudflare map",
            )
        zone_id = cfg.get("zone_id", "")
        now = datetime.now(UTC)
        try:
            zone_meta = await self._client.get_zone(zone_id)
            abuse_flagged = zone_meta.get("status", "active") != "active"
            analytics = await self._analytics(zone_id, now - timedelta(minutes=self.window_minutes), now)
            if analytics is None:
                return RouteHealth(
                    route_id=redirector_id,
                    status=ObservationStatus.SUCCESS,
                    healthy=not abuse_flagged,
                    observed_at=now,
                    checks={"zone_status": zone_meta.get("status"), "note": "no analytics rows for window"},
                )
            total = self._count(analytics, "total")
            edge5xx = self._count(analytics, "edge5xx")
            origin5xx = self._count(analytics, "origin5xx")
            blocked = self._count(analytics, "firewall")
            origin_rate = (origin5xx / total) if total else 0.0
            origin_healthy = origin_rate < ORIGIN_UNHEALTHY_RATE if total else True
            return RouteHealth(
                route_id=redirector_id,
                status=ObservationStatus.SUCCESS,
                healthy=origin_healthy and not abuse_flagged,
                observed_at=now,
                checks={
                    "zone_status": zone_meta.get("status"),
                    "total_requests": total,
                    "edge_5xx_rate": round(edge5xx / total, 4) if total else 0.0,
                    "origin_5xx_rate": round(origin_rate, 4),
                    "blocked_events": blocked,
                    "window_minutes": self.window_minutes,
                },
            )
        except CloudflareError as exc:
            return RouteHealth(
                route_id=redirector_id,
                status=ObservationStatus.UNAVAILABLE,
                healthy=False,
                observed_at=now,
                checks={"zone_id": zone_id},
                error=f"cloudflare API error: {exc}",
            )

    async def plan_route_switch(
        self,
        engagement_id: str,
        logical_route_id: str,
        old_redirector_id: str,
        new_redirector_id: str,
    ) -> InfrastructurePlan:
        route = self.route_map.get(logical_route_id)
        if route is None:
            raise ValueError(f"logical route {logical_route_id} not in the cloudflare route map")
        if new_redirector_id not in self.redirector_map:
            raise ValueError(f"redirector {new_redirector_id} not in the cloudflare map")
        target = self.redirector_map[new_redirector_id].get("target", "")
        return InfrastructurePlan(
            plan_id=str(uuid4()),
            engagement_id=engagement_id,
            logical_route_id=logical_route_id,
            old_redirector_id=old_redirector_id,
            new_redirector_id=new_redirector_id,
            preconditions=["cloudflare token valid", "route record readable", "standby target known"],
            steps=[f"repoint dns record {route.get('dns_record_id')} to {target}"],
            rollback_steps=[f"restore dns record {route.get('dns_record_id')} to its previous content"],
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )

    async def execute_route_switch(self, plan: InfrastructurePlan, idempotency_key: str) -> InfrastructureResult:
        route = self.route_map[plan.logical_route_id]
        target = self.redirector_map[plan.new_redirector_id].get("target", "")
        zone_id = route.get("zone_id", "")
        record_id = route.get("dns_record_id", "")
        try:
            record = await self._client.get_dns_record(zone_id, record_id)
            previous = {
                "content": record.get("content"),
                "proxied": record.get("proxied", True),
                "type": record.get("type", "A"),
            }
            if previous["content"] == target:
                self._switches[idempotency_key] = {
                    "route_id": plan.logical_route_id,
                    "zone_id": zone_id,
                    "record_id": record_id,
                    "previous": previous,
                    "switched_at": datetime.now(UTC).isoformat(),
                }
                return InfrastructureResult(
                    success=True,
                    plan_id=plan.plan_id,
                    idempotency_key=idempotency_key,
                    previous_state=previous,
                    current_state={"content": target, "note": "already pointed at standby - idempotent no-op"},
                    message=f"route record already at {target}; treated as idempotent success",
                )
            await self._client.patch_dns_record(
                zone_id,
                record_id,
                {"content": target, "proxied": True, "comment": f"{MARKER} {idempotency_key}"},
            )
            switched_at = datetime.now(UTC)
            self._switches[idempotency_key] = {
                "route_id": plan.logical_route_id,
                "zone_id": zone_id,
                "record_id": record_id,
                "previous": previous,
                "switched_at": switched_at.isoformat(),
            }
            return InfrastructureResult(
                success=True,
                plan_id=plan.plan_id,
                idempotency_key=idempotency_key,
                previous_state=previous,
                current_state={"content": target, "proxied": True, "switched_at": switched_at.isoformat()},
                message=f"route record {record_id} repointed {previous['content']} -> {target}",
            )
        except CloudflareError as exc:
            return InfrastructureResult(
                success=False,
                plan_id=plan.plan_id,
                idempotency_key=idempotency_key,
                message=f"cloudflare API error: {exc}",
            )

    async def rollback_route_switch(self, plan: InfrastructurePlan, idempotency_key: str) -> InfrastructureResult:
        switch = self._switches.get(idempotency_key)
        if switch is None:
            return InfrastructureResult(
                success=False,
                plan_id=plan.plan_id,
                idempotency_key=idempotency_key,
                message="no switch state recorded for this idempotency key - refusing to guess",
            )
        previous = switch["previous"]
        try:
            await self._client.patch_dns_record(
                switch["zone_id"],
                switch["record_id"],
                {"content": previous["content"], "proxied": previous.get("proxied", True), "comment": ""},
            )
            self._switches.pop(idempotency_key, None)
            return InfrastructureResult(
                success=True,
                plan_id=plan.plan_id,
                idempotency_key=idempotency_key,
                previous_state=previous,
                current_state={"content": previous["content"], "restored": True},
                message=f"route record restored to {previous['content']}",
            )
        except CloudflareError as exc:
            return InfrastructureResult(
                success=False,
                plan_id=plan.plan_id,
                idempotency_key=idempotency_key,
                message=f"cloudflare API error: {exc}",
            )

    # ------------------------------------------------------------- verification

    async def verify_callback_route(
        self, engagement_id: str, route_id: str, callback_ids: list[str]
    ) -> RouteVerification:
        """Independent edge correlation: requests still arriving at the route's
        zone after the switch. Absence of analytics is UNKNOWN, never success."""
        switch = next((item for item in self._switches.values() if item.get("route_id") == route_id), None)
        if switch is None:
            return RouteVerification(
                status=VerificationStatus.UNKNOWN,
                route_id=route_id,
                reason="no cloudflare route switch recorded for this route",
            )
        try:
            switched_at = datetime.fromisoformat(switch["switched_at"])
            analytics = await self._analytics(switch["zone_id"], switched_at, datetime.now(UTC))
        except (CloudflareError, ValueError) as exc:
            return RouteVerification(
                status=VerificationStatus.UNKNOWN,
                route_id=route_id,
                reason=f"edge telemetry unavailable: {exc}",
            )
        if analytics is None:
            return RouteVerification(
                status=VerificationStatus.UNKNOWN,
                route_id=route_id,
                missing_callback_ids=list(callback_ids),
                reason="no analytics rows for the route zone since the switch",
            )
        total = self._count(analytics, "total")
        if total >= 1:
            return RouteVerification(
                status=VerificationStatus.CONFIRMED,
                route_id=route_id,
                observed_callback_ids=list(callback_ids),
                missing_callback_ids=[],
                reason=f"{total} edge requests observed on the route zone after the switch",
            )
        return RouteVerification(
            status=VerificationStatus.UNKNOWN,
            route_id=route_id,
            missing_callback_ids=list(callback_ids),
            reason="zero edge requests on the route zone since the switch",
        )
