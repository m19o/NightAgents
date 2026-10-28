#!/usr/bin/env bash
# NightAgents range provisioning - run ONCE as the range owner (or from a
# Ludus/Ansible task) on the lab host. After this completes, candidates only
# ever run:  python demo/scripts/run_demo.py --blue-only
#
# What it does:
#   1. starts the NightAgents control plane (compose) and waits for readiness
#   2. provisions the demo lab: builds (or reuses) the Poseidon payload
#      artifact via the local Mythic, starts the redirector fleet + victim,
#      executes the implant and confirms a live callback
#   3. leaves everything running
#
# Prerequisites: see demo/GUIDE.md (Mythic running with poseidon + http
# profile on host port 80, operation "Operation Chimera", filled repo .env).
set -euo pipefail
cd "$(dirname "$0")/../.."   # repo root

echo "==> [1/3] NightAgents control plane"
docker compose --env-file .env up -d --build

echo "==> waiting for /health/ready"
for i in $(seq 1 30); do
  if curl -sf http://127.0.0.1:8000/health/ready | grep -q '"ready":true'; then
    break
  fi
  if [ "$i" -eq 30 ]; then
    echo "ERROR: control plane did not become ready" >&2
    exit 1
  fi
  sleep 5
done
echo "==> control plane ready"

echo "==> [2/3] provisioning demo lab (payload, fleet, implant)"
python3 demo/scripts/run_demo.py --setup-only

echo "==> [3/3] done"
echo "Lab is live. Candidates run:  python demo/scripts/run_demo.py --blue-only"
