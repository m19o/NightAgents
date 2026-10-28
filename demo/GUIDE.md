# NightAgents Setup & Demo Guide

Step-by-step operator guide: stand up the NightAgents control plane, connect it to a real local Mythic C2 lab, and run the live purple-team demo. Everything runs on one host; nothing touches production or third-party infrastructure.

For the architecture behind these steps see `docs/agentic/ARCHITECTURE.md`; for the teaching narrative see `demo/README.md`.

## 0. What you will have at the end

```
Mythic C2 (lab) -> Poseidon implant on a victim container -> 4-technique redirector fleet
      ^                                                              |
      |                                                     access logs -> Elastic
      |                                                              v
 NightAgents API/worker + 5 MCP servers  <- signed webhook <- beacon detector
      |
      +-> NightWatcher investigates -> operator approves -> NightRouter + Safety Kernel
          -> continuity UNKNOWN -> automatic rollback -> hash-chained audit trail
```

## 1. Prerequisites

- Linux host with Docker + the compose plugin, ~8 GB RAM free (Elastic, Mythic and the NightAgents stack run together).
- A clone of this repository; all commands below run from its root.
- A local Mythic 3.x install (this guide assumes `~/mythic-install`, managed with `mythic-cli`), with:
  - the **poseidon** agent builder and the **http** C2 profile installed and running,
  - the http profile bound to **host port 80** (the redirectors proxy to it),
  - an operation named exactly **Operation Chimera** (callback inventory is scoped by this
    name - the demo's NightAgents engagement uses the same name on purpose),
  - the admin credentials in `~/mythic-install/.env` (`MYTHIC_ADMIN_USER`,
    `MYTHIC_ADMIN_PASSWORD`). The demo driver reads them from there; nothing is hardcoded.

Verify the Mythic side before continuing:

```bash
docker ps --format '{{.Names}}' | grep -E 'mythic_server|poseidon|^http$'
curl -sk -o /dev/null -w '%{http_code}\n' https://127.0.0.1:7443/   # Mythic UI/API
curl -s  -o /dev/null -w '%{http_code}\n' http://127.0.0.1:80/       # http C2 profile
```

## 2. Configure NightAgents

```bash
cd /path/to/nightagents
cp .env.example .env
```

Fill `.env`. A complete development topology looks like this - replace every `CHANGE-ME` with an independent random 32+ character string (`openssl rand -hex 32`):

```bash
NIGHTAGENTS_ENVIRONMENT=development
POSTGRES_PASSWORD=CHANGE-ME

# Signing and webhook secrets - one independent value per purpose
NIGHTAGENTS_HANDOFF_SIGNING_SECRET=CHANGE-ME
NIGHTAGENTS_APPROVAL_SIGNING_SECRET=CHANGE-ME
NIGHTAGENTS_EXECUTION_GRANT_SECRET=CHANGE-ME
NIGHTAGENTS_ELASTIC_WEBHOOK_SECRET=CHANGE-ME     # the demo driver signs webhooks with this
NIGHTAGENTS_OUTBOX_WEBHOOK_SECRET=CHANGE-ME

# MCP identities: lab bearer tokens mapped to principals (OAuth resource-server model)
NIGHTAGENTS_MCP_TOKENS_JSON={"watcher-token":"nightwatcher","router-token":"nightrouter","kernel-token":"safety-kernel","research-token":"research-agent"}

# Three endpoint maps = three separated identities (planning / Safety-Kernel / research)
NIGHTAGENTS_MCP_ENDPOINTS_JSON={"elastic":{"url":"http://elastic-mcp:8101/mcp","token":"watcher-token"},"mythic":{"url":"http://mythic-mcp:8102/mcp","token":"watcher-token"},"memory":{"url":"http://memory-mcp:8103/mcp","token":"watcher-token"},"third_party":{"url":"http://third-party-mcp:8105/mcp","token":"watcher-token"}}
NIGHTAGENTS_SAFETY_MCP_ENDPOINTS_JSON={"mythic":{"url":"http://mythic-mcp:8102/mcp","token":"kernel-token"},"infrastructure":{"url":"http://infrastructure-mcp:8104/mcp","token":"kernel-token"}}
NIGHTAGENTS_RESEARCH_MCP_ENDPOINTS_JSON={"memory":{"url":"http://memory-mcp:8103/mcp","token":"research-token"},"third_party":{"url":"http://third-party-mcp:8105/mcp","token":"research-token"}}

# Integrations - the demo driver rewrites the three MYTHIC lines automatically per run
NIGHTAGENTS_ELASTIC_URL=http://elasticsearch:9200
NIGHTAGENTS_ELASTIC_VERIFY_TLS=false
NIGHTAGENTS_MYTHIC_URL=https://127.0.0.1:7443
NIGHTAGENTS_MYTHIC_API_KEY=
NIGHTAGENTS_MYTHIC_VERIFY_TLS=false

NIGHTAGENTS_INFRASTRUCTURE_MODE=database
NIGHTAGENTS_OPERATOR_ID=development-operator
```

Notes:

- The service names and ports (`elastic-mcp:8101` … `third-party-mcp:8105`) are fixed by `docker-compose.yml`; keep them as shown for the Compose topology.
- `NIGHTAGENTS_MYTHIC_API_KEY` starts empty - the demo driver mints a fresh Mythic API token each run (they expire after ~4 hours) and rewrites the `MYTHIC_*` lines to point the container at the host-resident Mythic (`https://host.docker.internal:7443`).
- Leave every `NIGHTAGENTS_MODEL_*` unset to use the deterministic local model; set `NIGHTAGENTS_MODEL_API_URL`/`_KEY`/`_NAME` or `NIGHTAGENTS_MODEL_ROLES_JSON` to route roles to an OpenAI-compatible endpoint.

## 3. Start the stack

```bash
cd /path/to/nightagents
docker compose --env-file .env up -d --build
```

This starts PostgreSQL, single-node Elastic (development mode, security disabled - lab only), the `migrate` job (Alembic), the API, the worker (scheduler + outbox) and the five MCP domain servers (`elastic-mcp`, `mythic-mcp`, `memory-mcp`, `infrastructure-mcp`, `third-party-mcp`).

Verify readiness - MCP contract discovery must pass before agents run:

```bash
curl -s http://127.0.0.1:8000/health/ready
# {"status":"ready","ready":true,"issues":[],"mcp_ready":true,"mcp_issues":[]}
curl -s http://127.0.0.1:8000/api/v2/operations/mcp-health | python3 -m json.tool
```

If you build behind a flaky DNS/network, build with host networking and retag:

```bash
docker build --network=host -t nightagents-api:latest .
for t in worker elastic-mcp infrastructure-mcp memory-mcp migrate mythic-mcp third-party-mcp; do
  docker tag nightagents-api:latest nightagents-$t:latest
done
docker compose --env-file .env up -d
```

## 4. Run the demo

Three modes:

```bash
# Full red+blue narrative run (presenter mode; needs Mythic admin creds)
python demo/scripts/run_demo.py

# Provision the lab once (range owner / Ludus task): payload artifact cached in
# demo/artifacts/, fleet up, implant beaconing
bash demo/scripts/lab_provision.sh        # = stack up + run_demo.py --setup-only

# Blue-team scenario only (candidates; no Mythic access needed, repeatable)
python demo/scripts/run_demo.py --blue-only
```

Hosting as a range (Ludus or any VM): the owner provisions **once** with `lab_provision.sh`; candidates only ever run `--blue-only`, which verifies the provisioned lab (Elastic, API, mythic-mcp link, payload artifact), ensures the fleet and implant are running, and then executes stages 3–6 below with a fresh incident each run.

The driver narrates every stage. What to expect:

| Stage | What happens | Success signal |
|---|---|---|
| 0 Preflight | Checks Elastic/API/Mythic, mints a fresh Mythic API token, reconnects `mythic-mcp` to the real instance | `mythic-mcp connected to the real mythic instance` |
| 1 Red: build | Builds a Poseidon payload via the real Mythic API, beaconing through redirector 1 | `payload downloaded: demo/work/poseidon-demo` |
| 2 Red: execute | Starts the 4-redirector fleet + victim, runs the implant | `live callback in mythic: id=...` |
| 3 Blue: monitoring | Registers the `Operation Chimera` engagement + redirector assets, streams nginx logs to `logs-redirector-demo` | `shipper config written` |
| 4 Blue: detect | Fingerprints the beacon (intervals, URIs, fixed UA, one client), sends the HMAC-signed webhook | `signed incident webhook accepted, incident=...` |
| 5 Blue: respond | NightWatcher investigates (source health + Elastic evidence + real Mythic callbacks), signed handoff, operator approval, NightRouter executes | `execution finished with state=rolled_back` |
| 6 Evidence | Audit-chain verification, calibration, ops summary | `audit chain valid=True` |

The stage-5 rollback is the designed outcome, not a failure: Mythic callbacks cannot prove which redirector carried them, so continuity is UNKNOWN and the Safety Kernel rolls back. Unknown continuity is never reported as success.

While it runs (or after), watch the evidence live:

```bash
# Beacon telemetry the defender actually sees
curl -s 'http://127.0.0.1:9200/logs-redirector-demo/_search?pretty&size=5'

# Live callbacks in Mythic (or the Mythic UI at https://127.0.0.1:7443)
# Decoy behavior: fetch a filtering redirector as a "scanner"
docker exec nightagents-demo-redirector-filter-1 wget -qO- http://localhost:8080/ | head -3

# NightAgents side
curl -s http://127.0.0.1:8000/api/v2/audit/verify
curl -s http://127.0.0.1:8000/api/v2/operations/summary
```

## 5. Operator approvals in Slack (interactive)

The platform's real workflow is: NightWatcher posts a handoff to Slack with **Approve / Reject buttons**, the operator clicks, Slack calls NightAgents back, and only then can execution proceed. The demo driver waits for that click (`--auto-approve` exists for unattended test runs only).

Setup (once, in the Slack app at api.slack.com/apps):

0. **Fast path** - point the setup script at any workspace's bot token, signing secret and channel; it validates, writes `.env`, and proves delivery: `python demo/scripts/slack_setup.py --bot-token xoxb-... --signing-secret <secret> --channel <id>` (For the tactic, use one shared workspace; attendees join the channel and approve from their own phones - no per-attendee setup.)
1. **Inbound path A - Socket Mode (recommended, no public URL needed)**: enable *Socket Mode* in the app, create an app-level token with the `connections:write` scope, and put it in `.env` as `NIGHTAGENTS_SLACK_APP_TOKEN`. The worker dials out to Slack over an authenticated websocket and receives Approve/Reject clicks there - works on laptops and NAT'd range VMs with zero exposure.
2. **Inbound path B - HTTP webhook**: *Interactivity & Shortcuts* → On → Request URL: `https://<your-public-host>/api/v2/slack/interactions`. Slack must be able to reach this URL - on a laptop use a tunnel (`cloudflared tunnel --url http://127.0.0.1:8000`); on a Ludus VM use its address. Put the app's *Basic Information → Signing Secret* in `.env` as `NIGHTAGENTS_SLACK_SIGNING_SECRET`. Both paths share one handler, one audit trail.
3. Bot token in `.env` as `NIGHTAGENTS_SLACK_API_TOKEN`, plus `NIGHTAGENTS_SLACK_CHANNEL` (a user id delivers DMs; a channel id works after `/invite @nightagents`).
4. Recreate the stack: `docker compose --env-file .env up -d --force-recreate api worker`.
5. **Per-attendee channels (optional)**: give each attendee their own channel and let the platform route their agent notifications there: `python demo/scripts/slack_channels.py --bot-token xoxb-... alice bob carol` (bot needs the `channels:manage` scope), then set `NIGHTAGENTS_SLACK_CHANNELS_JSON={"<engagement_id>":"<channel_id>", ...}`. Every handoff, execution outcome and intel hit for an engagement lands in its own channel; everything else falls back to `NIGHTAGENTS_SLACK_CHANNEL`.

Security properties: every interaction callback is verified against the Slack request signature + timestamp (fail-closed 401 otherwise), approvals recorded with the Slack operator identity (`slack:<username>`), and a handoff decided once cannot be re-decided. Without a signing secret the approval message degrades to plain text with the CLI command.

## 6. Cleanup

```bash
bash demo/scripts/cleanup.sh     # demo fleet down, volumes and demo/work removed
```

The main stack stays up. The demo's `.env` edits (`MYTHIC_*` lines) are harmless - the next run rewrites them again.

## 6. Troubleshooting (all seen in practice)

- **Stage 0: `mythic-mcp did not become healthy`** - the API token is stale (4 h expiry) or Mythic is down. Re-run the driver; it mints a fresh token. Check `docker logs nightagents-mythic-mcp-1`.
- **Stage 1: payload build errors** - check the build in the Mythic UI (payloads page); the driver polls `build_phase` and prints `build_stderr` on failure.
- **Stage 1: download 404** - old client bug; `/direct/download/` needs the filemeta `agent_file_id`, not `payload.file_id`. Fixed in `demo/scripts/mythic_api.py` - pull latest.
- **Stage 2: no live callback** - confirm the http C2 profile container is up and on host port 80 (`docker restart http` clears a stale RabbitMQ queue lock), and that the victim can resolve `redirector-pass.demo` (both are on the `nightagents-demo_demo` network).
- **Stage 4: 0 events in Elastic** - the shipper must reach Elastic over the `nightagents_default` network (`http://elasticsearch:9200`); host-published ports bound to localhost are unreachable from containers. Also recreate stale demo volumes with `docker compose -f demo/docker-compose.demo.yml down -v` - the nginx image seeds fresh volumes with `access.log -> /dev/stdout` symlinks.
- **Stage 5: investigation 500** - on loaded hosts the orchestrator's per-phase timeout, run deadline or context budget can fire. All manifests ship explicit bounds (`timeout_seconds: 60`, `deadline_seconds: 600`, 64k investigation context); rebuild the images after changing skills (`COPY skills ./skills` bakes them in). Check the run state: `docker exec nightagents-postgres-1 psql -U nightagents -c "select status, current_node from agent_runs order by updated_at desc limit 1;"`
- **General docker sluggishness** - this lab runs Elastic + Mythic + NightAgents + a desktop on one host; load spikes stall the daemon. Wait it out rather than retrying aggressively.

## 7. Safety

The demo is deliberately defensive in scope: local lab containers only, the implant beacons only to local redirectors, and the NightAgents side demonstrates detection, evidence handling, human approval and safe rollback. Do not point it at anything outside your authorized lab.
