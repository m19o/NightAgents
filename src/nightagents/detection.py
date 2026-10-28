import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from nightagents.db.models import EngagementRecord, IncidentRecord
from nightagents.db.repositories import AuditRepository
from nightagents.mcp_client import MCPGateway
from nightagents.operational import OperationalMetrics

BEACON_QUERY_ID = "beacon-fingerprint-v1"
BEACON_SOURCE = "logs-nginx.access-*"


class DetectionService:
    """Scheduled pull-based detection: the platform opens incidents itself.

    Every run verifies the Elastic evidence source is healthy before querying -
    absence of evidence is never a negative outcome, so an unhealthy source
    skips the run entirely. Beacon fingerprints (periodic requests from one
    client IP to a small URI set with a fixed user agent) open incidents with
    a deterministic ``auto-<hash>`` external id, which keeps runs idempotent.
    """

    def __init__(
        self,
        db: Session,
        gateway: MCPGateway,
        lookback_seconds: int = 900,
        threshold: int = 4,
        max_distinct_uris: int = 2,
        source: str = BEACON_SOURCE,
        investigator=None,
    ):
        self.db = db
        self.gateway = gateway
        self.lookback_seconds = lookback_seconds
        self.threshold = threshold
        self.max_distinct_uris = max_distinct_uris
        self.source = source
        self.investigator = investigator
        self.audit = AuditRepository(db)
        self.metrics = OperationalMetrics(db)

    async def run_once(self) -> dict[str, int]:
        engagements = list(
            self.db.scalars(
                select(EngagementRecord)
                .where(EngagementRecord.status == "active")
                .order_by(EngagementRecord.id)
            )
        )
        if not engagements:
            return {"scanned": 0, "opened": 0, "duplicates": 0, "skipped": 0, "failures": 0, "investigations": 0}
        source_health = await self._check_source_health([self.source])
        if not source_health or any(state.get("status") != "healthy" for state in source_health.values()):
            self.metrics.record("detection.skipped_unhealthy_source", 1.0)
            self.db.commit()
            return {
                "scanned": 0,
                "opened": 0,
                "duplicates": 0,
                "skipped": len(engagements),
                "failures": 0,
                "investigations": 0,
            }
        opened = 0
        duplicates = 0
        failures = 0
        investigations = 0
        for engagement in engagements:
            try:
                result = await self._scan_engagement(engagement, source_health)
                opened += result["opened"]
                duplicates += result["duplicates"]
                investigations += result["investigations"]
            except Exception:
                self.db.rollback()
                failures += 1
                self.metrics.record("detection.failures", 1.0, {"engagement_id": engagement.id})
                self.db.commit()
        self.metrics.record("detection.incidents_opened", opened)
        self.db.commit()
        return {
            "scanned": len(engagements),
            "opened": opened,
            "duplicates": duplicates,
            "skipped": 0,
            "failures": failures,
            "investigations": investigations,
        }

    async def _scan_engagement(self, engagement: EngagementRecord, source_health: dict) -> dict[str, int]:
        result = await self.gateway.call_tool(
            "elastic",
            "run_catalog_query",
            {
                "query_id": BEACON_QUERY_ID,
                "parameters": {
                    "engagement_id": engagement.id,
                    "lookback_seconds": self.lookback_seconds,
                },
            },
        )
        events = result.get("events", []) if isinstance(result, dict) else []
        opened = 0
        duplicates = 0
        investigations = 0
        for fingerprint in self._fingerprints(events):
            base_id = f"auto-{self._fingerprint_hash(engagement.id, fingerprint)}"
            active = self.db.scalar(
                select(IncidentRecord).where(
                    IncidentRecord.engagement_id == engagement.id,
                    IncidentRecord.external_id.like(f"{base_id}%"),
                    IncidentRecord.status.in_(["received", "investigating"]),
                )
            )
            if active:
                duplicates += 1
                continue
            # Resolved incidents with the same fingerprint may re-alert; keep
            # the (engagement, external_id) uniqueness constraint satisfied.
            prior = list(
                self.db.scalars(
                    select(IncidentRecord).where(
                        IncidentRecord.engagement_id == engagement.id,
                        IncidentRecord.external_id.like(f"{base_id}%"),
                    )
                )
            )
            external_id = f"{base_id}-{len(prior) + 1}" if prior else base_id
            incident = IncidentRecord(
                engagement_id=engagement.id,
                external_id=external_id,
                severity="high",
                rule_id=BEACON_QUERY_ID,
                summary=(
                    f"Beaconing pattern from {fingerprint['client_ip']} to "
                    f"{len(fingerprint['uris'])} URI(s) with a fixed user agent"
                ),
                evidence_refs_json=[f"elastic:{BEACON_SOURCE}"]
                + [f"elastic:{BEACON_SOURCE}#{event_id}" for event_id in fingerprint["event_ids"]],
                source_health_json=source_health,
                occurred_at=datetime.now(UTC),
            )
            self.db.add(incident)
            self.db.flush()
            self.audit.append(
                "detection",
                "incident.auto_opened",
                "incident",
                incident.id,
                {"external_id": external_id, "fingerprint": fingerprint},
                engagement.id,
            )
            opened += 1
            if self.investigator is not None:
                try:
                    await self.investigator(incident.id)
                    investigations += 1
                except Exception:
                    # Investigation failures never block detection; the
                    # incident remains open for manual investigation.
                    self.metrics.record("detection.investigation_failures", 1.0, {"incident_id": incident.id})
                    self.db.commit()
        self.db.commit()
        return {"opened": opened, "duplicates": duplicates, "investigations": investigations}

    def _fingerprints(self, events: list[dict]) -> list[dict]:
        groups: dict[tuple[str, str], list[dict]] = {}
        for event in events:
            client_ip = (event.get("source") or {}).get("ip")
            user_agent = (event.get("user_agent") or {}).get("original")
            uri = (event.get("url") or {}).get("path")
            if not client_ip or not user_agent or not uri:
                continue
            groups.setdefault((client_ip, user_agent), []).append(event)
        findings = []
        for (client_ip, user_agent), grouped in groups.items():
            uris = sorted({(event.get("url") or {}).get("path") for event in grouped})
            if len(grouped) < self.threshold or len(uris) > self.max_distinct_uris:
                continue
            findings.append(
                {
                    "client_ip": client_ip,
                    "user_agent": user_agent,
                    "uris": uris,
                    "request_count": len(grouped),
                    "event_ids": sorted(str(event["_id"]) for event in grouped if event.get("_id")),
                }
            )
        findings.sort(key=lambda item: (item["client_ip"], item["user_agent"]))
        return findings

    async def _check_source_health(self, source_ids: list[str]) -> dict[str, dict]:
        try:
            result = await self.gateway.call_tool("elastic", "get_source_health", {"source_ids": source_ids})
        except Exception:
            return {source: {"status": "unknown"} for source in source_ids}
        return result if isinstance(result, dict) else {}

    @staticmethod
    def _fingerprint_hash(engagement_id: str, fingerprint: dict) -> str:
        material = json.dumps(
            {
                "engagement_id": engagement_id,
                "client_ip": fingerprint["client_ip"],
                "user_agent": fingerprint["user_agent"],
                "uris": fingerprint["uris"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(material.encode()).hexdigest()[:16]
