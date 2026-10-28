from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from nightagents.db.base import Base
from nightagents.db.models import AssetRecord, EngagementRecord, IncidentRecord, MetricRecord
from nightagents.db.session import build_engine
from nightagents.third_party.monitor import ThirdPartyMonitorService

FEEDS = {"virustotal": {"url": "https://feed.test/vt", "severity": "high"}}

OBSERVATION = {
    "indicator": "burned.redirector.demo",
    "type": "domain_flag",
    "detail": "12/90 engines flag domain as malicious",
}


class FakeCollector:
    def __init__(self, payload=None, status="success"):
        self.payload = payload or {"observations": [OBSERVATION]}
        self.status = status
        self.calls = []

    async def collect_json(self, source_id, url, engagement_id, verify_tls=True):
        self.calls.append((source_id, url, engagement_id, verify_tls))
        if self.status != "success":
            return {"status": "unavailable", "error": "connection reset"}
        return {"status": "success", "data": self.payload}


class FakeNotifier:
    def __init__(self):
        self.messages = []

    async def notify(self, text, fields=None, engagement_id=None):
        self.messages.append((text, fields, engagement_id))
        return True


def _setup():
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    engagement = EngagementRecord(name="intel-engagement")
    db.add(engagement)
    db.flush()
    db.add(
        AssetRecord(
            engagement_id=engagement.id,
            asset_type="domain",
            external_id="burned.redirector.demo",
            name="burned.redirector.demo",
        )
    )
    db.commit()
    return db, engagement


def _incidents(db):
    return list(db.scalars(select(IncidentRecord).order_by(IncidentRecord.received_at)))


async def test_intel_observation_opens_incident_and_notifies():
    db, engagement = _setup()
    collector = FakeCollector()
    notifier = FakeNotifier()
    monitor = ThirdPartyMonitorService(db, FEEDS, collector=collector, notifier=notifier)
    result = await monitor.run_once()
    assert result["opened"] == 1
    incident = _incidents(db)[0]
    assert incident.external_id.startswith("intel-")
    assert incident.severity == "high"
    assert incident.rule_id == "third-party-virustotal"
    assert incident.source_health_json["virustotal"]["status"] == "healthy"
    assert any("burned.redirector.demo" in ref for ref in incident.evidence_refs_json)
    assert len(notifier.messages) == 1
    assert collector.calls[0][3] is True


async def test_second_run_is_idempotent_and_resolved_realerts():
    db, engagement = _setup()
    monitor = ThirdPartyMonitorService(db, FEEDS, collector=FakeCollector())
    await monitor.run_once()
    assert (await monitor.run_once())["opened"] == 0
    _incidents(db)[0].status = "resolved"
    db.commit()
    result = await monitor.run_once()
    assert result["opened"] == 1
    assert len(_incidents(db)) == 2
    assert _incidents(db)[1].external_id.endswith("-2")


async def test_unrelated_observations_open_nothing():
    db, engagement = _setup()
    payload = {"observations": [{"indicator": "someone-else.example", "type": "domain_flag"}]}
    monitor = ThirdPartyMonitorService(db, FEEDS, collector=FakeCollector(payload=payload))
    result = await monitor.run_once()
    assert result["opened"] == 0
    assert _incidents(db) == []


async def test_unavailable_feed_is_skipped_with_metric():
    db, engagement = _setup()
    monitor = ThirdPartyMonitorService(db, FEEDS, collector=FakeCollector(status="down"))
    result = await monitor.run_once()
    assert result["opened"] == 0
    metric = db.scalar(
        select(MetricRecord).where(MetricRecord.name == "third_party_monitor.feed_unavailable")
    )
    assert metric is not None
