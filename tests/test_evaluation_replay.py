from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from nightagents.db.base import Base
from nightagents.db.models import EvaluationAssertionRecord, EvaluationRunRecord
from nightagents.db.session import build_engine
from nightagents.evaluation import ReplayEvaluator


async def test_replay_evaluator_runs_recorded_fixture() -> None:
    root = Path(__file__).parents[1]
    fixture_count = len(list((root / "evaluations" / "fixtures").glob("*.json")))
    report = await ReplayEvaluator(root / "skills").run_directory(root / "evaluations" / "fixtures")
    assert report["passed"] is True
    assert report["total"] == fixture_count
    for item in report["reports"]:
        assert item["actual_tools"] == item["expected_tools"]


async def test_replay_evaluator_enforces_threshold() -> None:
    root = Path(__file__).parents[1]
    report = await ReplayEvaluator(root / "skills").run_directory(
        root / "evaluations" / "fixtures",
        threshold={"min_passed": 10},
    )
    assert report["passed"] is False
    assert report["threshold_ok"] is False
    assert any("below threshold" in reason for reason in report["threshold_reasons"])


def test_replay_evaluator_persists_results() -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    root = Path(__file__).parents[1]
    report = __import__("asyncio").run(
        ReplayEvaluator(root / "skills", db=db).run_directory(root / "evaluations" / "fixtures")
    )
    assert report["passed"] is True
    runs = list(db.scalars(select(EvaluationRunRecord)))
    assert len(runs) == report["total"]
    assertions = list(db.scalars(select(EvaluationAssertionRecord)))
    assert len(assertions) >= 6
    assert all(run.passed for run in runs)
