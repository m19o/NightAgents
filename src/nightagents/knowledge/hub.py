import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from nightagents.db.models import (
    KnowledgeCandidateRecord,
    KnowledgeDocumentRecord,
    KnowledgeSourceRecord,
    MemoryRecord,
    SourceStatusRecord,
)
from nightagents.knowledge.contracts import FetchResult, KnowledgeSource


class KnowledgeHub:
    def __init__(
        self,
        db: Session,
        transport: httpx.AsyncBaseTransport | None = None,
        maximum_document_bytes: int = 2_000_000,
    ):
        self.db = db
        self.transport = transport
        self.maximum_document_bytes = maximum_document_bytes

    def register_source(self, source: KnowledgeSource) -> KnowledgeSourceRecord:
        record = self.db.get(KnowledgeSourceRecord, source.source_id)
        values = source.model_dump(mode="json")
        values["id"] = values.pop("source_id")
        values["source_uri"] = str(values["source_uri"])
        if record is None:
            record = KnowledgeSourceRecord(**values)
            self.db.add(record)
        else:
            for key, value in values.items():
                setattr(record, key, value)
        self.db.flush()
        return record

    def due_sources(self, now: datetime | None = None) -> list[KnowledgeSourceRecord]:
        current = now or datetime.now(UTC)
        sources = list(self.db.scalars(select(KnowledgeSourceRecord).where(KnowledgeSourceRecord.enabled.is_(True))))
        due = []
        for source in sources:
            state = self.db.get(SourceStatusRecord, source.id)
            if state is None or state.next_check_at is None or self._aware(state.next_check_at) <= current:
                due.append(source)
        return due

    async def refresh(self, source_id: str) -> FetchResult:
        source = self.db.get(KnowledgeSourceRecord, source_id)
        now = datetime.now(UTC)
        if source is None or not source.enabled:
            return FetchResult(source_id=source_id, status="disabled", retrieved_at=now)
        state = self.db.get(SourceStatusRecord, source_id) or SourceStatusRecord(source_id=source_id)
        self.db.add(state)
        state.last_checked_at = now
        state.next_check_at = now + timedelta(seconds=source.poll_interval_seconds)
        parsed = urlparse(source.source_uri)
        if parsed.scheme != "https" or parsed.hostname != source.allowed_host:
            return self._failure(source, state, now, "source URI violates allowlist")
        try:
            async with httpx.AsyncClient(
                timeout=20,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                response = await client.get(source.source_uri, headers={"Accept": "text/plain, application/json"})
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if not any(item in content_type for item in ("text/", "json", "xml", "yaml", "octet-stream")):
                    return self._failure(source, state, now, f"unsupported content type: {content_type}")
                body = response.content
            if len(body) > self.maximum_document_bytes:
                return self._failure(source, state, now, "document exceeds size limit")
            digest = hashlib.sha256(body).hexdigest()
            existing = self.db.scalar(
                select(KnowledgeDocumentRecord).where(
                    KnowledgeDocumentRecord.source_id == source_id,
                    KnowledgeDocumentRecord.content_hash == digest,
                )
            )
            state.status = "healthy"
            state.last_success_at = now
            state.consecutive_failures = 0
            state.details_json = {"content_hash": digest}
            if existing:
                self.db.commit()
                return FetchResult(
                    source_id=source_id,
                    status="unchanged",
                    content_hash=digest,
                    document_id=existing.id,
                    retrieved_at=now,
                )
            document = KnowledgeDocumentRecord(
                source_id=source_id,
                source_uri=source.source_uri,
                title=source.id,
                publisher=source.publisher,
                content_hash=digest,
                status="quarantined",
                trust_tier=source.trust_tier,
                license_name=source.license_policy,
                original_text=body.decode("utf-8", errors="replace"),
                raw_text=self._normalize(body, content_type),
                metadata_json={
                    "content_type": content_type,
                    "parser_version": "deterministic-v1",
                    "risk_flags": self._risk_flags(body.decode("utf-8", errors="replace")),
                },
            )
            self.db.add(document)
            self.db.commit()
            return FetchResult(
                source_id=source_id,
                status="changed",
                content_hash=digest,
                document_id=document.id,
                retrieved_at=now,
                changed=True,
            )
        except Exception as exc:
            return self._failure(source, state, now, str(exc))

    def propose_candidate(
        self,
        document_id: str,
        candidate_type: str,
        title: str,
        content: dict,
        citations: list[str],
        confidence: float,
    ) -> KnowledgeCandidateRecord:
        document = self.db.get(KnowledgeDocumentRecord, document_id)
        if document is None or document.status != "quarantined":
            raise ValueError("candidate requires a quarantined document")
        existing = self.db.scalar(
            select(KnowledgeCandidateRecord).where(
                KnowledgeCandidateRecord.document_id == document_id,
                KnowledgeCandidateRecord.candidate_type == candidate_type,
                KnowledgeCandidateRecord.status == "proposed",
            )
        )
        if existing:
            return existing
        candidate = KnowledgeCandidateRecord(
            document_id=document_id,
            candidate_type=candidate_type,
            title=title,
            content_json=content,
            citations_json=citations,
            confidence=confidence,
        )
        self.db.add(candidate)
        self.db.commit()
        return candidate

    def propose_default_candidates(self, document_id: str) -> list[KnowledgeCandidateRecord]:
        document = self.db.get(KnowledgeDocumentRecord, document_id)
        if document is None or document.status != "quarantined":
            raise ValueError("candidate extraction requires a quarantined document")
        existing = list(
            self.db.scalars(
                select(KnowledgeCandidateRecord).where(KnowledgeCandidateRecord.document_id == document_id)
            )
        )
        if existing:
            return existing
        lowered = document.raw_text.lower()
        candidate_type = "DETECTION_QUERY" if any(
            token in lowered for token in ("detect", "query", "kql", "sigma", "telemetry")
        ) else "KNOWLEDGE_CLAIM"
        title = next((line[:200] for line in document.raw_text.splitlines() if line.strip()), document.title[:200])
        risk_flags = document.metadata_json.get("risk_flags", [])
        confidence = max(self._trust_score(document.trust_tier) - (0.3 if risk_flags else 0), 0.1)
        candidate = KnowledgeCandidateRecord(
            document_id=document.id,
            candidate_type=candidate_type,
            title=title,
            content_json={
                "normalized_excerpt": document.raw_text[:4000],
                "content_hash": document.content_hash,
                "risk_flags": risk_flags,
                "promotion_effect": "none_until_separate_review",
            },
            citations_json=[f"{document.source_uri}#sha256={document.content_hash}"],
            confidence=confidence,
        )
        self.db.add(candidate)
        self.db.commit()
        return [candidate]

    def approve_document(self, document_id: str, reviewer: str) -> KnowledgeDocumentRecord:
        return self.review_document(document_id, reviewer, "approve", False, "")

    def review_document(
        self,
        document_id: str,
        reviewer: str,
        decision: str,
        risk_acceptance: bool,
        notes: str,
    ) -> KnowledgeDocumentRecord:
        document = self.db.get(KnowledgeDocumentRecord, document_id)
        if document is None:
            raise ValueError("document not found")
        if document.status != "quarantined":
            raise ValueError("only quarantined documents can be reviewed")
        if decision == "reject":
            document.status = "rejected"
            document.metadata_json = document.metadata_json | {
                "reviewed_by": reviewer,
                "reviewed_at": datetime.now(UTC).isoformat(),
                "review_notes": notes,
            }
            self.db.commit()
            return document
        risk_flags = document.metadata_json.get("risk_flags", [])
        if risk_flags and not risk_acceptance:
            raise ValueError("document has risk flags; explicit risk acceptance is required")
        now = datetime.now(UTC)
        superseded = list(
            self.db.scalars(
                select(KnowledgeDocumentRecord).where(
                    KnowledgeDocumentRecord.source_id == document.source_id,
                    KnowledgeDocumentRecord.status == "active",
                    KnowledgeDocumentRecord.id != document.id,
                )
            )
        )
        superseded_ids = []
        for prior in superseded:
            prior.status = "superseded"
            prior.valid_until = now
            superseded_ids.append(prior.id)
            memories = self.db.scalars(select(MemoryRecord).where(MemoryRecord.active.is_(True)))
            for memory in memories:
                if memory.metadata_json.get("document_id") == prior.id:
                    memory.active = False
                    memory.expires_at = now
        document.status = "active"
        document.metadata_json = document.metadata_json | {
            "reviewed_by": reviewer,
            "reviewed_at": now.isoformat(),
            "review_notes": notes,
            "risk_accepted": bool(risk_flags and risk_acceptance),
            "supersedes": superseded_ids,
        }
        memory = MemoryRecord(
            memory_type="semantic",
            content=document.raw_text,
            source_reference=document.source_uri,
            trust_score=self._trust_score(document.trust_tier),
            active=True,
            metadata_json={"document_id": document.id, "content_hash": document.content_hash},
        )
        self.db.add(memory)
        self.db.commit()
        return document

    def review_candidate(
        self, candidate_id: str, reviewer: str, decision: str, notes: str
    ) -> KnowledgeCandidateRecord:
        candidate = self.db.get(KnowledgeCandidateRecord, candidate_id)
        if candidate is None:
            raise ValueError("candidate not found")
        if candidate.status != "proposed":
            raise ValueError("candidate already reviewed")
        candidate.status = "approved" if decision == "approve" else "rejected"
        candidate.content_json = candidate.content_json | {
            "review": {"reviewer": reviewer, "notes": notes, "reviewed_at": datetime.now(UTC).isoformat()}
        }
        self.db.commit()
        return candidate

    def _failure(
        self, source: KnowledgeSourceRecord, state: SourceStatusRecord, now: datetime, error: str
    ) -> FetchResult:
        state.status = "unavailable"
        state.consecutive_failures += 1
        state.details_json = {"error": error}
        self.db.commit()
        return FetchResult(source_id=source.id, status="unavailable", retrieved_at=now, error=error)

    @staticmethod
    def _trust_score(tier: str) -> float:
        return {"authoritative": 1.0, "trusted": 0.8, "community": 0.6}.get(tier, 0.4)

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    @staticmethod
    def _normalize(body: bytes, content_type: str) -> str:
        text = body.decode("utf-8", errors="replace").replace("\x00", "")
        if "json" not in content_type:
            return "\n".join(line.strip() for line in text.splitlines() if line.strip())
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return text
        strings: list[str] = []

        def visit(item) -> None:
            if isinstance(item, dict):
                for key, nested in item.items():
                    if key.lower() not in {"binary", "content_base64", "attachment"}:
                        visit(nested)
            elif isinstance(item, list):
                for nested in item:
                    visit(nested)
            elif isinstance(item, str):
                strings.append(item)

        visit(value)
        return "\n".join(strings)

    @staticmethod
    def _risk_flags(text: str) -> list[str]:
        patterns = {
            "prompt_override": r"\b(ignore|disregard)\s+(all\s+)?(previous|prior|system)\b",
            "tool_instruction": r"\b(call|invoke|execute|run)\s+(the\s+)?(tool|command|shell)\b",
            "active_content": r"<script\b|javascript:",
            "secret_request": r"\b(reveal|print|return)\s+(the\s+)?(secret|token|api key|password)\b",
        }
        lowered = text.lower()
        return [name for name, pattern in patterns.items() if re.search(pattern, lowered)]
