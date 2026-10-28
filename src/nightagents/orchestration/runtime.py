import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from nightagents.config import get_settings
from nightagents.db.models import AgentRunRecord, AgentStepRecord, DisagreementRecord, OperatorReviewTaskRecord
from nightagents.db.repositories import AuditRepository, WorkflowRepository
from nightagents.mcp_client import MCPGateway, RecordedMCPGateway
from nightagents.orchestration.summarize import summarize_context
from nightagents.skills.registry import SkillManifest, SkillPhase, SkillRegistry
from nightagents.workflows.budget import WorkflowBudget

PhaseHandler = Callable[[dict, SkillPhase], Awaitable[dict]]


@dataclass
class OrchestrationResult:
    run_id: str
    status: str
    state: dict


class RunCancelledError(RuntimeError):
    pass


class RunDeadlineExceededError(TimeoutError):
    pass


class RunPausedError(RuntimeError):
    pass


class DurableSkillOrchestrator:
    def __init__(
        self,
        db: Session,
        registry: SkillRegistry,
        gateway: MCPGateway,
        model,
        context_max_raw_items: int | None = None,
    ):
        self.db = db
        self.registry = registry
        self.gateway = gateway
        self.model = model
        self.context_max_raw_items = (
            get_settings().model_context_max_raw_items if context_max_raw_items is None else context_max_raw_items
        )
        self.workflows = WorkflowRepository(db)
        self.audit = AuditRepository(db)

    async def run(
        self,
        agent: str,
        skill_id: str,
        engagement_id: str,
        objective: str,
        initial_state: dict,
        run_id: str | None = None,
    ) -> OrchestrationResult:
        manifest = self.registry.get(agent, skill_id)
        run = self._load_or_create(run_id, engagement_id, agent, objective, manifest, initial_state)
        state = dict(run.state_json)
        budget = WorkflowBudget(
            maximum_tool_calls=manifest.maximum_tool_calls,
            maximum_iterations=manifest.maximum_iterations,
            maximum_context_tokens=manifest.maximum_context_tokens,
        )
        gateway = RecordedMCPGateway(self.db, run.id, self.gateway)
        validator = getattr(self.gateway, "validate_manifest", None)
        if validator is not None:
            await validator(manifest)
        phases = {phase.node: phase for phase in manifest.phases}
        current_node = run.current_node if run.current_node not in {"start", "__complete__"} else (
            manifest.phases[0].node if manifest.phases else None
        )
        try:
            while current_node:
                self.db.refresh(run)
                if run.cancelled_at is not None or run.status == "cancelled":
                    raise RunCancelledError("agent run was cancelled")
                if run.status == "paused":
                    raise RunPausedError(run.wait_reason or "agent run was paused")
                deadline = run.deadline_at
                if deadline and (deadline if deadline.tzinfo else deadline.replace(tzinfo=UTC)) <= datetime.now(UTC):
                    raise RunDeadlineExceededError("agent run deadline exceeded")
                phase = phases[current_node]
                run.current_node = phase.node
                self.db.commit()
                budget.consume_iteration()
                self._enforce_context(state, budget)
                output = await asyncio.wait_for(
                    self._execute_phase(
                        manifest,
                        phase,
                        state,
                        gateway,
                        budget,
                        self.registry.instructions(agent, skill_id),
                    ),
                    timeout=phase.timeout_seconds,
                )
                if phase.output_schema:
                    self.registry.validate_output(phase.output_schema, output)
                state[phase.node] = output
                model_info = self._model_info()
                step = self.workflows.add_step(
                    run,
                    phase.node,
                    "completed",
                    {"state_keys": sorted(state)},
                    output,
                    manifest.skill_id,
                    phase.model_role,
                    model_id=model_info.get("model_id"),
                    model_version=model_info.get("model_version"),
                    prompt_version=model_info.get("prompt_version"),
                )
                if phase.kind == "model" and phase.model_role:
                    self._detect_disagreement(run, step, phase.model_role, output)
                current_node = self._next_node(manifest, phase, output)
                self.workflows.checkpoint(run, current_node or "__complete__", state)
                self.db.commit()
            self.workflows.complete(run, "completed", state)
            self.audit.append(
                agent,
                "skill.completed",
                "agent_run",
                run.id,
                {"skill_id": manifest.skill_id, "skill_version": manifest.version},
                engagement_id,
            )
            self.db.commit()
            return OrchestrationResult(run.id, "completed", state)
        except Exception as exc:
            if isinstance(exc, RunCancelledError):
                run.status = "cancelled"
            elif isinstance(exc, RunPausedError):
                run.status = "paused"
            elif isinstance(exc, (RunDeadlineExceededError, TimeoutError)):
                run.status = "timed_out"
            else:
                run.status = "failed"
            run.state_json = state | {"error": str(exc)}
            self.audit.append(
                agent,
                f"skill.{run.status}",
                "agent_run",
                run.id,
                {"skill_id": manifest.skill_id, "error": str(exc)},
                engagement_id,
            )
            self.db.commit()
            raise

    async def _execute_phase(
        self,
        manifest: SkillManifest,
        phase: SkillPhase,
        state: dict,
        gateway: RecordedMCPGateway,
        budget: WorkflowBudget,
        instructions: str,
    ) -> dict:
        if phase.kind == "model":
            context = self._context(state, phase.context_keys)
            context = summarize_context(context, self.context_max_raw_items)
            payload = {"skill_instructions": instructions, "evidence_context": context, **context}
            if phase.output_schema:
                payload["expected_output_schema"] = self.registry.schema(phase.output_schema)
            last_error: ValueError | None = None
            for _ in range(phase.maximum_attempts):
                output = await self.model.invoke(phase.model_role, payload)
                if not phase.output_schema:
                    return output
                try:
                    self.registry.validate_output(phase.output_schema, output)
                except ValueError as exc:
                    # Small models occasionally violate the schema; re-ask once
                    # with the validation error made explicit before failing.
                    last_error = exc
                    payload = {**payload, "previous_output_error": str(exc)}
                    continue
                return output
            raise last_error if last_error is not None else ValueError("model phase produced no output")
        if phase.kind != "tool" or not phase.tool:
            raise ValueError(f"unsupported phase: {phase.node}")
        if phase.tool not in manifest.tools:
            raise PermissionError(f"skill does not allow tool: {phase.tool}")
        server, tool = phase.tool.split(".", 1)
        arguments = self._resolve(phase.arguments, state)
        attempts = phase.maximum_attempts
        for attempt in range(1, attempts + 1):
            budget.consume_tool()
            try:
                result = await gateway.call_tool(server, tool, arguments)
                return result if isinstance(result, dict) else {"items": result}
            except Exception:
                if attempt == attempts:
                    raise
                await asyncio.sleep(min(0.1 * attempt, 0.5))
        raise RuntimeError("unreachable")

    def _load_or_create(
        self,
        run_id: str | None,
        engagement_id: str,
        agent: str,
        objective: str,
        manifest: SkillManifest,
        initial_state: dict,
    ) -> AgentRunRecord:
        if run_id:
            run = self.db.get(AgentRunRecord, run_id)
            if run is None or run.agent != agent or run.engagement_id != engagement_id:
                raise ValueError("resumable agent run not found")
            return run
        deadline_at = datetime.now(UTC) + timedelta(seconds=manifest.deadline_seconds)
        run = self.workflows.create_run(
            engagement_id,
            agent,
            objective,
            {
                "skill_id": manifest.skill_id,
                "skill_version": manifest.version,
                "maximum_tool_calls": manifest.maximum_tool_calls,
                "maximum_context_tokens": manifest.maximum_context_tokens,
                "model_name": getattr(self.model, "model_name", type(self.model).__name__),
            },
        )
        run.state_json = initial_state
        run.deadline_at = deadline_at
        self.db.commit()
        return run

    def _model_info(self) -> dict:
        return {
            "model_id": getattr(self.model, "model_name", type(self.model).__name__),
            "model_version": getattr(self.model, "model_version", "unknown"),
            "prompt_version": getattr(self.model, "prompt_version", ""),
        }

    def _detect_disagreement(self, run: AgentRunRecord, step: "AgentStepRecord", role: str, output: dict) -> None:
        threshold = 0.3
        current_supported = output.get("supported")
        if current_supported is None:
            return
        previous = list(
            self.db.scalars(
                select(AgentStepRecord)
                .where(
                    AgentStepRecord.run_id == run.id,
                    AgentStepRecord.model_role.isnot(None),
                    AgentStepRecord.id != step.id,
                )
                .order_by(AgentStepRecord.sequence)
            )
        )
        disagreeing: list[AgentStepRecord] = []
        for other in previous:
            other_supported = other.output_json.get("supported")
            if other_supported is None:
                continue
            if current_supported != other_supported:
                disagreeing.append(other)
        if disagreeing:
            record = DisagreementRecord(
                run_id=run.id,
                step_ids_json=[step.id] + [other.id for other in disagreeing],
                roles_json=[role] + [other.model_role for other in disagreeing],
                disagreement_type="supported_mismatch",
                threshold=threshold,
                adjudicator_role="adjudicator",
            )
            self.db.add(record)
            self.audit.append(
                "orchestrator",
                "model.disagreement_detected",
                "agent_run",
                run.id,
                {"disagreement_id": record.id, "roles": record.roles_json},
                run.engagement_id,
            )

    async def adjudicate_disagreement(self, disagreement_id: str) -> dict:
        record = self.db.get(DisagreementRecord, disagreement_id)
        if record is None:
            raise ValueError("disagreement not found")
        if record.status == "resolved":
            raise ValueError("disagreement already resolved")
        steps = list(
            self.db.scalars(
                select(AgentStepRecord).where(AgentStepRecord.id.in_(record.step_ids_json))
            )
        )
        step_payloads = [step.output_json for step in steps]
        result = await self.model.invoke("adjudicator", {"steps": step_payloads})
        resolution = result.get("resolution", "escalate_to_operator")
        confidence = result.get("confidence", 0.0)
        run = self.db.get(AgentRunRecord, record.run_id)
        engagement_id = run.engagement_id if run is not None else None
        record.adjudicator_role = "adjudicator"
        record.resolution = resolution
        if resolution == "escalate_to_operator":
            record.status = "escalated"
            review = OperatorReviewTaskRecord(
                engagement_id=engagement_id,
                target_type="disagreement",
                target_id=record.id,
                reason=f"model disagreement could not be resolved: {record.roles_json}",
            )
            self.db.add(review)
            self.db.flush()
            self.audit.append(
                "adjudicator",
                "model.disagreement_escalated",
                "disagreement",
                record.id,
                {"review_task_id": review.id, "roles": record.roles_json},
                engagement_id,
            )
        else:
            record.status = "resolved"
            self.audit.append(
                "adjudicator",
                "model.disagreement_resolved",
                "disagreement",
                record.id,
                {"resolution": resolution, "confidence": confidence, "roles": record.roles_json},
                engagement_id,
            )
        record.resolved_at = datetime.now(UTC)
        self.db.commit()
        return {
            "disagreement_id": record.id,
            "status": record.status,
            "resolution": record.resolution,
            "confidence": confidence,
        }

    @staticmethod
    def _context(state: dict, keys: list[str]) -> dict:
        return {key: state.get(key) for key in keys} if keys else state

    @staticmethod
    def _enforce_context(state: dict, budget: WorkflowBudget) -> None:
        budget.set_context_tokens(len(str(state)) // 4)

    @classmethod
    def _resolve(cls, value, state: dict):
        if isinstance(value, dict):
            return {key: cls._resolve(item, state) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._resolve(item, state) for item in value]
        if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
            current = state
            for part in value[2:-1].split("."):
                current = current[part]
            return current
        return value

    @classmethod
    def _next_node(cls, manifest: SkillManifest, phase: SkillPhase, output: dict) -> str | None:
        for rule in phase.transitions:
            actual = cls._path(output, rule.path)
            if rule.operator == "equals":
                matched = actual == rule.value
            elif rule.operator == "not_equals":
                matched = actual != rule.value
            elif rule.operator == "truthy":
                matched = bool(actual)
            elif rule.operator == "falsy":
                matched = not bool(actual)
            elif rule.operator == "contains":
                matched = actual is not None and hasattr(actual, "__contains__") and rule.value in actual
            else:
                raise ValueError(f"unsupported transition operator: {rule.operator}")
            if matched:
                return None if rule.target == "$end" else rule.target
        if phase.next_node:
            return None if phase.next_node == "$end" else phase.next_node
        index = [item.node for item in manifest.phases].index(phase.node)
        return manifest.phases[index + 1].node if index + 1 < len(manifest.phases) else None

    @staticmethod
    def _path(value: dict, path: str):
        current = value
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return current
