import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nightagents.agents.agentic_nightrouter import AgenticNightRouter
from nightagents.agents.agentic_nightwatcher import AgenticNightWatcher
from nightagents.agents.research import ResearchAgent
from nightagents.api.auth import require_operator
from nightagents.approval import ApprovalService
from nightagents.assets.lifecycle import RedirectorLifecycleService
from nightagents.assets.reconcile import MythicInventoryReconciler
from nightagents.config import Settings, get_settings
from nightagents.contracts.assets import EngagementCreate, RedirectorHealthUpdate, RedirectorRegistration
from nightagents.contracts.feedback import PredictionOutcomeCreate
from nightagents.contracts.incident import ApprovalRequest, ExecuteRequest, IncidentAccepted, IncidentWebhook
from nightagents.contracts.mythic import CallbackInventory
from nightagents.db.models import (
    AgentRunRecord,
    AssetRecord,
    EngagementRecord,
    ExecutionRecord,
    HandoffRecord,
    IncidentRecord,
    KnowledgeCandidateRecord,
    KnowledgeDocumentRecord,
    KnowledgeSourceRecord,
    OperatorReviewTaskRecord,
    OutboxEventRecord,
    PredictionRecord,
    SourceStatusRecord,
    WebhookNonceRecord,
)
from nightagents.db.repositories import AuditRepository
from nightagents.db.session import create_schema, get_session
from nightagents.feedback import PredictionFeedbackService
from nightagents.interactions import handle_interaction_payload
from nightagents.knowledge.contracts import KnowledgeCandidateReview, KnowledgeDocumentReview, KnowledgeSource
from nightagents.knowledge.hub import KnowledgeHub
from nightagents.mcp_client import RemoteMCPGateway
from nightagents.models.factory import build_model
from nightagents.notifications.slack import SlackNotifier
from nightagents.operational.prometheus import PROMETHEUS_CONTENT_TYPE, render_prometheus
from nightagents.orchestration.runtime import DurableSkillOrchestrator
from nightagents.predictions import PredictionHorizonService
from nightagents.security.slack_signing import verify_slack_signature
from nightagents.security.webhook import verify_webhook
from nightagents.skills.registry import SkillRegistry


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if get_settings().environment == "development":
        create_schema()
    yield


def _required_mcp_tools() -> list[str]:
    registry = SkillRegistry(Path("skills"))
    tools: set[str] = set()
    for manifest in registry.list():
        tools.update(manifest.tools)
    return sorted(tools)


def _operations_summary(db: Session) -> dict:
    def scalar_count(model, *conditions) -> int:
        return int(db.scalar(select(func.count()).select_from(model).where(*conditions)) or 0)

    return {
        "incidents_open": scalar_count(IncidentRecord, IncidentRecord.status != "closed"),
        "agent_runs_active": scalar_count(AgentRunRecord, AgentRunRecord.status == "running"),
        "executions_observing": scalar_count(ExecutionRecord, ExecutionRecord.state == "observing"),
        "outbox_pending": scalar_count(OutboxEventRecord, OutboxEventRecord.published_at.is_(None)),
        "knowledge_quarantined": scalar_count(
            KnowledgeDocumentRecord, KnowledgeDocumentRecord.status == "quarantined"
        ),
        "audit": AuditRepository(db).verify_chain(),
    }


def create_app() -> FastAPI:
    app = FastAPI(title="NightAgents", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "service": "nightagents"}

    @app.get("/health/ready")
    async def readiness(settings: Annotated[Settings, Depends(get_settings)]) -> dict:
        report = settings.deployment_readiness()
        mcp_ready = True
        mcp_issues: list[str] = []
        if settings.mcp_endpoints_json != "{}":
            try:
                gateway = RemoteMCPGateway.from_json(settings.mcp_endpoints_json)
                required = _required_mcp_tools()
                validation = await gateway.validate_required_tools(required)
                if not validation["valid"]:
                    mcp_ready = False
                    mcp_issues.extend(f"missing MCP tool: {name}" for name in validation["missing"])
                await gateway.close()
            except Exception as exc:
                mcp_ready = False
                mcp_issues.append(f"MCP discovery failed: {exc}")
        ready = report["ready"] and mcp_ready
        return {
            "status": "ready" if ready else "not_ready",
            **report,
            "mcp_ready": mcp_ready,
            "mcp_issues": mcp_issues,
        }

    @app.get("/metrics")
    async def prometheus_metrics(db: Annotated[Session, Depends(get_session)]) -> Response:
        return Response(render_prometheus(db), media_type=PROMETHEUS_CONTENT_TYPE)

    @app.get("/api/v2/operations/mcp-health")
    async def mcp_health(
        db: Annotated[Session, Depends(get_session)],
        settings: Annotated[Settings, Depends(get_settings)],
        _: Annotated[str, Depends(require_operator)],
    ) -> dict:
        gateway = RemoteMCPGateway.from_json(settings.mcp_endpoints_json)
        try:
            discovered = await gateway.discover_all()
            health = gateway.health_snapshot()
            health_check = await gateway.check_health()
            backpressure = gateway.backpressure_metrics()
            required = _required_mcp_tools()
            validation = await gateway.validate_required_tools(required)
        finally:
            await gateway.close()
        return {
            "discovered": discovered,
            "health": health,
            "health_check": health_check,
            "backpressure": backpressure,
            "required_tools": required,
            "validation": validation,
        }

    @app.get("/api/v2/audit/verify")
    async def verify_audit_chain(
        db: Annotated[Session, Depends(get_session)],
        _: Annotated[str, Depends(require_operator)],
    ) -> dict:
        return AuditRepository(db).verify_chain()

    @app.get("/api/v2/operations/summary")
    async def operations_summary(
        db: Annotated[Session, Depends(get_session)],
        _: Annotated[str, Depends(require_operator)],
    ) -> dict:
        return _operations_summary(db)

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard_page() -> HTMLResponse:
        """The live operations page. Static shell only - the data endpoint it
        polls keeps the operator-token requirement, so the page itself carries
        no new auth surface."""
        return HTMLResponse((Path(__file__).parent.parent / "web" / "dashboard.html").read_text())

    @app.get("/api/v2/dashboard/live")
    async def dashboard_live(
        db: Annotated[Session, Depends(get_session)],
        _: Annotated[str, Depends(require_operator)],
    ) -> dict:
        incidents = db.scalars(
            select(IncidentRecord)
            .where(IncidentRecord.status != "closed")
            .order_by(IncidentRecord.received_at.desc())
            .limit(10)
        )
        handoffs = db.scalars(
            select(HandoffRecord)
            .where(HandoffRecord.status == "proposed")
            .order_by(HandoffRecord.created_at.desc())
            .limit(10)
        )
        executions = db.scalars(select(ExecutionRecord).order_by(ExecutionRecord.updated_at.desc()).limit(10))
        return {
            "summary": _operations_summary(db),
            "incidents": [
                {
                    "id": item.id,
                    "severity": item.severity,
                    "rule_id": item.rule_id,
                    "status": item.status,
                    "summary": item.summary,
                    "received_at": item.received_at.isoformat() if item.received_at else None,
                }
                for item in incidents
            ],
            "handoffs": [
                {
                    "id": item.id,
                    "status": item.status,
                    "expires_at": item.expires_at.isoformat() if item.expires_at else None,
                }
                for item in handoffs
            ],
            "executions": [
                {
                    "id": item.id,
                    "handoff_id": item.handoff_id,
                    "state": item.state,
                    "finished_at": item.updated_at.isoformat() if item.updated_at else None,
                }
                for item in executions
            ],
        }

    @app.get("/api/v2/agent-runs/{run_id}")
    async def get_agent_run(
        run_id: str,
        db: Annotated[Session, Depends(get_session)],
        _: Annotated[str, Depends(require_operator)],
    ) -> dict:
        run = db.get(AgentRunRecord, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="agent run not found")
        return {
            "run_id": run.id,
            "agent": run.agent,
            "status": run.status,
            "current_node": run.current_node,
            "state": run.state_json,
            "deadline_at": run.deadline_at,
            "cancelled_at": run.cancelled_at,
        }

    @app.post("/api/v2/agent-runs/{run_id}/cancel")
    async def cancel_agent_run(
        run_id: str,
        db: Annotated[Session, Depends(get_session)],
        operator: Annotated[str, Depends(require_operator)],
    ) -> dict:
        run = db.get(AgentRunRecord, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="agent run not found")
        if run.status == "completed":
            raise HTTPException(status_code=409, detail="completed run cannot be cancelled")
        run.status = "cancelled"
        run.cancelled_at = datetime.now(UTC)
        AuditRepository(db).append(
            operator,
            "agent_run.cancelled",
            "agent_run",
            run.id,
            {"current_node": run.current_node},
            run.engagement_id,
        )
        db.commit()
        return {"run_id": run.id, "status": run.status}

    @app.post("/api/v2/agent-runs/{run_id}/pause")
    async def pause_agent_run(
        run_id: str,
        db: Annotated[Session, Depends(get_session)],
        operator: Annotated[str, Depends(require_operator)],
        reason: str = "operator pause",
    ) -> dict:
        run = db.get(AgentRunRecord, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="agent run not found")
        if run.status != "running":
            raise HTTPException(status_code=409, detail="only running runs can be paused")
        run.status = "paused"
        run.wait_reason = reason
        AuditRepository(db).append(
            operator,
            "agent_run.paused",
            "agent_run",
            run.id,
            {"current_node": run.current_node, "reason": reason},
            run.engagement_id,
        )
        db.commit()
        return {"run_id": run.id, "status": run.status, "wait_reason": run.wait_reason}

    @app.post("/api/v2/agent-runs/{run_id}/resume")
    async def resume_agent_run(
        run_id: str,
        db: Annotated[Session, Depends(get_session)],
        settings: Annotated[Settings, Depends(get_settings)],
        _: Annotated[str, Depends(require_operator)],
    ) -> dict:
        run = db.get(AgentRunRecord, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="agent run not found")
        if run.status not in {"failed", "paused", "timed_out", "cancelled"}:
            raise HTTPException(status_code=409, detail="run is not resumable")
        skill_id = run.budget_json.get("skill_id")
        if not skill_id:
            raise HTTPException(status_code=409, detail="run has no resumable skill")
        endpoints = (
            settings.research_mcp_endpoints_json
            if run.agent == "research-agent"
            else settings.mcp_endpoints_json
        )
        run.status = "running"
        run.cancelled_at = None
        run.deadline_at = datetime.now(UTC) + timedelta(seconds=settings.workflow_deadline_seconds)
        run.wait_reason = None
        db.commit()
        result = await DurableSkillOrchestrator(
            db,
            SkillRegistry(Path("skills")),
            RemoteMCPGateway.from_json(endpoints),
            build_model(settings),
        ).run(run.agent, skill_id, run.engagement_id, run.objective, {}, run_id=run.id)
        return {"run_id": result.run_id, "status": result.status, "state": result.state}

    @app.post("/api/v2/engagements", status_code=201)
    async def create_engagement(
        body: EngagementCreate,
        db: Annotated[Session, Depends(get_session)],
        _: Annotated[str, Depends(require_operator)],
    ) -> dict:
        existing = db.scalar(select(EngagementRecord).where(EngagementRecord.name == body.name))
        if existing:
            return {"engagement_id": existing.id, "name": existing.name, "status": existing.status}
        engagement = EngagementRecord(name=body.name)
        db.add(engagement)
        db.flush()
        AuditRepository(db).append(
            "operator",
            "engagement.created",
            "engagement",
            engagement.id,
            {"name": engagement.name},
            engagement.id,
        )
        db.commit()
        return {"engagement_id": engagement.id, "name": engagement.name, "status": engagement.status}

    @app.post("/api/v2/engagements/{engagement_id}/redirectors", status_code=201)
    async def register_redirector(
        engagement_id: str,
        body: RedirectorRegistration,
        db: Annotated[Session, Depends(get_session)],
        _: Annotated[str, Depends(require_operator)],
    ) -> dict:
        try:
            asset = RedirectorLifecycleService(db).register(
                engagement_id,
                body.redirector_id,
                body.domain,
                body.logical_route_id,
                body.provider_resource_id,
                body.mythic_profile,
                body.failover_group,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"asset_id": asset.id, "redirector_id": asset.external_id, "state": asset.state}

    @app.put("/api/v2/engagements/{engagement_id}/redirectors/{redirector_id}/health")
    async def record_redirector_health(
        engagement_id: str,
        redirector_id: str,
        body: RedirectorHealthUpdate,
        db: Annotated[Session, Depends(get_session)],
        _: Annotated[str, Depends(require_operator)],
    ) -> dict:
        try:
            asset = RedirectorLifecycleService(db).record_health(
                engagement_id, redirector_id, body.status == "healthy", body.evidence
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"asset_id": asset.id, "state": asset.state, "version": asset.version}

    @app.get("/api/v2/engagements/{engagement_id}/assets")
    async def list_assets(
        engagement_id: str,
        db: Annotated[Session, Depends(get_session)],
        _: Annotated[str, Depends(require_operator)],
    ) -> list[dict]:
        query = select(AssetRecord).where(AssetRecord.engagement_id == engagement_id).order_by(AssetRecord.asset_type)
        assets = list(db.scalars(query))
        return [
            {
                "asset_id": item.id,
                "type": item.asset_type,
                "external_id": item.external_id,
                "name": item.name,
                "state": item.state,
                "version": item.version,
                "attributes": item.attributes_json,
            }
            for item in assets
        ]

    @app.post("/api/v2/engagements/{engagement_id}/reconcile/mythic")
    async def reconcile_mythic_inventory(
        engagement_id: str,
        db: Annotated[Session, Depends(get_session)],
        settings: Annotated[Settings, Depends(get_settings)],
        _: Annotated[str, Depends(require_operator)],
    ) -> dict:
        engagement = db.get(EngagementRecord, engagement_id)
        if engagement is None:
            raise HTTPException(status_code=404, detail="engagement not found")
        try:
            inventory_data = await RemoteMCPGateway.from_json(settings.mcp_endpoints_json).call_tool(
                "mythic", "list_callbacks", {"engagement_id": engagement.name}
            )
            inventory = CallbackInventory.model_validate(inventory_data)
            return MythicInventoryReconciler(db).reconcile(engagement_id, inventory)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/v2/knowledge/sources", status_code=201)
    async def register_knowledge_source(
        body: KnowledgeSource,
        db: Annotated[Session, Depends(get_session)],
        _: Annotated[str, Depends(require_operator)],
    ) -> dict:
        record = KnowledgeHub(db).register_source(body)
        db.commit()
        return {"source_id": record.id, "enabled": record.enabled, "trust_tier": record.trust_tier}

    @app.get("/api/v2/knowledge/sources")
    async def list_knowledge_sources(
        db: Annotated[Session, Depends(get_session)],
        _: Annotated[str, Depends(require_operator)],
    ) -> list[dict]:
        sources = list(db.scalars(select(KnowledgeSourceRecord).order_by(KnowledgeSourceRecord.id)))
        return [
            {
                "source_id": source.id,
                "publisher": source.publisher,
                "uri": source.source_uri,
                "enabled": source.enabled,
                "trust_tier": source.trust_tier,
                "health": (
                    db.get(SourceStatusRecord, source.id).status
                    if db.get(SourceStatusRecord, source.id)
                    else "unknown"
                ),
            }
            for source in sources
        ]

    @app.post("/api/v2/knowledge/sources/{source_id}/refresh")
    async def refresh_knowledge_source(
        source_id: str,
        db: Annotated[Session, Depends(get_session)],
        settings: Annotated[Settings, Depends(get_settings)],
        _: Annotated[str, Depends(require_operator)],
    ) -> dict:
        hub = KnowledgeHub(db)
        result = await hub.refresh(source_id)
        payload = result.model_dump(mode="json")
        if result.changed and result.document_id and settings.research_mcp_endpoints_json != "{}":
            researched = await ResearchAgent(
                db,
                RemoteMCPGateway.from_json(settings.research_mcp_endpoints_json),
                build_model(settings),
            ).process(result.document_id)
            payload["research_run_id"] = researched.run_id
        return payload

    @app.get("/api/v2/knowledge/documents")
    async def list_knowledge_documents(
        db: Annotated[Session, Depends(get_session)],
        _: Annotated[str, Depends(require_operator)],
        document_status: Annotated[str | None, Query(alias="status")] = None,
    ) -> list[dict]:
        statement = select(KnowledgeDocumentRecord)
        if document_status:
            statement = statement.where(KnowledgeDocumentRecord.status == document_status)
        documents = list(db.scalars(statement.order_by(KnowledgeDocumentRecord.retrieved_at.desc())))
        return [
            {
                "document_id": document.id,
                "source_id": document.source_id,
                "title": document.title,
                "status": document.status,
                "content_hash": document.content_hash,
                "retrieved_at": document.retrieved_at,
            }
            for document in documents
        ]

    @app.post("/api/v2/knowledge/documents/{document_id}/approve")
    async def approve_knowledge_document(
        document_id: str,
        db: Annotated[Session, Depends(get_session)],
        operator: Annotated[str, Depends(require_operator)],
    ) -> dict:
        try:
            document = KnowledgeHub(db).approve_document(document_id, operator)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"document_id": document.id, "status": document.status}

    @app.post("/api/v2/knowledge/documents/{document_id}/review")
    async def review_knowledge_document(
        document_id: str,
        body: KnowledgeDocumentReview,
        db: Annotated[Session, Depends(get_session)],
        operator: Annotated[str, Depends(require_operator)],
    ) -> dict:
        try:
            document = KnowledgeHub(db).review_document(
                document_id, operator, body.decision, body.risk_acceptance, body.notes
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"document_id": document.id, "status": document.status, "metadata": document.metadata_json}

    @app.get("/api/v2/knowledge/candidates")
    async def list_knowledge_candidates(
        db: Annotated[Session, Depends(get_session)],
        _: Annotated[str, Depends(require_operator)],
    ) -> list[dict]:
        candidates = list(
            db.scalars(select(KnowledgeCandidateRecord).order_by(KnowledgeCandidateRecord.created_at.desc()))
        )
        return [
            {
                "candidate_id": candidate.id,
                "document_id": candidate.document_id,
                "type": candidate.candidate_type,
                "title": candidate.title,
                "confidence": candidate.confidence,
                "status": candidate.status,
                "citations": candidate.citations_json,
            }
            for candidate in candidates
        ]

    @app.post("/api/v2/knowledge/candidates/{candidate_id}/review")
    async def review_knowledge_candidate(
        candidate_id: str,
        body: KnowledgeCandidateReview,
        db: Annotated[Session, Depends(get_session)],
        operator: Annotated[str, Depends(require_operator)],
    ) -> dict:
        try:
            candidate = KnowledgeHub(db).review_candidate(
                candidate_id, operator, body.decision, body.notes
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"candidate_id": candidate.id, "status": candidate.status}

    @app.post("/api/v2/handoffs/{handoff_id}/outcome", status_code=201)
    async def record_prediction_outcome(
        handoff_id: str,
        body: PredictionOutcomeCreate,
        db: Annotated[Session, Depends(get_session)],
        operator: Annotated[str, Depends(require_operator)],
    ) -> dict:
        try:
            prediction = db.scalar(
                select(PredictionRecord).where(PredictionRecord.handoff_id == handoff_id)
            )
            handoff = db.get(HandoffRecord, handoff_id)
            provenance = {}
            if handoff is not None:
                provenance = {
                    "model_version": handoff.payload_json.get("model_version", ""),
                    "skill_version": str(handoff.payload_json.get("skill_version", "")),
                }
            outcome = PredictionFeedbackService(db).record(
                handoff_id,
                operator,
                body,
                prediction_id=prediction.id if prediction else None,
                provenance=provenance,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "outcome_id": outcome.id,
            "matched": outcome.matched,
            "brier_score": outcome.brier_score,
        }

    @app.get("/api/v2/predictions/{prediction_id}/status")
    async def get_prediction_status(
        prediction_id: str,
        db: Annotated[Session, Depends(get_session)],
        _: Annotated[str, Depends(require_operator)],
    ) -> dict:
        prediction = db.get(PredictionRecord, prediction_id)
        if prediction is None:
            raise HTTPException(status_code=404, detail="prediction not found")
        return {
            "prediction_id": prediction.id,
            "status": prediction.status,
            "predicted_event": prediction.predicted_event,
            "predicted_probability": prediction.predicted_probability,
            "expires_at": prediction.expires_at,
            "outcome_id": prediction.outcome_id,
            "review_task_id": prediction.review_task_id,
        }

    @app.post("/api/v2/predictions/{prediction_id}/verify-horizon")
    async def verify_prediction_horizon(
        prediction_id: str,
        db: Annotated[Session, Depends(get_session)],
        settings: Annotated[Settings, Depends(get_settings)],
        operator: Annotated[str, Depends(require_operator)],
    ) -> dict:
        try:
            result = await PredictionHorizonService(
                db, RemoteMCPGateway.from_json(settings.mcp_endpoints_json)
            ).verify_at_expiry(prediction_id, operator)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return result

    @app.get("/api/v2/operator-review-tasks")
    async def list_operator_review_tasks(
        db: Annotated[Session, Depends(get_session)],
        _: Annotated[str, Depends(require_operator)],
        status: str | None = None,
    ) -> list[dict]:
        statement = select(OperatorReviewTaskRecord)
        if status:
            statement = statement.where(OperatorReviewTaskRecord.status == status)
        tasks = list(db.scalars(statement.order_by(OperatorReviewTaskRecord.created_at.desc())))
        return [
            {
                "task_id": task.id,
                "target_type": task.target_type,
                "target_id": task.target_id,
                "reason": task.reason,
                "status": task.status,
                "created_at": task.created_at,
            }
            for task in tasks
        ]

    @app.post("/api/v2/operator-review-tasks/{task_id}/resolve")
    async def resolve_operator_review_task(
        task_id: str,
        db: Annotated[Session, Depends(get_session)],
        operator: Annotated[str, Depends(require_operator)],
        resolution: str = "",
    ) -> dict:
        try:
            task = PredictionHorizonService(db, RemoteMCPGateway.from_json("{}")).resolve_review_task(
                task_id, operator, resolution
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"task_id": task.id, "status": task.status, "resolution": task.resolution}

    @app.get("/api/v2/engagements/{engagement_id}/prediction-calibration")
    async def prediction_calibration(
        engagement_id: str,
        db: Annotated[Session, Depends(get_session)],
        _: Annotated[str, Depends(require_operator)],
    ) -> dict:
        return PredictionFeedbackService(db).calibration(engagement_id)

    @app.post("/api/v2/webhooks/elastic/incidents", response_model=IncidentAccepted, status_code=202)
    async def elastic_incident(
        request: Request,
        db: Annotated[Session, Depends(get_session)],
        settings: Annotated[Settings, Depends(get_settings)],
        x_nightagents_timestamp: str = Header(),
        x_nightagents_nonce: str = Header(),
        x_nightagents_signature: str = Header(),
    ) -> IncidentAccepted:
        body = await request.body()
        try:
            verify_webhook(
                settings.elastic_webhook_secret,
                x_nightagents_timestamp,
                x_nightagents_nonce,
                x_nightagents_signature,
                body,
            )
            payload = IncidentWebhook.model_validate(json.loads(body))
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
        if db.get(EngagementRecord, payload.engagement_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="engagement not found")
        if payload.redirector_asset_id:
            redirector = db.get(AssetRecord, payload.redirector_asset_id)
            if redirector is None or redirector.engagement_id != payload.engagement_id:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid redirector asset")
        if db.get(WebhookNonceRecord, x_nightagents_nonce):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="webhook nonce already used")
        db.add(
            WebhookNonceRecord(
                nonce=x_nightagents_nonce,
                scope="elastic.incident",
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
        try:
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="webhook nonce already used") from exc
        existing = db.scalar(
            select(IncidentRecord).where(
                IncidentRecord.engagement_id == payload.engagement_id,
                IncidentRecord.external_id == payload.external_alert_id,
            )
        )
        if existing:
            return IncidentAccepted(incident_id=existing.id, duplicate=True)
        incident = IncidentRecord(
            engagement_id=payload.engagement_id,
            external_id=payload.external_alert_id,
            redirector_asset_id=payload.redirector_asset_id,
            severity=payload.severity,
            rule_id=payload.rule_id,
            summary=payload.summary,
            evidence_refs_json=payload.evidence_refs,
            source_health_json=payload.source_health,
            occurred_at=payload.occurred_at,
        )
        db.add(incident)
        db.commit()
        return IncidentAccepted(incident_id=incident.id, duplicate=False)

    @app.post("/api/v2/incidents/{incident_id}/investigate")
    async def investigate(
        incident_id: str,
        db: Annotated[Session, Depends(get_session)],
        settings: Annotated[Settings, Depends(get_settings)],
        _: Annotated[str, Depends(require_operator)],
    ) -> dict:
        result = await AgenticNightWatcher(
            db,
            RemoteMCPGateway.from_json(settings.mcp_endpoints_json),
            build_model(settings),
            settings,
        ).investigate(incident_id)
        payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
        if isinstance(payload, dict) and payload.get("handoff_id"):
            incident = db.get(IncidentRecord, incident_id)
            await SlackNotifier(
                db,
                settings.slack_webhook_url,
                api_token=settings.slack_api_token,
                channel=settings.slack_channel,
                channel_map=json.loads(settings.slack_channels_json or "{}"),
                interactive=bool(settings.slack_signing_secret),
            ).handoff_pending_approval(payload, engagement_id=incident.engagement_id if incident else None)
            db.commit()
        return payload

    @app.get("/api/v2/handoffs/{handoff_id}")
    async def get_handoff(
        handoff_id: str,
        db: Annotated[Session, Depends(get_session)],
        _: Annotated[str, Depends(require_operator)],
    ) -> dict:
        handoff = db.get(HandoffRecord, handoff_id)
        if handoff is None:
            raise HTTPException(status_code=404, detail="handoff not found")
        return {
            "handoff_id": handoff.id,
            "status": handoff.status,
            "expires_at": handoff.expires_at.isoformat() if handoff.expires_at else None,
        }

    @app.post("/api/v2/slack/interactions")
    async def slack_interactions(
        request: Request,
        db: Annotated[Session, Depends(get_session)],
        settings: Annotated[Settings, Depends(get_settings)],
    ) -> dict:
        """Slack interactivity receiver. Authenticated by the Slack request
        signature (not the operator token) - same verification discipline as
        the Elastic incident webhook."""
        body = await request.body()
        if not verify_slack_signature(
            settings.slack_signing_secret,
            request.headers.get("X-Slack-Request-Timestamp", ""),
            body,
            request.headers.get("X-Slack-Signature", ""),
        ):
            raise HTTPException(status_code=401, detail="invalid slack signature")
        form = parse_qs(body.decode(errors="replace"))
        try:
            payload = json.loads(form.get("payload", ["{}"])[0])
        except (json.JSONDecodeError, IndexError):
            raise HTTPException(status_code=400, detail="invalid interaction payload") from None
        text = await handle_interaction_payload(db, settings, payload)
        return {"text": text}

    @app.post("/api/v2/handoffs/{handoff_id}/approve")
    async def approve(
        handoff_id: str,
        body: ApprovalRequest,
        db: Annotated[Session, Depends(get_session)],
        settings: Annotated[Settings, Depends(get_settings)],
        operator_identity: Annotated[str, Depends(require_operator)],
    ) -> dict:
        try:
            if body.operator_id != operator_identity:
                raise ValueError("operator identity does not match authenticated principal")
            approval = ApprovalService(db, settings).approve(handoff_id, operator_identity)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"approval_id": approval.id, "status": approval.decision}

    @app.post("/api/v2/handoffs/{handoff_id}/execute")
    async def execute(
        handoff_id: str,
        body: ExecuteRequest,
        db: Annotated[Session, Depends(get_session)],
        settings: Annotated[Settings, Depends(get_settings)],
        _: Annotated[str, Depends(require_operator)],
    ) -> dict:
        try:
            result = await AgenticNightRouter(
                db,
                RemoteMCPGateway.from_json(settings.mcp_endpoints_json),
                RemoteMCPGateway.from_json(settings.safety_mcp_endpoints_json),
                build_model(settings),
                settings,
            ).execute_approved_handoff(
                handoff_id,
                body.logical_route_id,
                body.old_redirector_id,
                body.new_redirector_id,
            )
            outcome = {"execution_id": result.id, "state": result.state, "plan": result.plan_json}
            await SlackNotifier(
                db,
                settings.slack_webhook_url,
                api_token=settings.slack_api_token,
                channel=settings.slack_channel,
                channel_map=json.loads(settings.slack_channels_json or "{}"),
            ).execution_finished(handoff_id, outcome, engagement_id=result.engagement_id)
            db.commit()
            return outcome
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("nightagents.api.main:app", host=get_settings().api_host, port=8000, reload=False)
