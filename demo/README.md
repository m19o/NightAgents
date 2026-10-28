# NightAgents Live Demo - Red Team Village

A self-contained purple-team demo that runs the full loop on one host. **Giving the talk? Start with [demo/TALK.md](TALK.md)** - a 40-minute beginner script built around this demo; setup is in [demo/GUIDE.md](GUIDE.md). **Running the tactic? Use [demo/LABS.md](LABS.md)** - six hands-on teaching labs for attendees.

```
Mythic C2 (real, local)  ->  Poseidon implant on a victim container
        ^                            |
        |                     beacon through redirector fleet (4 techniques)
        |                            v
   NightAgents  <- signed webhook <- detector <- Elastic <- redirector access logs
   (NightWatcher -> HITL approval -> NightRouter -> Safety Kernel -> audit chain)
```

Everything is lab infrastructure: a local Mythic install, Docker containers, and a local Elastic. Nothing touches production or third-party networks.

## Why this demo exists

Red teamers build redirector fleets so the implant never knows the real C2 address. Blue teamers almost never see the implant either - they see *infrastructure telemetry*: proxy and redirector access logs shipped to a SIEM. This demo shows both sides at once, with NightAgents acting as the agentic blue-team control plane that turns a beacon fingerprint into a verified, human-approved, audited response.

## The five blue-team detection methods (what the demo shows)

NightAgents now watches for every way blue teams catch redirectors, live:

1. **Beacon rhythm** - the implant's metronome in redirector access logs (`DetectionService`).
2. **VirusTotal flag** - a third party marks the domain malicious (`intel-*` incident, high).
3. **Shodan exposure** - host profile published: open ports, service banners, cert fingerprint.
4. **Certificate-transparency logs** - a new cert for the domain appears in public CT logs.
5. **Abuse complaint** - the hosting provider receives a C2 report.

Methods 2–5 arrive through the **Third-Party MCP monitor** (`ThirdPartyMonitorService`), which polls configured intel feeds and opens provenance-tagged `intel-*` incidents with Slack alerts. In the lab, `demo/scripts/thirdparty_feed.py` simulates the providers over HTTPS (clearly labeled); in production, point `NIGHTAGENTS_THIRD_PARTY_FEEDS_JSON` at the real APIs - the code path is identical.

## The four redirection techniques

The fleet (`docker-compose.demo.yml`) brings up four nginx redirectors, all forwarding to the same Mythic `http` C2 profile on the host:

| # | Name | Config | Technique |
|---|------|--------|-----------|
| 1 | `redirector-pass` | `nginx/pass.conf` | Plain reverse proxy. Everything is forwarded. Simple, noisy, easy to burn. |
| 2 | `redirector-filter` | `nginx/filter.conf` | **Traffic shaping / allowlisting.** Only implant-shaped requests (matching User-Agent and URIs) reach the C2; everything else gets a 302 to a decoy page. Curious analysts and scanners never touch the real C2. |
| 3 | `redirector-front` | `nginx/front.conf` | **Vhost hiding (domain fronting analogue).** One IP serves two virtual hosts: `weather-front.demo` returns a benign decoy site, while the same IP with the `cdn-metrics.demo` Host header silently proxies to the C2. The C2 hides behind a legit-looking front on the same address. |
| 4 | `redirector-ab` | `nginx/ab.conf` | **A/B path splitting.** One domain serves real content under `/static/*` while `/index` and `/data` are C2 endpoints. The domain carries plausible cover traffic and C2 traffic simultaneously. |

Key teaching point: the implant is configured with `callback_host = http://redirector-pass.demo` - it never learns the real Mythic address. Burning a redirector does not burn the C2. That is the whole economic argument for redirection, and it is exactly the asset-rotation workflow NightAgents automates on the defensive side.

## The blue-team cycle, stage by stage

`scripts/run_demo.py` narrates each stage as it runs:

- **Stage 0 - Preflight.** Verify Elastic, the NightAgents API, and the real Mythic are healthy; mint a fresh Mythic API token; reconnect the NightAgents Mythic MCP server to the real instance and wait for readiness.
- **Stage 1 - Red: build the implant.** Mythic builds a Poseidon payload through the real `createpayload_webhook`, configured to beacon every ~10s through redirector 1. The binary never contains the C2 address.
- **Stage 2 - Red: execute.** The redirector fleet and a victim container start; the implant runs on the victim and calls home. The driver waits for a **live callback in the real Mythic** before proceeding.
- **Stage 3 - Blue: third-party monitoring.** A NightAgents engagement is registered (named to match the Mythic operation), redirector assets are registered with verified health, and nginx access logs flow into the `logs-redirector-demo` Elastic data stream, tagged with the engagement id.
- **Stage 4 - Blue: discovery + fingerprinting.** The detector queries Elastic for the beacon rhythm: periodic GETs to `/index`, POSTs to `/data`, one client IP, one fixed User-Agent. The fingerprint - intervals, URIs, UA, victim IP - goes into a signed, HMAC-authenticated incident webhook. NightAgents accepts it only with a valid signature and replay protection.
- **Stage 5 - Blue: validate → assess → decide → act.** NightWatcher runs the durable investigation graph: it independently verifies evidence-source health (negative evidence never counts unless the source is proven healthy), pulls the Elastic evidence, and queries the **real Mythic callback inventory through MCP**. It emits a signed handoff; a human operator approves it; NightRouter plans a route migration from the burned redirector to the standby, executes it through the Safety Kernel, and verifies callback continuity.
- **Stage 6 - Blue: evidence trail.** Audit-chain verification, prediction calibration, and an operations summary.

### The deliberate teaching twist

The route migration in stage 5 is expected to end in `rolled_back`. Mythic callbacks do not record which redirector carried them, so callback continuity after a route switch is **unknown** - and NightAgents' rule is that unknown continuity triggers rollback or operator intervention, never a reported success. The platform refuses to claim a win it cannot prove. That is the Safety Kernel doing its job, live.

## Running it

Step-by-step setup (stack configuration, Mythic lab prerequisites, troubleshooting): **[demo/GUIDE.md](GUIDE.md)**.

Prerequisites:

- Docker + docker compose
- The NightAgents stack up: `docker compose up -d` (see `docs/DEPLOYMENT.md`)
- A local Mythic install with the `poseidon` agent and `http` C2 profile running, reachable at `https://127.0.0.1:7443`, with an operation named **Operation Chimera** (the driver reads lab admin credentials from `~/mythic-install/.env` - nothing is hardcoded)

Then, from the repo root:

```bash
python demo/scripts/run_demo.py                 # full red+blue narrative run
# or, for range hosting: provision once, then let candidates run blue-only
bash demo/scripts/lab_provision.sh              # range owner, once
python demo/scripts/run_demo.py --blue-only     # candidates, repeatable
```

Cleanup (tears down the fleet and removes working files):

```bash
bash demo/scripts/cleanup.sh
```

## Files

- `docker-compose.demo.yml` - redirector fleet, victim, and log shipper
- `nginx/*.conf`, `nginx/decoy.html` - the four redirection techniques
- `scripts/run_demo.py` - staged demo driver with narration
- `scripts/mythic_api.py` - minimal Mythic 3.x API client (stdlib only)
- `scripts/shipper.py` - tails redirector access logs into Elastic, tagged per engagement
- `scripts/cleanup.sh` - teardown

## Safety

This demo is intentionally defensive in scope: it runs entirely on local lab containers, the payload beacons only to local redirectors, and the NightAgents side demonstrates detection, evidence handling, human approval, and safe rollback. It contains no guardrail bypasses, stealth/evasion automation, or deployment against anything outside the lab.
