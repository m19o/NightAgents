from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nightagents.db.models import (
    AgentRunRecord,
    AgentStepRecord,
    AuditChainHeadRecord,
    AuditEventRecord,
    OutboxEventRecord,
)
from nightagents.security.signing import content_hash


class AuditRepository:
    def __init__(self, db: Session):
        self.db = db

    def append(
        self,
        actor: str,
        event_type: str,
        target_type: str,
        target_id: str,
        details: dict,
        engagement_id: str | None = None,
    ) -> AuditEventRecord:
        head = self.db.scalar(
            select(AuditChainHeadRecord).where(AuditChainHeadRecord.id == "global").with_for_update()
        )
        if head is None:
            head = AuditChainHeadRecord(id="global")
            self.db.add(head)
            self.db.flush()
        previous_hash = head.event_hash
        created_at = datetime.now(UTC)
        digest = content_hash(
            {
                "previous_hash": previous_hash,
                "actor": actor,
                "event_type": event_type,
                "target_type": target_type,
                "target_id": target_id,
                "details": details,
                "engagement_id": engagement_id,
                "created_at": created_at.isoformat(),
            }
        )
        event = AuditEventRecord(
            engagement_id=engagement_id,
            actor=actor,
            event_type=event_type,
            target_type=target_type,
            target_id=target_id,
            details_json=details,
            previous_hash=previous_hash,
            event_hash=digest,
            created_at=created_at,
        )
        self.db.add(event)
        head.event_hash = digest
        head.version += 1
        head.updated_at = created_at
        return event

    def verify_chain(self) -> dict:
        events = list(self.db.scalars(select(AuditEventRecord)))
        if not events:
            return {"valid": True, "events": 0, "head": ""}
        by_previous: dict[str, list[AuditEventRecord]] = {}
        for event in events:
            by_previous.setdefault(event.previous_hash, []).append(event)
            expected = content_hash(
                {
                    "previous_hash": event.previous_hash,
                    "actor": event.actor,
                    "event_type": event.event_type,
                    "target_type": event.target_type,
                    "target_id": event.target_id,
                    "details": event.details_json,
                    "engagement_id": event.engagement_id,
                    "created_at": self._iso(event.created_at),
                }
            )
            if expected != event.event_hash:
                return {"valid": False, "events": len(events), "reason": f"hash mismatch at {event.id}"}
        current = ""
        visited: set[str] = set()
        while current in by_previous:
            children = by_previous[current]
            if len(children) != 1:
                return {"valid": False, "events": len(events), "reason": f"audit fork after {current}"}
            event = children[0]
            if event.event_hash in visited:
                return {"valid": False, "events": len(events), "reason": "audit cycle detected"}
            visited.add(event.event_hash)
            current = event.event_hash
        if len(visited) != len(events):
            return {"valid": False, "events": len(events), "reason": "detached audit events"}
        head = self.db.get(AuditChainHeadRecord, "global")
        if head is None or head.event_hash != current or head.version != len(events):
            return {"valid": False, "events": len(events), "reason": "audit head mismatch"}
        return {"valid": True, "events": len(events), "head": current}

    @staticmethod
    def _iso(value: datetime) -> str:
        aware = value if value.tzinfo else value.replace(tzinfo=UTC)
        return aware.astimezone(UTC).isoformat()


class WorkflowRepository:
    def __init__(self, db: Session):
        self.db = db

    def create_run(self, engagement_id: str, agent: str, objective: str, budget: dict) -> AgentRunRecord:
        run = AgentRunRecord(engagement_id=engagement_id, agent=agent, objective=objective, budget_json=budget)
        self.db.add(run)
        self.db.flush()
        return run

    def checkpoint(self, run: AgentRunRecord, node: str, state: dict) -> None:
        run.current_node = node
        run.state_json = state
        run.updated_at = datetime.now(UTC)

    def add_step(
        self,
        run: AgentRunRecord,
        node: str,
        status: str,
        input_data: dict,
        output_data: dict,
        skill_id: str | None = None,
        model_role: str | None = None,
        model_id: str | None = None,
        model_version: str | None = None,
        prompt_version: str | None = None,
    ) -> AgentStepRecord:
        sequence = self.db.scalar(
            select(func.count()).select_from(AgentStepRecord).where(AgentStepRecord.run_id == run.id)
        )
        step = AgentStepRecord(
            run_id=run.id,
            sequence=int(sequence or 0) + 1,
            node=node,
            skill_id=skill_id,
            model_role=model_role,
            model_id=model_id,
            model_version=model_version,
            prompt_version=prompt_version,
            input_json=input_data,
            output_json=output_data,
            status=status,
        )
        self.db.add(step)
        return step

    def complete(self, run: AgentRunRecord, status: str, state: dict) -> None:
        run.status = status
        run.state_json = state
        run.completed_at = datetime.now(UTC)


class OutboxRepository:
    def __init__(self, db: Session):
        self.db = db

    def add(self, aggregate_type: str, aggregate_id: str, event_type: str, payload: dict) -> OutboxEventRecord:
        event = OutboxEventRecord(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload_json=payload,
        )
        self.db.add(event)
        return event
