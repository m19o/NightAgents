#!/usr/bin/env bash
# CI gate for NightAgents replay evaluation.
#
# Fails the build when any replay fixture fails or when fewer fixtures pass
# than exist on disk. Calibration regressions are covered by the unit tests
# in tests/test_prediction_feedback.py; this gate enforces the replay
# regression thresholds suitable for CI.
set -euo pipefail

cd "$(dirname "$0")/.."

FIXTURE_DIR="${NIGHTAGENTS_FIXTURE_DIR:-evaluations/fixtures}"
SKILLS_DIR="${NIGHTAGENTS_SKILLS_DIR:-skills}"

fixture_count=$(find "$FIXTURE_DIR" -maxdepth 1 -name '*.json' | wc -l)
if [ "$fixture_count" -eq 0 ]; then
    echo "ci-evaluation-gate: no fixtures found in $FIXTURE_DIR" >&2
    exit 1
fi

echo "ci-evaluation-gate: requiring 0 failures and >= ${fixture_count} passed fixtures"
if command -v nightagents >/dev/null 2>&1; then
    nightagents evaluate \
        --fixtures "$FIXTURE_DIR" \
        --skills "$SKILLS_DIR" \
        --max-failures 0 \
        --min-passed "$fixture_count"
else
    PYTHONPATH=src python -c "from nightagents.cli import main; main()" evaluate \
        --fixtures "$FIXTURE_DIR" \
        --skills "$SKILLS_DIR" \
        --max-failures 0 \
        --min-passed "$fixture_count"
fi
echo "ci-evaluation-gate: OK"
