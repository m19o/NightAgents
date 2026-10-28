import hashlib
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from nightagents.db.models import ClaimRecord, MemoryRecord
from nightagents.knowledge.memory import MemoryService


class ClaimAnalysisService:
    """Extract atomic claims from a knowledge candidate and compare them to active governed memory.

    Labels each claim relationship as support, contradiction, or unknown. Never promotes knowledge
    automatically; all candidates remain review-only.
    """

    CONTRADICTION_TERMS = {"not", "never", "no", "false", "benign", "ignore", "safe"}

    def __init__(self, db: Session):
        self.db = db
        self.memory = MemoryService(db)

    def extract_claims(self, candidate_text: str) -> list[str]:
        sentences = re.split(r"(?<=[.!?])\s+", candidate_text)
        return [sentence.strip() for sentence in sentences if len(sentence.strip()) > 10]

    def analyze(self, candidate_id: str, candidate_text: str, engagement_id: str | None) -> list[ClaimRecord]:
        claims = self.extract_claims(candidate_text)
        records: list[ClaimRecord] = []
        for claim_text in claims:
            signature = hashlib.sha256(claim_text.lower().encode()).hexdigest()[:16]
            matches = self.memory.search(claim_text, engagement_id, memory_types=["semantic"], limit=5)
            best = matches[0] if matches else None
            relationship = "unknown"
            diff = ""
            memory_id = None
            if best is not None:
                memory_id = best.memory_id
                relationship = self._classify(claim_text, best.content)
                diff = self._semantic_diff(claim_text, best.content)
            record = ClaimRecord(
                candidate_id=candidate_id,
                claim_text=claim_text,
                lexical_signature=signature,
                compared_memory_id=memory_id,
                relationship=relationship,
                semantic_diff=diff,
                status="review",
            )
            self.db.add(record)
            records.append(record)
        self.db.flush()
        return records

    def _classify(self, claim: str, memory_content: str) -> str:
        claim_lower = claim.lower()
        memory_lower = memory_content.lower()
        claim_negated = bool(self.CONTRADICTION_TERMS & set(claim_lower.split()))
        memory_negated = bool(self.CONTRADICTION_TERMS & set(memory_lower.split()))
        if claim_negated != memory_negated:
            return "contradiction"
        shared_terms = self.memory._tokens(claim) & self.memory._tokens(memory_content)
        if shared_terms:
            return "support"
        return "unknown"

    def _semantic_diff(self, claim: str, memory_content: str) -> str:
        claim_terms = self.memory._tokens(claim)
        memory_terms = self.memory._tokens(memory_content)
        added = claim_terms - memory_terms
        removed = memory_terms - claim_terms
        parts = []
        if added:
            parts.append(f"new terms: {', '.join(sorted(added))}")
        if removed:
            parts.append(f"missing terms: {', '.join(sorted(removed))}")
        return "; ".join(parts) or "no lexical diff"

    def active_memory_for_candidate(self, candidate_id: str) -> list[MemoryRecord]:
        return list(
            self.db.scalars(
                select(MemoryRecord).where(
                    MemoryRecord.id.in_(
                        select(ClaimRecord.compared_memory_id).where(
                            ClaimRecord.candidate_id == candidate_id,
                            ClaimRecord.compared_memory_id.isnot(None),
                        )
                    )
                )
            )
        )
