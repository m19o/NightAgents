import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic

from sqlalchemy import delete, select

from nightagents.agents.agentic_nightwatcher import AgenticNightWatcher
from nightagents.agents.research import ResearchAgent
from nightagents.config import get_settings
from nightagents.db.models import EngagementRecord, IncidentRecord, WebhookNonceRecord
from nightagents.db.session import SessionLocal
from nightagents.detection import DetectionService
from nightagents.knowledge.hub import KnowledgeHub
from nightagents.mcp_client import MCPGateway, RemoteMCPGateway
from nightagents.models.factory import build_model
from nightagents.notifications.slack import SlackNotifier
from nightagents.operational import OperationalMetrics, RetentionPolicy
from nightagents.predictions import PredictionHorizonService
from nightagents.third_party.monitor import ThirdPartyMonitorService
from nightagents.workers.outbox import OutboxPublisher


class AgentScheduler:
    """Small durable-worker loop; every iteration opens a fresh DB session."""

    def __init__(
        self,
        outbox_handler: Callable,
        interval_seconds: float = 5.0,
        detection_gateway: MCPGateway | None = None,
    ):
        self.outbox_handler = outbox_handler
        self.interval_seconds = interval_seconds
        self.detection_gateway = detection_gateway
        self._last_detection_run = 0.0
        self._last_intel_run = 0.0
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run_once(self) -> dict[str, int]:
        with SessionLocal() as db:
            metrics = OperationalMetrics(db)
            expired_nonces = db.execute(
                delete(WebhookNonceRecord).where(WebhookNonceRecord.expires_at <= datetime.now(UTC))
            ).rowcount
            db.commit()
            published = await OutboxPublisher(db).publish_once(self.outbox_handler)
            hub = KnowledgeHub(db)
            refreshed = 0
            research_failures = 0
            research_runs = 0
            settings = get_settings()
            for source in hub.due_sources():
                try:
                    result = await hub.refresh(source.id)
                    if (
                        result.changed
                        and result.document_id
                        and settings.research_mcp_endpoints_json != "{}"
                    ):
                        await ResearchAgent(
                            db,
                            RemoteMCPGateway.from_json(settings.research_mcp_endpoints_json),
                            build_model(settings),
                        ).process(result.document_id)
                        research_runs += 1
                    refreshed += 1
                except Exception:
                    research_failures += 1
                    refreshed += 1

            horizon_closed = 0
            horizon_reviews = 0
            try:
                horizon_service = PredictionHorizonService(
                    db, RemoteMCPGateway.from_json(settings.mcp_endpoints_json)
                )
                for prediction in horizon_service.due_predictions():
                    try:
                        result = await horizon_service.verify_at_expiry(prediction.id)
                        if result["status"] == "outcome_recorded":
                            horizon_closed += 1
                        else:
                            horizon_reviews += 1
                    except Exception:
                        horizon_reviews += 1
            except Exception:
                pass

            detection_opened = 0
            detection_failures = 0
            if (
                settings.detection_interval_seconds > 0
                and monotonic() - self._last_detection_run >= settings.detection_interval_seconds
            ):
                try:
                    gateway = self.detection_gateway or RemoteMCPGateway.from_json(settings.mcp_endpoints_json)
                    investigator = None
                    watcher = None
                    if settings.auto_investigate or settings.agentic_hunt:
                        watcher = AgenticNightWatcher(
                            db,
                            RemoteMCPGateway.from_json(settings.mcp_endpoints_json),
                            build_model(settings),
                            settings,
                        )
                    if settings.auto_investigate:

                        async def investigator(incident_id: str) -> None:
                            payload = await watcher.investigate(incident_id)
                            if isinstance(payload, dict) and payload.get("disposition") == "abstain":
                                # Model-driven critics have run-to-run variance;
                                # abstention is safe-neutral, so retry once.
                                payload = await watcher.investigate(incident_id)
                            if isinstance(payload, dict) and payload.get("handoff_id"):
                                incident = db.get(IncidentRecord, incident_id)
                                await SlackNotifier(
                                    db,
                                    settings.slack_webhook_url,
                                    api_token=settings.slack_api_token,
                                    channel=settings.slack_channel,
                                    channel_map=json.loads(settings.slack_channels_json or "{}"),
                                    interactive=bool(settings.slack_signing_secret),
                                ).handoff_pending_approval(
                                    payload, engagement_id=incident.engagement_id if incident else None
                                )
                                db.commit()

                    detection = await DetectionService(
                        db,
                        gateway,
                        source=settings.detection_source,
                        investigator=investigator,
                    ).run_once()
                    detection_opened = detection["opened"]
                    detection_failures = detection["failures"]
                    if settings.agentic_hunt and watcher is not None:
                        # The agent hunts telemetry itself; the deterministic
                        # detection above remains the safety net underneath.
                        for engagement in db.scalars(
                            select(EngagementRecord).where(EngagementRecord.status == "active")
                        ):
                            try:
                                hunt = await watcher.hunt(engagement.id)
                                detection_opened += hunt["opened"]
                                if investigator is not None:
                                    for hunt_incident_id in hunt["incident_ids"]:
                                        await investigator(hunt_incident_id)
                            except Exception:
                                detection_failures += 1
                except Exception:
                    detection_failures += 1
                self._last_detection_run = monotonic()

            intel_opened = 0
            intel_failures = 0
            if (
                settings.third_party_monitor_interval_seconds > 0
                and monotonic() - self._last_intel_run >= settings.third_party_monitor_interval_seconds
            ):
                try:
                    monitor = ThirdPartyMonitorService(
                        db,
                        json.loads(settings.third_party_feeds_json or "{}"),
                        verify_tls=settings.third_party_feeds_verify_tls,
                        notifier=SlackNotifier(
                            db,
                            settings.slack_webhook_url,
                            api_token=settings.slack_api_token,
                            channel=settings.slack_channel,
                            channel_map=json.loads(settings.slack_channels_json or "{}"),
                        ),
                    )
                    intel = await monitor.run_once()
                    intel_opened = intel["opened"]
                    intel_failures = intel["failures"]
                except Exception:
                    intel_failures += 1
                self._last_intel_run = monotonic()

            metrics.record("scheduler.outbox.published", published)
            metrics.record("scheduler.knowledge.refreshed", refreshed)
            metrics.record("scheduler.research.started", research_runs)
            metrics.record("scheduler.research.failures", research_failures)
            metrics.record("scheduler.horizon.closed", horizon_closed)
            metrics.record("scheduler.horizon.reviews", horizon_reviews)
            metrics.record("scheduler.detection.incidents_opened", detection_opened)
            metrics.record("scheduler.detection.failures", detection_failures)
            metrics.record("scheduler.third_party_monitor.incidents_opened", intel_opened)
            metrics.record("scheduler.third_party_monitor.failures", intel_failures)
            metrics.record("scheduler.webhook_nonces.expired", int(expired_nonces or 0))

            archived_tool_calls = 0
            archived_audit_events = 0
            try:
                retention = RetentionPolicy(db)
                archived_tool_calls = retention.archive_tool_call_payloads(older_than_days=30)
                archived_audit_events = retention.archive_audit_events(older_than_days=365)
            except Exception:
                pass
            metrics.record("retention.tool_calls.archived", archived_tool_calls)
            metrics.record("retention.audit_events.archived", archived_audit_events)
            db.commit()

            return {
                "published": published,
                "knowledge_sources_refreshed": refreshed,
                "research_runs_started": research_runs,
                "research_failures": research_failures,
                "expired_webhook_nonces_removed": int(expired_nonces or 0),
                "horizon_outcomes_recorded": horizon_closed,
                "horizon_operator_reviews": horizon_reviews,
                "detection_incidents_opened": detection_opened,
                "detection_failures": detection_failures,
                "archived_tool_calls": archived_tool_calls,
                "archived_audit_events": archived_audit_events,
            }

    async def run(self) -> None:
        while not self._stop.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass
