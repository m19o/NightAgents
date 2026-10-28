from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from nightagents.db.models import EngagementRecord, KnowledgeCandidateRecord, KnowledgeDocumentRecord
from nightagents.db.repositories import AuditRepository
from nightagents.knowledge.claims import ClaimAnalysisService
from nightagents.mcp_client import MCPGateway
from nightagents.orchestration.runtime import DurableSkillOrchestrator, OrchestrationResult
from nightagents.orchestration.selection import SkillSelector
from nightagents.skills.registry import SkillRegistry


class ResearchAgent:
    def __init__(
        self,
        db: Session,
        gateway: MCPGateway,
        model,
        skills_root: Path | None = None,
    ):
        self.db = db
        registry = SkillRegistry(skills_root or Path("skills"))
        self.orchestrator = DurableSkillOrchestrator(db, registry, gateway, model)
        self.selector = SkillSelector(db, registry, model)
        self.audit = AuditRepository(db)
        self.claims = ClaimAnalysisService(db)

    async def process(self, document_id: str) -> OrchestrationResult:
        document = self.db.get(KnowledgeDocumentRecord, document_id)
        if document is None or document.status != "quarantined":
            raise ValueError("quarantined document required")
        engagement = self.db.scalar(select(EngagementRecord).where(EngagementRecord.name == "__knowledge__"))
        if engagement is None:
            engagement = EngagementRecord(name="__knowledge__", status="system")
            self.db.add(engagement)
            self.db.commit()
        skill = await self.selector.select(
            "research-agent",
            ["process-quarantined-document"],
            engagement.id,
            f"Extract review candidates from document {document.id}",
            {"source_id": document.source_id, "risk_flags": document.metadata_json.get("risk_flags", [])},
        )
        result = await self.orchestrator.run(
            "research-agent",
            skill.skill_id,
            engagement.id,
            f"Extract review candidates from document {document.id}",
            {"document_id": document.id},
        )
        candidate = self.db.scalar(
            select(KnowledgeCandidateRecord).where(KnowledgeCandidateRecord.document_id == document.id)
        )
        if candidate is not None:
            text = candidate.content_json.get("normalized_excerpt", "")
            self.claims.analyze(candidate.id, text, None)
            self.audit.append(
                "research-agent",
                "knowledge.claims_extracted",
                "knowledge_candidate",
                candidate.id,
                {"document_id": document.id, "automatic_promotion": False},
                engagement.id,
            )
            self.db.commit()
        return result
