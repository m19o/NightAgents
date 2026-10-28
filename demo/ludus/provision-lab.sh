#!/usr/bin/env bash
# NightAgents lab VM provisioning for Ludus (idempotent).
# Run as the range post-deploy step on each squad VM.
# Requires: REPO_URL (private repo, or pre-seeded /opt/nightagents),
# and the squad's secrets injected as environment variables.
set -euo pipefail

REPO_URL="${REPO_URL:-}"
REPO_DIR="${REPO_DIR:-/opt/nightagents}"
SQUAD="${SQUAD:-squad-01}"
OPENAI_KEY="${OPENAI_KEY:?set OPENAI_KEY (or configure a local model instead)}"
SLACK_BOT_TOKEN="${SLACK_BOT_TOKEN:?set SLACK_BOT_TOKEN for the squad channel}"
SLACK_SIGNING_SECRET="${SLACK_SIGNING_SECRET:?set SLACK_SIGNING_SECRET}"
SLACK_CHANNEL="${SLACK_CHANNEL:?set SLACK_CHANNEL (channel id for the squad)}"
SLACK_APP_TOKEN="${SLACK_APP_TOKEN:-}"   # xapp-... enables Socket Mode approvals (no public URL)
MYTHIC_HOST="${MYTHIC_HOST:-mythic}"   # shared Mythic VM hostname, or 'localhost'

echo "==> [1/6] base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq docker.io docker-compose-v2 git curl python3 fallocate >/dev/null
systemctl enable --now docker

echo "==> [2/6] swap (freeze protection for ES + stack)"
if ! swapon --show | grep -q swapfile; then
  fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "==> [3/6] repo"
if [ ! -d "$REPO_DIR" ]; then
  git clone "$REPO_URL" "$REPO_DIR"
fi
cd "$REPO_DIR"

echo "==> [4/6] .env"
cp -n .env.example .env || true
rand() { openssl rand -hex 24; }
sed -i \
  -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(rand)|" \
  -e "s|^NIGHTAGENTS_HANDOFF_SIGNING_SECRET=.*|NIGHTAGENTS_HANDOFF_SIGNING_SECRET=$(rand)|" \
  -e "s|^NIGHTAGENTS_APPROVAL_SIGNING_SECRET=.*|NIGHTAGENTS_APPROVAL_SIGNING_SECRET=$(rand)|" \
  -e "s|^NIGHTAGENTS_EXECUTION_GRANT_SECRET=.*|NIGHTAGENTS_EXECUTION_GRANT_SECRET=$(rand)|" \
  -e "s|^NIGHTAGENTS_ELASTIC_WEBHOOK_SECRET=.*|NIGHTAGENTS_ELASTIC_WEBHOOK_SECRET=$(rand)|" \
  -e "s|^NIGHTAGENTS_OUTBOX_WEBHOOK_SECRET=.*|NIGHTAGENTS_OUTBOX_WEBHOOK_SECRET=$(rand)|" .env
cat >> .env <<EOF
NIGHTAGENTS_MODEL_API_URL=https://api.openai.com/v1
NIGHTAGENTS_MODEL_API_KEY=$OPENAI_KEY
NIGHTAGENTS_MODEL_NAME=gpt-4o-mini
NIGHTAGENTS_MODEL_ROLES_JSON={"skill_selector":{"provider":"local"},"predictor":{"provider":"local"}}
NIGHTAGENTS_SLACK_API_TOKEN=$SLACK_BOT_TOKEN
NIGHTAGENTS_SLACK_CHANNEL=$SLACK_CHANNEL
NIGHTAGENTS_SLACK_SIGNING_SECRET=$SLACK_SIGNING_SECRET
NIGHTAGENTS_SLACK_APP_TOKEN=$SLACK_APP_TOKEN
NIGHTAGENTS_MYTHIC_URL=https://$MYTHIC_HOST:7443
NIGHTAGENTS_MYTHIC_VERIFY_TLS=false
NIGHTAGENTS_DETECTION_INTERVAL_SECONDS=120
NIGHTAGENTS_DETECTION_SOURCE=logs-redirector-demo
NIGHTAGENTS_AUTO_INVESTIGATE=true
NIGHTAGENTS_HANDOFF_TTL_MINUTES=60
NIGHTAGENTS_INFRASTRUCTURE_MODE=lab
NIGHTAGENTS_LAB_VICTIM_CONTAINER=victim
NIGHTAGENTS_LAB_HOSTNAME=redirector-pass.demo
NIGHTAGENTS_LAB_REDIRECTOR_MAP_JSON={"demo-redirector-burned":"redirector-pass","demo-redirector-standby":"redirector-filter"}
EOF
# MCP identity map + endpoint maps (lab-local tokens)
WATCHER="watcher-$(rand | head -c 8)"; KERNEL="kernel-$(rand | head -c 8)"; RESEARCH="research-$(rand | head -c 8)"
cat >> .env <<EOF
NIGHTAGENTS_MCP_TOKENS_JSON={"$WATCHER":"nightwatcher","$KERNEL":"safety-kernel","$RESEARCH":"research-agent"}
NIGHTAGENTS_MCP_ENDPOINTS_JSON={"elastic":{"url":"http://elastic-mcp:8101/mcp","token":"$WATCHER"},"mythic":{"url":"http://mythic-mcp:8102/mcp","token":"$WATCHER"},"memory":{"url":"http://memory-mcp:8103/mcp","token":"$WATCHER"},"infrastructure":{"url":"http://infrastructure-mcp:8104/mcp","token":"$WATCHER"},"third_party":{"url":"http://third-party-mcp:8105/mcp","token":"$WATCHER"}}
NIGHTAGENTS_SAFETY_MCP_ENDPOINTS_JSON={"mythic":{"url":"http://mythic-mcp:8102/mcp","token":"$KERNEL"},"infrastructure":{"url":"http://infrastructure-mcp:8104/mcp","token":"$KERNEL"}}
NIGHTAGENTS_RESEARCH_MCP_ENDPOINTS_JSON={"memory":{"url":"http://memory-mcp:8103/mcp","token":"$RESEARCH"},"third_party":{"url":"http://third-party-mcp:8105/mcp","token":"$RESEARCH"}}
EOF

echo "==> [5/6] build + start (host network for docker pull reliability)"
docker build --network=host -q -t nightagents-api:latest .
for t in worker elastic-mcp infrastructure-mcp memory-mcp migrate mythic-mcp third-party-mcp; do
  docker tag nightagents-api:latest nightagents-$t:latest
done
docker compose --env-file .env up -d
for i in $(seq 1 40); do
  curl -sf http://127.0.0.1:8000/health/ready 2>/dev/null | grep -q '"ready":true' && break
  sleep 5
done

echo "==> [6/6] provision lab (payload, fleet, implant) + verify slack"
bash demo/scripts/lab_provision.sh
python3 demo/scripts/slack_setup.py \
  --bot-token "$SLACK_BOT_TOKEN" \
  --signing-secret "$SLACK_SIGNING_SECRET" \
  --channel "$SLACK_CHANNEL" --no-restart

echo "LAB READY: $SQUAD - run: python3 demo/scripts/run_demo.py --blue-only"
