#!/usr/bin/env bash
# Tear down the NightAgents live demo: demo containers, demo network,
# shipper state, and payload artifacts. Leaves the main NightAgents stack
# and Mythic running.
set -euo pipefail
cd "$(dirname "$0")/../.."

docker compose -f demo/docker-compose.demo.yml down -v 2>/dev/null || true
rm -rf demo/work
echo "demo torn down"
