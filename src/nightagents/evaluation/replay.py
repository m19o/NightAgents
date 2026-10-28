import asyncio
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from nightagents.db.base import Base
from nightagents.db.models import (
    EngagementRecord,
    EvaluationAssertionRecord,
    EvaluationRunRecord,
    ToolCallRecord,
)
from nightagents.db.session import build_engine
from nightagents.models.local import LocalStructuredModel
from nightagents.orchestration.runtime import DurableSkillOrchestrator
from nightagents.skills.registry import SkillRegistry


class RecordedFixtureGateway:
    def __init__(self, responses: dict[str, object]):
        self.responses = responses
        self.offsets: dict[str, int] = {}

    async def call_tool(self, server: str, tool: str, arguments: dict):
        key = f"{server}.{tool}"
        if key not in self.responses:
            raise AssertionError(f"fixture has no response for {key}")
        value = self.responses[key]
        if not isinstance(value, list):
            return value
        offset = self.offsets.get(key, 0)
        if offset >= len(value):
            raise AssertionError(f"fixture exhausted responses for {key}")
        self.offsets[key] = offset + 1
        return value[offset]


class ReplayEvaluator:
    def __init__(self, skills_root: Path, db=None):
        self.skills_root = skills_root
        self.db = db

    async def run_file(self, fixture_path: Path) -> dict:
        fixture = json.loads(await asyncio.to_thread(fixture_path.read_text))
        engine = build_engine("sqlite://") if self.db is None else self.db.bind
        if self.db is None:
            Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        with factory() as db:
            engagement_name = fixture.get("engagement_name", "evaluation")
            existing = db.scalar(select(EngagementRecord).where(EngagementRecord.name == engagement_name))
            if existing is not None:
                engagement = existing
            else:
                engagement = EngagementRecord(
                    id=fixture.get("engagement_id", "evaluation-engagement"),
                    name=engagement_name,
                )
                db.add(engagement)
                db.commit()
            orchestrator = DurableSkillOrchestrator(
                db,
                SkillRegistry(self.skills_root),
                RecordedFixtureGateway(fixture["tool_responses"]),
                LocalStructuredModel(),
            )
            result = await orchestrator.run(
                fixture["agent"],
                fixture["skill_id"],
                engagement.id,
                f"Replay {fixture['evaluation_id']}",
                fixture["initial_state"],
            )
            assertions = []
            for path, expected in fixture.get("expected_state", {}).items():
                actual = self._path(result.state, path)
                assertions.append({"path": path, "expected": expected, "actual": actual, "passed": actual == expected})
            calls = list(db.scalars(select(ToolCallRecord).where(ToolCallRecord.run_id == result.run_id)))
            expected_tools = fixture.get("expected_tools", [])
            actual_tools = [f"{call.server}.{call.tool}" for call in calls]
            tools_passed = not expected_tools or actual_tools == expected_tools
            passed = all(item["passed"] for item in assertions) and tools_passed
            run_record = EvaluationRunRecord(
                evaluation_id=fixture["evaluation_id"],
                dataset_version=fixture.get("dataset_version", ""),
                model_version=fixture.get("model_version", ""),
                prompt_version=fixture.get("prompt_version", ""),
                skill_version=fixture.get("skill_version", ""),
                passed=passed,
                run_status=result.status,
            )
            db.add(run_record)
            db.flush()
            for assertion in assertions:
                db.add(
                    EvaluationAssertionRecord(
                        run_id=run_record.id,
                        path=assertion["path"],
                        expected_json=assertion["expected"],
                        actual_json=assertion["actual"],
                        passed=assertion["passed"],
                    )
                )
            db.commit()
            return {
                "evaluation_id": fixture["evaluation_id"],
                "passed": passed,
                "assertions": assertions,
                "expected_tools": expected_tools,
                "actual_tools": actual_tools,
                "run_status": result.status,
                "run_id": run_record.id,
            }

    async def run_directory(self, fixture_directory: Path, threshold: dict | None = None) -> dict:
        paths = await asyncio.to_thread(lambda: sorted(fixture_directory.glob("*.json")))
        reports = [await self.run_file(path) for path in paths]
        failed = sum(not report["passed"] for report in reports)
        threshold = threshold or {}
        max_failures = threshold.get("max_failures")
        min_passed = threshold.get("min_passed")
        threshold_ok = True
        threshold_reasons: list[str] = []
        if max_failures is not None and failed > max_failures:
            threshold_ok = False
            threshold_reasons.append(f"failures {failed} exceed threshold {max_failures}")
        if min_passed is not None and (len(reports) - failed) < min_passed:
            threshold_ok = False
            threshold_reasons.append(f"passed {len(reports) - failed} below threshold {min_passed}")
        return {
            "passed": bool(reports) and all(report["passed"] for report in reports) and threshold_ok,
            "total": len(reports),
            "failed": failed,
            "threshold_ok": threshold_ok,
            "threshold_reasons": threshold_reasons,
            "reports": reports,
        }

    @staticmethod
    def _path(value: dict, path: str):
        current = value
        for part in path.split("."):
            current = current[part]
        return current
