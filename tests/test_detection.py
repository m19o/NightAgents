from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from nightagents.config import Settings
from nightagents.db.base import Base
from nightagents.db.models import AuditEventRecord, EngagementRecord, IncidentRecord, MetricRecord
from nightagents.db.session import build_engine
from nightagents.detection import BEACON_QUERY_ID, BEACON_SOURCE, DetectionService
from nightagents.workers import scheduler as scheduler_module
from nightagents.workers.scheduler import AgentScheduler


class FakeMCPGateway:
    def __init__(self, source_health=None, query_events=None):
        self.source_health = (
            source_health if source_health is not None else {BEACON_SOURCE: {"status": "healthy", "age_seconds": 5}}
        )
        self.query_events = query_events or []
        self.calls: list[tuple[str, str]] = []

    async def call_tool(self, server: str, tool: str, arguments: dict):
        self.calls.append((server, tool))
        if server == "elastic" and tool == "get_source_health":
            return self.source_health
        if server == "elastic" and tool == "run_catalog_query":
            return {"status": "success", "events": self.query_events}
        return {}


def _beacon_events(count=5, client_ip="10.0.0.9", user_agent="curl/8.0", uris=("/index", "/api/v1")):
    return [
        {
            "_id": f"evt-{index}",
            "source": {"ip": client_ip},
            "user_agent": {"original": user_agent},
            "url": {"path": uris[index % len(uris)]},
        }
        for index in range(count)
    ]


def _setup():
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    engagement = EngagementRecord(name="detection-engagement")
    db.add(engagement)
    db.commit()
    return db, engagement


async def test_healthy_source_beacon_opens_incident():
    db, engagement = _setup()
    gateway = FakeMCPGateway(query_events=_beacon_events())
    result = await DetectionService(db, gateway).run_once()
    assert result == {"scanned": 1, "opened": 1, "duplicates": 0, "skipped": 0, "failures": 0, "investigations": 0}
    incident = db.scalar(select(IncidentRecord).where(IncidentRecord.engagement_id == engagement.id))
    assert incident is not None
    assert incident.external_id.startswith("auto-")
    assert incident.severity == "high"
    assert incident.rule_id == BEACON_QUERY_ID
    assert f"elastic:{BEACON_SOURCE}" in incident.evidence_refs_json
    assert incident.source_health_json[BEACON_SOURCE]["status"] == "healthy"
    audit = db.scalar(
        select(AuditEventRecord).where(
            AuditEventRecord.event_type == "incident.auto_opened",
            AuditEventRecord.target_id == incident.id,
        )
    )
    assert audit is not None
    metric = db.scalar(select(MetricRecord).where(MetricRecord.name == "detection.incidents_opened"))
    assert metric is not None
    assert metric.value == 1.0


async def test_second_run_opens_no_duplicate():
    db, engagement = _setup()
    gateway = FakeMCPGateway(query_events=_beacon_events())
    service = DetectionService(db, gateway)
    first = await service.run_once()
    second = await service.run_once()
    assert first["opened"] == 1
    assert second["opened"] == 0
    assert second["duplicates"] == 1
    incidents = db.scalar(
        select(func.count()).select_from(IncidentRecord).where(IncidentRecord.engagement_id == engagement.id)
    )
    assert incidents == 1


async def test_unhealthy_source_skips_detection():
    db, engagement = _setup()
    gateway = FakeMCPGateway(
        source_health={BEACON_SOURCE: {"status": "stale"}},
        query_events=_beacon_events(),
    )
    result = await DetectionService(db, gateway).run_once()
    assert result["opened"] == 0
    assert result["skipped"] == 1
    assert ("elastic", "run_catalog_query") not in gateway.calls
    incidents = db.scalar(
        select(func.count()).select_from(IncidentRecord).where(IncidentRecord.engagement_id == engagement.id)
    )
    assert incidents == 0
    metric = db.scalar(select(MetricRecord).where(MetricRecord.name == "detection.skipped_unhealthy_source"))
    assert metric is not None


async def test_below_threshold_opens_no_incident():
    db, engagement = _setup()
    gateway = FakeMCPGateway(query_events=_beacon_events(count=3))
    result = await DetectionService(db, gateway).run_once()
    assert result["opened"] == 0
    incidents = db.scalar(
        select(func.count()).select_from(IncidentRecord).where(IncidentRecord.engagement_id == engagement.id)
    )
    assert incidents == 0


async def test_scheduler_detection_failure_is_isolated(monkeypatch):
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(scheduler_module, "SessionLocal", factory)
    monkeypatch.setattr(scheduler_module, "get_settings", lambda: Settings(detection_interval_seconds=1))

    class BrokenDetection:
        def __init__(self, db, gateway):
            pass

        async def run_once(self):
            raise RuntimeError("detection blew up")

    monkeypatch.setattr(scheduler_module, "DetectionService", BrokenDetection)

    async def outbox_handler(event):
        pass

    scheduler = AgentScheduler(outbox_handler, detection_gateway=FakeMCPGateway())
    result = await scheduler.run_once()
    assert result["detection_failures"] == 1
    assert result["detection_incidents_opened"] == 0
    assert result["published"] == 0
    with factory() as db:
        metric = db.scalar(select(MetricRecord).where(MetricRecord.name == "scheduler.detection.failures"))
        assert metric is not None
        assert metric.value == 1.0


async def test_auto_investigate_runs_for_new_incidents_only():
    db, engagement = _setup()
    gateway = FakeMCPGateway(query_events=_beacon_events())
    investigated = []

    async def investigator(incident_id: str) -> None:
        investigated.append(incident_id)

    service = DetectionService(db, gateway, investigator=investigator)
    first = await service.run_once()
    assert first["opened"] == 1
    assert first["investigations"] == 1
    assert len(investigated) == 1

    # Second run finds only duplicates; the investigator must not fire again.
    second = await service.run_once()
    assert second["opened"] == 0
    assert second["duplicates"] == 1
    assert second["investigations"] == 0
    assert len(investigated) == 1


async def test_investigation_failure_is_isolated_and_incident_stays_open():
    db, engagement = _setup()
    gateway = FakeMCPGateway(query_events=_beacon_events())

    async def broken_investigator(incident_id: str) -> None:
        raise RuntimeError("model endpoint down")

    service = DetectionService(db, gateway, investigator=broken_investigator)
    result = await service.run_once()
    assert result["opened"] == 1
    assert result["investigations"] == 0
    incident = db.scalar(select(IncidentRecord).where(IncidentRecord.engagement_id == engagement.id))
    assert incident is not None
    metric = db.scalar(select(MetricRecord).where(MetricRecord.name == "detection.investigation_failures"))
    assert metric is not None


async def test_resolved_duplicate_fingerprint_opens_fresh_incident():
    db, engagement = _setup()
    gateway = FakeMCPGateway(query_events=_beacon_events())
    service = DetectionService(db, gateway)
    await service.run_once()
    first = db.scalar(select(IncidentRecord).where(IncidentRecord.engagement_id == engagement.id))
    first.status = "resolved"
    db.commit()
    result = await service.run_once()
    assert result["opened"] == 1
    assert result["duplicates"] == 0
    incidents = list(db.scalars(select(IncidentRecord).where(IncidentRecord.engagement_id == engagement.id)))
    assert len(incidents) == 2
