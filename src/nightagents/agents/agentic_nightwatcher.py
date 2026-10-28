import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from nightagents.config import Settings
from nightagents.contracts.agent import ActionRecommendation, AgentHandoff, Disposition, Prediction
from nightagents.contracts.common import EvidenceRef
from nightagents.db.models import EngagementRecord, HandoffRecord, IncidentRecord, PredictionRecord
from nightagents.db.repositories import AuditRepository, OutboxRepository
from nightagents.mcp_client import MCPGateway
from nightagents.operational import OperationalMetrics
from nightagents.orchestration.runtime import DurableSkillOrchestrator
from nightagents.orchestration.selection import SkillSelector
from nightagents.security.signing import content_hash, sign_hash
from nightagents.skills.registry import SkillRegistry


class AgenticNightWatcher:
    def __init__(
        self,
        db: Session,
        gateway: MCPGateway,
        model,
        settings: Settings,
        skills_root: Path | None = None,
    ):
        self.db = db
        self.settings = settings
        root = skills_root or Path("skills")
        self.skills_root = root
        registry = SkillRegistry(root)
        self.orchestrator = DurableSkillOrchestrator(db, registry, gateway, model)
        self.selector = SkillSelector(db, registry, model)
        self.audit = AuditRepository(db)
        self.outbox = OutboxRepository(db)

    async def investigate(self, incident_id: str) -> AgentHandoff | dict:
        incident = self.db.get(IncidentRecord, incident_id)
        if incident is None:
            raise ValueError("incident not found")
        engagement = self.db.get(EngagementRecord, incident.engagement_id)
        if engagement is None:
            raise ValueError("engagement not found")
        required_sources = list(incident.source_health_json) or ["logs-nginx.access-*"]
        initial = {
            "engagement_id": engagement.id,
            "engagement_name": engagement.name,
            "external_alert_id": incident.external_id,
            "redirector_id": incident.redirector_asset_id or "unknown",
            "required_sources": required_sources,
        }
        investigation_skill = await self.selector.select(
            "nightwatcher",
            ["investigate-elastic-incident"],
            engagement.id,
            f"Investigate incident {incident.id}",
            {"severity": incident.severity, "required_sources": required_sources},
        )
        investigation = await self.orchestrator.run(
            "nightwatcher",
            investigation_skill.skill_id,
            engagement.id,
            f"Investigate incident {incident.id}",
            initial,
        )
        verification = investigation.state["verification"]
        if not verification.get("supported"):
            return {
                "disposition": "request_evidence",
                "run_id": investigation.run_id,
                "unknowns": verification.get("unknowns", []),
            }
        prediction_state = {
            **investigation.state,
            "engagement_id": engagement.id,
            "summary": f"{incident.rule_id} {incident.summary}",
            "incident": {
                "id": incident.id,
                "severity": incident.severity,
                "rule_id": incident.rule_id,
                "summary": incident.summary,
            },
        }
        prediction_skill = await self.selector.select(
            "nightwatcher",
            ["predict-blue-team-chain"],
            engagement.id,
            f"Predict progression for incident {incident.id}",
            {"verification": verification, "rule_id": incident.rule_id},
        )
        prediction_run = await self.orchestrator.run(
            "nightwatcher",
            prediction_skill.skill_id,
            engagement.id,
            f"Predict progression for incident {incident.id}",
            prediction_state,
        )
        critique = prediction_run.state["critique"]
        # A real LLM critic will always find nitpicks; abstain only when the
        # critic's overall verdict says the assessment is fundamentally
        # unsupported. Fall back to the legacy rule when the verdict is absent.
        fundamentally_supported = critique.get(
            "fundamentally_supported", not critique.get("unsupported_claims")
        )
        if not fundamentally_supported:
            return {
                "disposition": "abstain",
                "run_id": prediction_run.run_id,
                "critique": critique,
            }
        handoff, prediction_record = self._handoff(incident, prediction_run.state, verification)
        record = HandoffRecord(
            id=handoff.handoff_id,
            engagement_id=incident.engagement_id,
            incident_id=incident.id,
            payload_json=handoff.model_dump(mode="json", exclude={"signature"}),
            payload_hash=handoff.payload_hash,
            signature=handoff.signature,
            expires_at=handoff.expires_at,
        )
        self.db.add(record)
        prediction_record.handoff_id = record.id
        self.db.add(prediction_record)
        self.outbox.add("handoff", record.id, "handoff.proposed", {"handoff_id": record.id})
        self.audit.append(
            "nightwatcher",
            "handoff.proposed",
            "handoff",
            record.id,
            {
                "hash": record.payload_hash,
                "investigation_run_id": investigation.run_id,
                "prediction_run_id": prediction_run.run_id,
                "prediction_id": prediction_record.id,
            },
            incident.engagement_id,
        )
        self.db.commit()
        return handoff

    def _handoff(
        self, incident: IncidentRecord, state: dict, verification: dict
    ) -> tuple[AgentHandoff, PredictionRecord]:
        created_at = datetime.now(UTC)
        reasoning = state["reasoning"]
        prediction = Prediction.model_validate(state["prediction"])
        evidence = self._evidence(incident, state.get("elastic_evidence", {}))
        unsigned = {
            "handoff_version": "1.0",
            "handoff_id": str(uuid4()),
            "from_agent": "nightwatcher",
            "to_agent": "nightrouter",
            "engagement_id": incident.engagement_id,
            "incident_id": incident.id,
            "phase": reasoning["phase"],
            "confidence": reasoning["confidence"],
            "disposition": Disposition.PROPOSE.value,
            "prediction": prediction.model_dump(mode="json"),
            "recommendation": ActionRecommendation(
                skill=reasoning["recommended_skill"],
                redirector_id=incident.redirector_asset_id or "unknown",
                parameters={"incident_id": incident.id},
            ).model_dump(mode="json"),
            "evidence": [item.model_dump(mode="json") for item in evidence],
            "unknowns": verification.get("unknowns", []),
            "created_at": created_at.isoformat(),
            "expires_at": (created_at + timedelta(minutes=self.settings.handoff_ttl_minutes)).isoformat(),
        }
        digest = content_hash(unsigned)
        handoff = AgentHandoff.model_validate(
            unsigned | {"payload_hash": digest, "signature": sign_hash(self.settings.handoff_signing_secret, digest)}
        )
        prediction_record = PredictionRecord(
            id=str(uuid4()),
            handoff_id=handoff.handoff_id,
            engagement_id=incident.engagement_id,
            predicted_event=prediction.event,
            predicted_probability=prediction.probability,
            horizon_seconds=prediction.horizon_seconds,
            confirmation_query_id=prediction.confirmation_query_id,
            expires_at=created_at + timedelta(seconds=prediction.horizon_seconds),
        )
        return handoff, prediction_record

    async def hunt(self, engagement_id: str) -> dict:
        """Agent-driven monitoring: the agent hunts redirector telemetry itself
        instead of relying on a deterministic detection engine.

        Deterministic guardrails (models propose, code owns safety): the
        source-health gate runs in code before any model token is spent -
        absence of evidence is never a negative outcome; findings without
        elastic evidence ids are discarded; incident external ids are content
        hashes, so repeat hunts are idempotent.
        """
        import json as _json

        engagement = self.db.get(EngagementRecord, engagement_id)
        if engagement is None:
            raise ValueError("engagement not found")
        metrics = OperationalMetrics(self.db)
        required_sources = [self.settings.detection_source]
        source_health = await self.orchestrator.gateway.call_tool(
            "elastic", "get_source_health", {"source_ids": required_sources}
        )
        if not source_health or any(state.get("status") != "healthy" for state in source_health.values()):
            metrics.record("hunt.skipped_unhealthy_source", 1.0, {"engagement_id": engagement.id})
            self.db.commit()
            return {
                "engagement_id": engagement.id,
                "skipped": "unhealthy_source",
                "findings": 0,
                "opened": 0,
                "duplicates": 0,
                "discarded": 0,
                "incident_ids": [],
            }
        playbook = (self.skills_root / "nightwatcher" / "hunt-redirector-telemetry" / "playbook.md").read_text()
        objective = f"Hunt redirector telemetry for engagement {engagement.name}"
        initial = {
            "engagement_id": engagement.id,
            "engagement_name": engagement.name,
            "required_sources": required_sources,
            "playbook": playbook,
        }
        hunt_skill = await self.selector.select(
            "nightwatcher",
            ["hunt-redirector-telemetry"],
            engagement.id,
            objective,
            {"required_sources": required_sources},
        )
        run = await self.orchestrator.run(
            "nightwatcher",
            hunt_skill.skill_id,
            engagement.id,
            objective,
            initial,
        )
        correlation = run.state.get("correlation") or {}
        findings = correlation.get("findings") or []
        opened = 0
        duplicates = 0
        discarded = 0
        incident_ids: list[str] = []
        for finding in findings:
            evidence_ids = sorted(str(item) for item in finding.get("evidence_ids") or [])
            if not evidence_ids:
                discarded += 1
                metrics.record("hunt.unsupported_finding_discarded", 1.0, {"engagement_id": engagement.id})
                continue
            digest = hashlib.sha256(
                _json.dumps([engagement.id, str(finding.get("title", "")), evidence_ids]).encode()
            ).hexdigest()[:24]
            external_id = f"hunt-{digest}"
            existing = self.db.scalar(
                select(IncidentRecord).where(
                    IncidentRecord.engagement_id == engagement.id,
                    IncidentRecord.external_id == external_id,
                )
            )
            if existing is not None:
                duplicates += 1
                continue
            incident = IncidentRecord(
                engagement_id=engagement.id,
                external_id=external_id,
                severity=str(finding.get("severity", "medium")),
                rule_id="agentic-hunt",
                summary=f"{finding.get('title')}: {finding.get('reasoning', '')}",
                evidence_refs_json=[{"source": "elastic", "evidence_id": item} for item in evidence_ids],
                source_health_json=required_sources,
                occurred_at=datetime.now(UTC),
            )
            self.db.add(incident)
            self.db.flush()
            self.audit.append(
                "nightwatcher",
                "incident.opened",
                "incident",
                incident.id,
                {
                    "external_id": external_id,
                    "confidence": finding.get("confidence"),
                    "evidence_ids": evidence_ids,
                    "hunt_run_id": run.run_id,
                },
                engagement_id=engagement.id,
            )
            opened += 1
            incident_ids.append(incident.id)
        metrics.record("hunt.incidents_opened", float(opened), {"engagement_id": engagement.id})
        self.db.commit()
        return {
            "engagement_id": engagement.id,
            "findings": len(findings),
            "opened": opened,
            "duplicates": duplicates,
            "discarded": discarded,
            "incident_ids": incident_ids,
        }

    @staticmethod
    def _evidence(incident: IncidentRecord, elastic: dict) -> list[EvidenceRef]:
        return [
            EvidenceRef(
                evidence_id=str(event.get("_id", index)),
                source="elastic",
                reference=f"{incident.external_id}:{event.get('_id', index)}",
                observed_at=incident.occurred_at,
                confidence=1.0,
            )
            for index, event in enumerate(elastic.get("events", [])[:100])
        ]
