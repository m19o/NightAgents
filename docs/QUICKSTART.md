# Quickstart

Two ways in: the **teaching lab** (everything self-contained, recommended first run) or a **platform deployment** against your own telemetry.

## 1. Teaching lab (self-contained)

Prereqs: Docker with compose. A Mythic C2 with the poseidon agent and an http profile on port 80, and an operation named `Operation Chimera` (the lab guide walks through a minimal Mythic setup; any throwaway install works).

```bash
git clone https://github.com/m19o/nightagents.git
cd nightagents
cp .env.example .env          # fills safe placeholders; edit as needed
docker compose --env-file .env up -d
bash demo/scripts/lab_provision.sh            # payload + redirector fleet + implant
python3 demo/scripts/live_show.py             # the full loop, live
```

`live_show.py` runs the whole story in your terminal: the autonomous hunter opens an incident, NightWatcher investigates with a real model (`NIGHTAGENTS_MODEL_API_KEY` - an OpenAI key works; see `NIGHTAGENTS_MODEL_ROLES_JSON` for role routing), a handoff card lands in Slack, you click **Approve**, and the Safety Kernel moves the beacon traffic to a standby redirector - proving continuity from the access logs, or rolling back when it cannot prove it.

Slack setup (5 minutes): create an app, enable **Socket Mode**, create an app-level token with `connections:write`, a bot token with `chat:write`, and put all three in `.env` (`NIGHTAGENTS_SLACK_APP_TOKEN`, `NIGHTAGENTS_SLACK_API_TOKEN`, `NIGHTAGENTS_SLACK_CHANNEL`). Approvals then work with no public URL and no tunnel. Full detail: `demo/GUIDE.md` section 5. The scenarios to run as a group: `demo/LABS.md`.

Open the live dashboard at `http://127.0.0.1:8000/dashboard`.

## 2. Platform deployment

The lab is just one engagement. To point the platform at your own infrastructure:

1. **Telemetry**: ship redirector access logs to Elastic and set `NIGHTAGENTS_ELASTIC_URL` (+ API key) and `NIGHTAGENTS_DETECTION_SOURCE`.
2. **C2**: set `NIGHTAGENTS_MYTHIC_URL` and a Mythic API token so callback inventories and continuity checks reflect your operation.
3. **Infrastructure control**: choose `NIGHTAGENTS_INFRASTRUCTURE_MODE`:
   - `database` (default): records intent only - dry run.
   - `cloudflare`: real edge control. Set
     `NIGHTAGENTS_CLOUDFLARE_API_TOKEN` (scoped: DNS:Edit, Analytics:Read),
     `NIGHTAGENTS_CLOUDFLARE_REDIRECTOR_MAP_JSON` (redirector id ->
     `{zone_id, dns_record_id, target}`) and
     `NIGHTAGENTS_CLOUDFLARE_ROUTE_MAP_JSON` (logical route id ->
     `{zone_id, dns_record_id}`). Try it safely first with
     `NIGHTAGENTS_CLOUDFLARE_SANDBOX=true`.
   - `provider`: your own plugin via the `nightagents.infrastructure`
     entry-point group implementing `InfrastructurePort`.
4. **Agents on**: `NIGHTAGENTS_DETECTION_INTERVAL_SECONDS=120`, `NIGHTAGENTS_AGENTIC_HUNT=true` (the agent hunts telemetry itself), `NIGHTAGENTS_AUTO_INVESTIGATE=true`.
5. **Operator channel**: Slack as above, plus `NIGHTAGENTS_OPERATOR_TOKENS_JSON` for API access outside development.

## Verify the install

```bash
curl -s http://127.0.0.1:8000/health/ready
curl -s http://127.0.0.1:8000/api/v2/audit/verify
python -m pytest tests -q          # from a source checkout, dev extras
```

## Layout

```text
src/nightagents/      platform (agents, orchestration, MCP, safety, detection, intel, slack)
skills/               versioned skill manifests, hunting playbook, output schemas
migrations/           Alembic revisions
demo/                 the teaching lab: scripts, redirector configs, scenarios, guide
demo/ludus/           Ansible role + range definition for Ludus cyber ranges
tests/, evaluations/  unit tests + replay fixtures
docs/                 architecture, security model, this quickstart
```
