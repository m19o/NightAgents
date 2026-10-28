"""Third-party intelligence monitoring.

Watches configured external feeds (VirusTotal, Shodan, certificate-transparency
logs, passive DNS, abuse desks) for observations about registered redirector
assets and opens incidents when one appears. This is how the platform detects
blue-team fingerprinting that never touches our own telemetry: the exposure
happens out there, in third-party datasets.

Feeds are fetched through the Third-Party MCP collector path (HTTPS-only,
provenance-preserving). Incidents are idempotent per (feed, indicator) while
active; resolved incidents may re-alert. In the lab the feed is a simulated
intel service; production points the same config at real APIs.
"""

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from nightagents.db.models import AssetRecord, EngagementRecord, IncidentRecord
from nightagents.db.repositories import AuditRepository
from nightagents.operational import OperationalMetrics
from nightagents.third_party.collector import ThirdPartyCollector

ACTIVE_STATUSES = ("received", "investigating")


class ThirdPartyMonitorService:
    def __init__(
        self,
        db: Session,
        feeds: dict[str, dict],
        collector: ThirdPartyCollector | None = None,
        verify_tls: bool = True,
        notifier=None,
    ):
        self.db = db
        self.feeds = feeds
        self.collector = collector or ThirdPartyCollector()
        self.verify_tls = verify_tls
        self.notifier = notifier
        self.audit = AuditRepository(db)
        self.metrics = OperationalMetrics(db)

    async def run_once(self) -> dict[str, int]:
        engagements = list(
            self.db.scalars(select(EngagementRecord).where(EngagementRecord.status == "active"))
        )
        opened = 0
        skipped = 0
        failures = 0
        for engagement in engagements:
            indicators = self._engagement_indicators(engagement.id)
            if not indicators:
                continue
            for feed_name, feed in self.feeds.items():
                try:
                    opened += await self._check_feed(engagement, feed_name, feed, indicators)
                except Exception:
                    self.db.rollback()
                    failures += 1
                    self.metrics.record("third_party_monitor.failures", 1.0, {"feed": feed_name})
                    self.db.commit()
        self.metrics.record("third_party_monitor.incidents_opened", opened)
        self.db.commit()
        return {"opened": opened, "skipped": skipped, "failures": failures}

    def _engagement_indicators(self, engagement_id: str) -> set[str]:
        assets = self.db.scalars(
            select(AssetRecord).where(
                AssetRecord.engagement_id == engagement_id,
                AssetRecord.asset_type.in_(["domain", "provider_resource", "redirector", "logical_route"]),
            )
        )
        return {str(asset.external_id).lower() for asset in assets if asset.external_id}

    async def _check_feed(
        self,
        engagement: EngagementRecord,
        feed_name: str,
        feed: dict,
        indicators: set[str],
    ) -> int:
        result = await self.collector.collect_json(
            feed_name, feed["url"], engagement.id, verify_tls=self.verify_tls
        )
        if result.get("status") != "success":
            self.metrics.record("third_party_monitor.feed_unavailable", 1.0, {"feed": feed_name})
            self.db.commit()
            return 0
        observations = (result.get("data") or {}).get("observations", [])
        opened = 0
        for observation in observations:
            indicator = str(observation.get("indicator", "")).lower()
            if not indicator or indicator not in indicators:
                continue
            opened += await self._open_incident(engagement, feed_name, feed, observation)
        return opened

    async def _open_incident(
        self,
        engagement: EngagementRecord,
        feed_name: str,
        feed: dict,
        observation: dict,
    ) -> int:
        base_id = f"intel-{self._observation_hash(engagement.id, feed_name, observation)}"
        active = self.db.scalar(
            select(IncidentRecord).where(
                IncidentRecord.engagement_id == engagement.id,
                IncidentRecord.external_id.like(f"{base_id}%"),
                IncidentRecord.status.in_(ACTIVE_STATUSES),
            )
        )
        if active:
            return 0
        prior = list(
            self.db.scalars(
                select(IncidentRecord).where(
                    IncidentRecord.engagement_id == engagement.id,
                    IncidentRecord.external_id.like(f"{base_id}%"),
                )
            )
        )
        external_id = f"{base_id}-{len(prior) + 1}" if prior else base_id
        obs_type = observation.get("type", "observation")
        incident = IncidentRecord(
            engagement_id=engagement.id,
            external_id=external_id,
            severity=str(feed.get("severity", "medium")),
            rule_id=f"third-party-{feed_name}",
            summary=(
                f"{feed_name} reports {observation.get('indicator')} "
                f"({obs_type}: {observation.get('detail', 'no detail')})"
            ),
            evidence_refs_json=[
                f"thirdparty:{feed_name}",
                f"thirdparty:{feed_name}#{json.dumps(observation, sort_keys=True)[:400]}",
            ],
            source_health_json={feed_name: {"status": "healthy", "via": "third-party-mcp"}},
            occurred_at=datetime.now(UTC),
        )
        self.db.add(incident)
        self.db.flush()
        self.audit.append(
            "third-party-monitor",
            "incident.intel_opened",
            "incident",
            incident.id,
            {"external_id": external_id, "feed": feed_name, "observation": observation},
            engagement.id,
        )
        self.db.commit()
        if self.notifier is not None:
            await self.notifier.notify(
                f":satellite: Third-party intel hit: *{feed_name}* flagged "
                f"`{observation.get('indicator')}` ({obs_type})",
                {
                    "severity": feed.get("severity", "medium"),
                    "indicator": observation.get("indicator"),
                    "observation": obs_type,
                    "incident": incident.id,
                },
                engagement.id,
            )
        return 1

    @staticmethod
    def _observation_hash(engagement_id: str, feed_name: str, observation: dict) -> str:
        material = json.dumps(
            {
                "engagement_id": engagement_id,
                "feed": feed_name,
                "indicator": observation.get("indicator"),
                "type": observation.get("type"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(material.encode()).hexdigest()[:16]
