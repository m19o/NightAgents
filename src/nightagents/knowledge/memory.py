import re
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from nightagents.db.models import MemoryRecord
from nightagents.knowledge.contracts import MemorySearchResult


class MemoryService:
    def __init__(self, db: Session):
        self.db = db

    def remember_episode(
        self,
        engagement_id: str,
        content: str,
        source_reference: str,
        metadata: dict | None = None,
        commit: bool = True,
        memory_type: str = "episodic",
        trust_score: float = 1.0,
    ) -> MemoryRecord:
        memory = MemoryRecord(
            engagement_id=engagement_id,
            memory_type=memory_type,
            content=content,
            source_reference=source_reference,
            trust_score=trust_score,
            active=True,
            metadata_json=metadata or {},
        )
        self.db.add(memory)
        if commit:
            self.db.commit()
        else:
            self.db.flush()
        return memory

    def search(
        self,
        query: str,
        engagement_id: str | None,
        memory_types: list[str] | None = None,
        limit: int = 8,
    ) -> list[MemorySearchResult]:
        if limit < 1 or limit > 20:
            raise ValueError("memory result limit outside bounds")
        statement = select(MemoryRecord).where(
            MemoryRecord.active.is_(True),
            or_(MemoryRecord.expires_at.is_(None), MemoryRecord.expires_at > datetime.now(UTC)),
            or_(MemoryRecord.engagement_id.is_(None), MemoryRecord.engagement_id == engagement_id),
        )
        if memory_types:
            statement = statement.where(MemoryRecord.memory_type.in_(memory_types))
        terms = self._tokens(query)
        scored = []
        for record in self.db.scalars(statement):
            document_terms = self._tokens(record.content)
            overlap = len(terms & document_terms) / max(len(terms), 1)
            score = overlap * 0.7 + float(record.trust_score) * 0.3
            if overlap:
                scored.append((score, record))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            MemorySearchResult(
                memory_id=record.id,
                memory_type=record.memory_type,
                content=record.content,
                source_reference=record.source_reference,
                score=round(score, 4),
                metadata=record.metadata_json,
            )
            for score, record in scored[:limit]
        ]

    @staticmethod
    def _tokens(value: str) -> set[str]:
        return {item for item in re.findall(r"[a-z0-9_-]{3,}", value.lower())}
