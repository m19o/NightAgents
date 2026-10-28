#!/usr/bin/env bash
# Build the Ludus tactic package: the minimal slice of NightAgents needed to run
# the teaching labs - nothing more.
#
# INCLUDED (required to run the demo loop):
#   runtime (src/, skills/, migrations/), container build (Dockerfile,
#   docker-compose.yml), the demo itself (demo/), .env.example, alembic.ini,
#   pyproject.toml
# EXCLUDED (stays in the private repo):
#   tests/, evaluations/, docs/ (design series + handoffs + status), scripts/,
#   HANDOFF.md, TRACEABILITY, all caches and VCS metadata
#
# Output: dist/nightagents-tactic/  (+ dist/nightagents-tactic.tar.gz)
# Push dist/nightagents-tactic to a separate small repo and give Ludus THAT url.
set -euo pipefail
cd "$(dirname "$0")/../.."   # repo root

OUT=dist/nightagents-tactic
rm -rf "$OUT" dist/nightagents-tactic.tar.gz
mkdir -p "$OUT"

for path in src skills migrations demo Dockerfile docker-compose.yml .env.example alembic.ini pyproject.toml; do
  cp -a "$path" "$OUT/"
done

# strip caches, docs and any secrets that snuck in - Ludus gets code + configs only
find "$OUT" -type d \( -name '__pycache__' -o -name '.pytest_cache' -o -name '.ruff_cache' \) -prune -exec rm -rf {} +
find "$OUT" -type f -name '*.md' -delete
rm -rf "$OUT/demo/work" "$OUT/demo/artifacts"

# one-paragraph orientation note (the only doc in the package)
cat > "$OUT/README" <<'EOF'
NightAgents tactic package - "Burning Redirectors Before Blue Team Does".
Run the scenario: python3 demo/scripts/run_demo.py --blue-only
Provision a lab VM:  bash demo/scripts/lab_provision.sh
EOF

tar -czf dist/nightagents-tactic.tar.gz -C dist nightagents-tactic
echo "package: $OUT"
echo "tarball: dist/nightagents-tactic.tar.gz"
du -sh "$OUT" dist/nightagents-tactic.tar.gz
