from nightagents.contracts.common import Capability
from nightagents.db.models import KnowledgeDocumentRecord
from nightagents.db.session import SessionLocal
from nightagents.knowledge.hub import KnowledgeHub
from nightagents.knowledge.memory import MemoryService
from nightagents.mcp_servers.compat import Context, MCPServer
from nightagents.mcp_servers.runtime import run_server
from nightagents.security.authorization import CapabilityPolicy
from nightagents.security.mcp_auth_config import mcp_auth_kwargs
from nightagents.security.mcp_identity import principal_from_context


def build_server() -> MCPServer:
    server = MCPServer(
        "nightagents-memory",
        instructions="Provenance-preserving semantic and episodic memory.",
        **mcp_auth_kwargs(),
    )

    @server.tool()
    def search_memory(
        query: str,
        engagement_id: str | None = None,
        memory_types: list[str] | None = None,
        limit: int = 8,
        ctx: Context | None = None,
    ) -> list[dict]:
        """Search active, non-expired semantic and engagement-scoped episodic memories."""
        if ctx is not None:
            CapabilityPolicy().require(principal_from_context(ctx), Capability.READ)
        with SessionLocal() as db:
            results = MemoryService(db).search(query, engagement_id, memory_types, limit)
            return [item.model_dump(mode="json") for item in results]

    @server.tool()
    def record_feedback(engagement_id: str, content: str, source_reference: str, metadata: dict, ctx: Context) -> dict:
        """Record authenticated operator or outcome feedback as episodic memory."""
        CapabilityPolicy().require(principal_from_context(ctx), Capability.PLAN)
        with SessionLocal() as db:
            memory = MemoryService(db).remember_episode(engagement_id, content, source_reference, metadata)
            return {"memory_id": memory.id, "status": "active"}

    @server.tool()
    def get_quarantined_document(document_id: str, ctx: Context) -> dict:
        """Read one quarantined document as explicitly untrusted research input."""
        CapabilityPolicy().require(principal_from_context(ctx), Capability.READ)
        with SessionLocal() as db:
            document = db.get(KnowledgeDocumentRecord, document_id)
            if document is None or document.status != "quarantined":
                raise ValueError("quarantined document not found")
            return {
                "document_id": document.id,
                "source_id": document.source_id,
                "source_uri": document.source_uri,
                "content_hash": document.content_hash,
                "trust_tier": document.trust_tier,
                "risk_flags": document.metadata_json.get("risk_flags", []),
                "content_untrusted": document.raw_text[:20000],
            }

    @server.tool()
    def propose_knowledge_candidate(
        document_id: str,
        candidate_type: str,
        title: str,
        content: dict,
        citations: list[str],
        confidence: float,
        conflict_review: dict,
        ctx: Context,
    ) -> dict:
        """Create a review-only candidate; this tool cannot activate knowledge."""
        CapabilityPolicy().require(principal_from_context(ctx), Capability.PLAN)
        with SessionLocal() as db:
            candidate = KnowledgeHub(db).propose_candidate(
                document_id,
                candidate_type,
                title,
                content | {"conflict_review": conflict_review},
                citations,
                confidence,
            )
            return {"candidate_id": candidate.id, "status": candidate.status}

    return server


def main() -> None:
    run_server(build_server())
