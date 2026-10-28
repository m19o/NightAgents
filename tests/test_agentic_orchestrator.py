from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from nightagents.agents.agentic_nightrouter import AgenticNightRouter
from nightagents.agents.agentic_nightwatcher import AgenticNightWatcher
from nightagents.agents.research import ResearchAgent
from nightagents.approval import ApprovalService
from nightagents.config import Settings
from nightagents.contracts.agent import AgentHandoff
from nightagents.db.base import Base
from nightagents.db.models import (
    AgentStepRecord,
    EngagementRecord,
    HandoffRecord,
    IncidentRecord,
    KnowledgeDocumentRecord,
    ToolCallRecord,
)
from nightagents.db.session import build_engine
from nightagents.mcp_client import MCPToolError, RemoteMCPGateway
from nightagents.models.local import LocalStructuredModel
from nightagents.orchestration.runtime import DurableSkillOrchestrator
from nightagents.skills.registry import SkillRegistry


class FakeGateway:
    def __init__(self):
        self.calls = []

    async def call_tool(self, server: str, tool: str, arguments: dict):
        self.calls.append((server, tool, arguments))
        responses = {
            ("elastic", "get_source_health"): {"logs-edge": {"status": "healthy"}},
            ("elastic", "get_incident"): {"status": "success", "events": [{"_id": "alert"}]},
            ("elastic", "run_catalog_query"): {
                "status": "success",
                "events": [{"_id": "event-1", "message": "profiling"}],
            },
            ("mythic", "list_callbacks"): {"status": "success", "callbacks": []},
            ("memory", "search_memory"): [],
        }
        return responses[(server, tool)]


class FlakyVerifierModel:
    """Fails schema validation once, then returns a compliant verdict."""

    model_name = "flaky-verifier"

    def __init__(self):
        self.invocations = 0
        self.saw_error_feedback = False

    async def invoke(self, role: str, context: dict) -> dict:
        assert role == "evidence_verifier"
        self.invocations += 1
        if context.get("previous_output_error"):
            self.saw_error_feedback = True
        if self.invocations == 1:
            return {"unknowns": []}  # missing required 'supported'
        return {
            "supported": True,
            "unknowns": [],
            "negative_evidence_status": "verified",
        }


class RouterGateway:
    def __init__(self, active_callback: bool = False, verification_status: str = "confirmed"):
        self.calls = []
        self.active_callback = active_callback
        self.verification_status = verification_status

    async def call_tool(self, server: str, tool: str, arguments: dict):
        self.calls.append((server, tool, arguments))
        now = datetime.now(UTC)
        if (server, tool) == ("mythic", "list_callbacks"):
            callbacks = []
            if self.active_callback:
                callbacks.append(
                    {
                        "callback_id": "callback-1",
                        "operation_id": "authorized-operation",
                        "payload_type": "lab-agent",
                        "c2_profile": "http",
                        "route_id": "old",
                        "last_seen": now.isoformat(),
                        "expected_checkin_seconds": 10,
                        "active": True,
                    }
                )
            return {"status": "success", "callbacks": callbacks, "observed_at": now.isoformat(), "error": None}
        if (server, tool) == ("infrastructure", "route_health"):
            return {
                "route_id": arguments["redirector_id"],
                "status": "success",
                "healthy": True,
                "observed_at": now.isoformat(),
                "checks": {"synthetic": True},
                "error": None,
            }
        if (server, tool) == ("infrastructure", "plan_route_switch"):
            return {
                "plan_id": "plan-1",
                "engagement_id": arguments["engagement_id"],
                "logical_route_id": arguments["logical_route_id"],
                "old_redirector_id": arguments["old_redirector_id"],
                "new_redirector_id": arguments["new_redirector_id"],
                "preconditions": ["synthetic health"],
                "steps": ["switch"],
                "rollback_steps": ["restore"],
                "expires_at": (now + timedelta(minutes=5)).isoformat(),
            }
        if (server, tool) == ("infrastructure", "execute_route_switch"):
            return {
                "success": True,
                "plan_id": arguments["plan"]["plan_id"],
                "idempotency_key": arguments["idempotency_key"],
                "previous_state": {"target": "old"},
                "current_state": {"target": "new"},
                "message": "synthetic switch",
            }
        if (server, tool) == ("infrastructure", "verify_callback_route"):
            return {
                "status": self.verification_status,
                "route_id": arguments["route_id"],
                "observed_callback_ids": [] if self.active_callback else [],
                "missing_callback_ids": ["callback-1"] if self.active_callback else [],
                "reason": "synthetic verification",
            }
        if (server, tool) == ("infrastructure", "rollback_route_switch"):
            return {
                "success": True,
                "plan_id": arguments["plan"]["plan_id"],
                "idempotency_key": arguments["idempotency_key"],
                "previous_state": {"target": "new"},
                "current_state": {"target": "old"},
                "message": "synthetic rollback",
            }
        raise AssertionError(f"unexpected tool call: {server}.{tool}")


class ResearchGateway:
    def __init__(self, document: KnowledgeDocumentRecord):
        self.document = document
        self.calls = []

    async def call_tool(self, server: str, tool: str, arguments: dict):
        self.calls.append((server, tool, arguments))
        if tool == "get_quarantined_document":
            return {
                "document_id": self.document.id,
                "source_id": self.document.source_id,
                "source_uri": self.document.source_uri,
                "content_hash": self.document.content_hash,
                "trust_tier": self.document.trust_tier,
                "risk_flags": [],
                "content_untrusted": self.document.raw_text,
            }
        if tool == "propose_knowledge_candidate":
            return {"candidate_id": "candidate-1", "status": "proposed"}
        if tool == "search_memory":
            return []
        raise AssertionError(f"unexpected research tool: {server}.{tool}")


async def test_durable_skill_orchestrator_executes_manifest_phases_through_mcp() -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        engagement = EngagementRecord(name="authorized-operation")
        db.add(engagement)
        db.commit()
        gateway = FakeGateway()
        registry = SkillRegistry(Path(__file__).parents[1] / "skills")
        orchestrator = DurableSkillOrchestrator(db, registry, gateway, LocalStructuredModel())
        result = await orchestrator.run(
            "nightwatcher",
            "investigate-elastic-incident",
            engagement.id,
            "Investigate synthetic incident",
            {
                "engagement_id": engagement.id,
                "engagement_name": engagement.name,
                "external_alert_id": "alert-1",
                "redirector_id": "edge-1",
                "required_sources": ["logs-edge"],
            },
        )
        steps = list(db.scalars(select(AgentStepRecord).where(AgentStepRecord.run_id == result.run_id)))
        calls = list(db.scalars(select(ToolCallRecord).where(ToolCallRecord.run_id == result.run_id)))
        assert result.status == "completed"
        assert result.state["verification"]["supported"] is True
        assert [item.node for item in steps] == [
            "source_health",
            "elastic_incident",
            "elastic_evidence",
            "callback_inventory",
            "verification",
        ]
        assert len(calls) == 4
        assert gateway.calls[0][2] == {"source_ids": ["logs-edge"]}


async def test_remote_gateway_fails_closed_for_unconfigured_server() -> None:
    with pytest.raises(MCPToolError, match="not configured"):
        await RemoteMCPGateway({}).call_tool("elastic", "get_source_health", {})


async def test_model_phase_retries_once_on_schema_violation() -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        engagement = EngagementRecord(name="authorized-operation")
        db.add(engagement)
        db.commit()
        model = FlakyVerifierModel()
        registry = SkillRegistry(Path(__file__).parents[1] / "skills")
        orchestrator = DurableSkillOrchestrator(db, registry, FakeGateway(), model)
        result = await orchestrator.run(
            "nightwatcher",
            "investigate-elastic-incident",
            engagement.id,
            "Investigate synthetic incident",
            {
                "engagement_id": engagement.id,
                "engagement_name": engagement.name,
                "external_alert_id": "alert-1",
                "redirector_id": "edge-1",
                "required_sources": ["logs-edge"],
            },
        )
        assert result.status == "completed"
        assert model.invocations == 2
        assert model.saw_error_feedback is True
        assert result.state["verification"]["supported"] is True


async def test_agentic_nightwatcher_composes_two_skills_and_signs_handoff() -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        engagement = EngagementRecord(name="authorized-operation")
        db.add(engagement)
        db.flush()
        incident = IncidentRecord(
            engagement_id=engagement.id,
            external_id="agentic-alert",
            severity="high",
            rule_id="profiling",
            summary="Synthetic profiling evidence",
            source_health_json={"logs-edge": {}},
            occurred_at=datetime.now(UTC),
        )
        db.add(incident)
        db.commit()
        gateway = FakeGateway()
        watcher = AgenticNightWatcher(
            db,
            gateway,
            LocalStructuredModel(),
            Settings(handoff_signing_secret="agentic-handoff-signing-secret-long-enough"),
            Path(__file__).parents[1] / "skills",
        )
        result = await watcher.investigate(incident.id)
        assert isinstance(result, AgentHandoff)
        assert result.prediction.event
        assert db.get(HandoffRecord, result.handoff_id) is not None
        assert [(server, tool) for server, tool, _ in gateway.calls] == [
            ("elastic", "get_source_health"),
            ("elastic", "get_incident"),
            ("elastic", "run_catalog_query"),
            ("mythic", "list_callbacks"),
            ("memory", "search_memory"),
        ]


async def test_agentic_nightrouter_plans_executes_and_verifies_through_mcp() -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(
        handoff_signing_secret="agentic-handoff-signing-secret-long-enough",
        approval_signing_secret="agentic-approval-signing-secret-long-enough",
        execution_grant_secret="agentic-execution-grant-secret-long-enough",
    )
    with factory() as db:
        engagement = EngagementRecord(name="authorized-operation")
        db.add(engagement)
        db.flush()
        incident = IncidentRecord(
            engagement_id=engagement.id,
            external_id="router-alert",
            severity="high",
            rule_id="profiling",
            summary="Synthetic profiling evidence",
            source_health_json={"logs-edge": {}},
            occurred_at=datetime.now(UTC),
        )
        db.add(incident)
        db.commit()
        handoff = await AgenticNightWatcher(
            db,
            FakeGateway(),
            LocalStructuredModel(),
            settings,
            Path(__file__).parents[1] / "skills",
        ).investigate(incident.id)
        ApprovalService(db, settings).approve(handoff.handoff_id, "operator")
        gateway = RouterGateway()
        execution = await AgenticNightRouter(
            db,
            gateway,
            gateway,
            LocalStructuredModel(),
            settings,
            Path(__file__).parents[1] / "skills",
        ).execute_approved_handoff(handoff.handoff_id, "logical", "old", "new")
        assert execution.state == "completed"
        assert ("infrastructure", "execute_route_switch") in [item[:2] for item in gateway.calls]
        assert ("infrastructure", "verify_callback_route") in [item[:2] for item in gateway.calls]


async def test_research_agent_treats_document_as_untrusted_and_only_proposes() -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        document = KnowledgeDocumentRecord(
            source_id="research-source",
            source_uri="https://source.test/doc",
            title="Research",
            publisher="Publisher",
            content_hash="a" * 64,
            status="quarantined",
            trust_tier="trusted",
            raw_text="Detection guidance using KQL evidence",
            original_text="Detection guidance using KQL evidence",
        )
        db.add(document)
        db.commit()
        gateway = ResearchGateway(document)
        result = await ResearchAgent(
            db,
            gateway,
            LocalStructuredModel(),
            Path(__file__).parents[1] / "skills",
        ).process(document.id)
        assert result.state["candidate"]["status"] == "proposed"
        proposal = gateway.calls[-1][2]
        assert proposal["content"]["untrusted_source"] is True
        assert proposal["citations"][0].endswith(f"sha256={document.content_hash}")


async def test_agentic_nightrouter_rolls_back_when_continuity_is_unknown() -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(
        handoff_signing_secret="agentic-handoff-signing-secret-long-enough",
        approval_signing_secret="agentic-approval-signing-secret-long-enough",
        execution_grant_secret="agentic-execution-grant-secret-long-enough",
    )
    with factory() as db:
        engagement = EngagementRecord(name="authorized-operation")
        db.add(engagement)
        db.flush()
        incident = IncidentRecord(
            engagement_id=engagement.id,
            external_id="rollback-alert",
            severity="high",
            rule_id="profiling",
            summary="Synthetic profiling evidence",
            source_health_json={"logs-edge": {}},
            occurred_at=datetime.now(UTC),
        )
        db.add(incident)
        db.commit()
        handoff = await AgenticNightWatcher(
            db, FakeGateway(), LocalStructuredModel(), settings, Path(__file__).parents[1] / "skills"
        ).investigate(incident.id)
        ApprovalService(db, settings).approve(handoff.handoff_id, "operator")
        gateway = RouterGateway(active_callback=True, verification_status="unknown")
        execution = await AgenticNightRouter(
            db, gateway, gateway, LocalStructuredModel(), settings, Path(__file__).parents[1] / "skills"
        ).execute_approved_handoff(handoff.handoff_id, "logical", "old", "new")
        assert execution.state == "rolled_back"
        assert ("infrastructure", "rollback_route_switch") in [item[:2] for item in gateway.calls]
