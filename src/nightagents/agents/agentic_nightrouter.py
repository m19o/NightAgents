from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from nightagents.config import Settings
from nightagents.contracts.infrastructure import InfrastructurePlan, InfrastructureResult
from nightagents.contracts.mythic import CallbackInventory, RouteVerification
from nightagents.db.models import ApprovalRecord, EngagementRecord, ExecutionRecord, ExecutionStepRecord, HandoffRecord
from nightagents.db.repositories import AuditRepository, OutboxRepository
from nightagents.mcp_client import MCPGateway, RecordedMCPGateway
from nightagents.orchestration.runtime import DurableSkillOrchestrator
from nightagents.orchestration.selection import SkillSelector
from nightagents.safety.kernel import SafetyKernel, Transition
from nightagents.security.signing import content_hash, verify_hash_signature
from nightagents.skills.registry import SkillRegistry


class AgenticNightRouter:
    def __init__(
        self,
        db: Session,
        planning_gateway: MCPGateway,
        execution_gateway: MCPGateway,
        model,
        settings: Settings,
        skills_root: Path | None = None,
    ):
        self.db = db
        self.settings = settings
        self.kernel = SafetyKernel()
        root = skills_root or Path("skills")
        registry = SkillRegistry(root)
        self.orchestrator = DurableSkillOrchestrator(db, registry, planning_gateway, model)
        self.selector = SkillSelector(db, registry, model)
        self.execution_gateway = execution_gateway
        self.audit = AuditRepository(db)
        self.outbox = OutboxRepository(db)

    async def execute_approved_handoff(
        self,
        handoff_id: str,
        logical_route_id: str,
        old_redirector_id: str,
        new_redirector_id: str,
    ) -> ExecutionRecord:
        handoff, engagement = self._approved_handoff(handoff_id)
        planning_skill = await self.selector.select(
            "nightrouter",
            ["plan-callback-migration"],
            engagement.id,
            f"Plan approved handoff {handoff.id}",
            {"old": old_redirector_id, "new": new_redirector_id},
        )
        planning = await self.orchestrator.run(
            "nightrouter",
            planning_skill.skill_id,
            engagement.id,
            f"Plan approved handoff {handoff.id}",
            {
                "engagement_id": engagement.id,
                "engagement_name": engagement.name,
                "logical_route_id": logical_route_id,
                "old_redirector_id": old_redirector_id,
                "new_redirector_id": new_redirector_id,
                "handoff": handoff.payload_json,
            },
        )
        if not planning.state["safety_review"].get("supported"):
            return self._failed_safe(handoff, "planning critic rejected the plan")
        inventory = CallbackInventory.model_validate(planning.state["callback_inventory"])
        plan = InfrastructurePlan.model_validate(planning.state["plan"])
        precheck = self.kernel.authorize_transition(Transition.PREPARE_FAILOVER, inventory, None, handoff.expires_at)
        if not precheck.allowed:
            return self._failed_safe(handoff, precheck.reason)
        execution = ExecutionRecord(
            engagement_id=engagement.id,
            handoff_id=handoff.id,
            plan_json=plan.model_dump(mode="json"),
            plan_hash=content_hash(plan.model_dump(mode="json")),
            state="prechecking",
            rollback_json={"old_redirector_id": old_redirector_id},
        )
        self.db.add(execution)
        self.db.flush()
        grant = self.kernel.issue_execution_grant(
            self.settings.execution_grant_secret,
            handoff.payload_hash,
            engagement.id,
            "infrastructure.execute_route_switch",
            plan.plan_id,
            plan.expires_at,
        )
        step = ExecutionStepRecord(
            execution_id=execution.id,
            transition="switch_logical_route",
            idempotency_key=f"{execution.id}:switch",
            request_json={"plan_id": plan.plan_id, "grant_id": grant.grant_id},
        )
        self.db.add(step)
        self.db.commit()
        executor = RecordedMCPGateway(self.db, planning.run_id, self.execution_gateway)
        result_data = await executor.call_tool(
            "infrastructure",
            "execute_route_switch",
            {
                "plan": plan.model_dump(mode="json"),
                "grant": grant.model_dump(mode="json"),
                "idempotency_key": step.idempotency_key,
            },
        )
        result = InfrastructureResult.model_validate(result_data)
        step.result_json = result.model_dump(mode="json")
        step.status = "completed" if result.success else "failed"
        step.completed_at = datetime.now(UTC)
        if not result.success:
            execution.state = "failed_safe"
            self.db.commit()
            return execution
        execution.state = "observing"
        callback_ids = [item.callback_id for item in inventory.callbacks if item.active]
        verification_skill = await self.selector.select(
            "nightrouter",
            ["verify-callback-continuity"],
            engagement.id,
            f"Verify route continuity for execution {execution.id}",
            {"callback_count": len(callback_ids), "route_id": new_redirector_id},
        )
        continuity = await self.orchestrator.run(
            "nightrouter",
            verification_skill.skill_id,
            engagement.id,
            f"Verify route continuity for execution {execution.id}",
            {
                "engagement_id": engagement.id,
                "engagement_name": engagement.name,
                "route_id": new_redirector_id,
                "callback_ids": callback_ids,
            },
        )
        verification = RouteVerification.model_validate(continuity.state["callback_verification"])
        drain = self.kernel.authorize_transition(
            Transition.DRAIN_OLD_ROUTE, inventory, verification, handoff.expires_at
        )
        if not drain.allowed:
            await self._rollback(execution, plan, handoff.payload_hash, continuity.run_id)
            return execution
        execution.state = "completed"
        self.outbox.add("execution", execution.id, "execution.completed", {"execution_id": execution.id})
        self.audit.append(
            "nightrouter",
            "execution.completed",
            "execution",
            execution.id,
            {"plan_hash": execution.plan_hash, "planning_run_id": planning.run_id},
            execution.engagement_id,
        )
        self.db.commit()
        return execution

    def _approved_handoff(self, handoff_id: str) -> tuple[HandoffRecord, EngagementRecord]:
        handoff = self.db.get(HandoffRecord, handoff_id)
        if handoff is None or handoff.status != "approved":
            raise ValueError("approved handoff required")
        approval = self.db.scalar(
            select(ApprovalRecord).where(
                ApprovalRecord.handoff_id == handoff.id,
                ApprovalRecord.decision == "approved",
            )
        )
        if approval is None or approval.handoff_hash != handoff.payload_hash:
            raise ValueError("approval does not bind current handoff")
        if not verify_hash_signature(self.settings.approval_signing_secret, handoff.payload_hash, approval.signature):
            raise ValueError("invalid approval signature")
        expires = handoff.expires_at if handoff.expires_at.tzinfo else handoff.expires_at.replace(tzinfo=UTC)
        if expires <= datetime.now(UTC):
            raise ValueError("handoff expired")
        engagement = self.db.get(EngagementRecord, handoff.engagement_id)
        if engagement is None:
            raise ValueError("engagement not found")
        return handoff, engagement

    async def _rollback(
        self, execution: ExecutionRecord, plan: InfrastructurePlan, proposal_hash: str, run_id: str
    ) -> None:
        grant = self.kernel.issue_execution_grant(
            self.settings.execution_grant_secret,
            proposal_hash,
            execution.engagement_id,
            "infrastructure.rollback_route_switch",
            plan.plan_id,
            plan.expires_at,
        )
        result = await RecordedMCPGateway(self.db, run_id, self.execution_gateway).call_tool(
            "infrastructure",
            "rollback_route_switch",
            {
                "plan": plan.model_dump(mode="json"),
                "grant": grant.model_dump(mode="json"),
                "idempotency_key": f"{execution.id}:rollback",
            },
        )
        parsed = InfrastructureResult.model_validate(result)
        execution.state = "rolled_back" if parsed.success else "failed_safe"
        execution.rollback_json = parsed.model_dump(mode="json")
        self.db.commit()

    def _failed_safe(self, handoff: HandoffRecord, reason: str) -> ExecutionRecord:
        execution = ExecutionRecord(
            engagement_id=handoff.engagement_id,
            handoff_id=handoff.id,
            plan_json={"reason": reason},
            plan_hash=content_hash({"handoff_id": handoff.id, "reason": reason}),
            state="failed_safe",
        )
        self.db.add(execution)
        self.db.commit()
        return execution
