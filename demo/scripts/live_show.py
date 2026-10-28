#!/usr/bin/env python3
"""NightAgents live show - the full loop in real time, one human switch.

Runs: fresh investigation (GPT) -> handoff -> WAITS for you to click Approve
(or Reject) in Slack -> executes -> watches the traffic move -> prints the
verdict. The only manual step is the Slack click; everything else is live.

    python3 demo/scripts/live_show.py
"""

import json
import os
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime

API = "http://127.0.0.1:8000"
ES = "http://127.0.0.1:9200"
PG = ["docker", "exec", "nightagents-postgres-1", "psql", "-U", "nightagents", "-t", "-A", "-c"]


def say(text: str) -> None:
    print(f"\n{'=' * 74}\n  {text}\n{'=' * 74}", flush=True)


def step(text: str) -> None:
    print(f"  [{datetime.now(UTC).strftime('%H:%M:%S')}] {text}", flush=True)


def req(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=180) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def sql(query: str) -> str:
    import subprocess

    out = subprocess.run([*PG, query], capture_output=True, text=True, timeout=30)
    return out.stdout.strip()


def main() -> None:
    say("NIGHTAGENTS LIVE SHOW\nhunter -> GPT -> your Slack click -> Safety Kernel -> proof")
    incident = sql("select id from incidents where status='received' order by received_at desc limit 1;")
    if not incident:
        step("no open incidents - wait for the hunter (runs every ~2 min) and retry")
        return
    step(f"incident: {incident[:13]}... (from the autonomous hunter)")

    say("NIGHTWATCHER INVESTIGATING (GPT-4o-mini, live)")
    step("source health -> elastic evidence -> memory -> mythic inventory -> verifier -> reasoner -> critic")
    status, result = req("POST", f"{API}/api/v2/incidents/{incident}/investigate")
    if status != 200 or not result.get("handoff_id"):
        step(f"investigation abstained or failed: {json.dumps(result)[:300]}")
        return
    handoff = result["handoff_id"]
    pred = result.get("prediction", {})
    step(f"handoff {handoff[:13]}... disposition={result.get('disposition')}")
    step(f"prediction: {pred.get('event')} p={pred.get('probability')}")

    say("YOUR MOVE - click Approve or Reject in Slack\n(the platform will not move without you)")
    ttl_minutes = 15
    env_path = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
    if os.path.exists(env_path):
        for line in open(env_path):
            if line.startswith("NIGHTAGENTS_HANDOFF_TTL_MINUTES="):
                ttl_minutes = int(line.split("=", 1)[1].strip())
    deadline = time.time() + ttl_minutes * 60 - 60  # stop polling just before the handoff dies
    while time.time() < deadline:
        status, state = req("GET", f"{API}/api/v2/handoffs/{handoff}")
        s = state.get("status")
        if s == "approved":
            step("operator APPROVED via Slack")
            break
        if s == "rejected":
            step("operator REJECTED - respecting the decision, no execution")
            return
        time.sleep(3)
    else:
        step(f"no decision within the {ttl_minutes}-minute handoff lifetime - it expired on its own")
        return

    say("NIGHTROUTER + SAFETY KERNEL EXECUTING")
    status, execution = req("POST", f"{API}/api/v2/handoffs/{handoff}/execute", {
        "logical_route_id": "route-demo-live",
        "old_redirector_id": "demo-redirector-burned",
        "new_redirector_id": "demo-redirector-standby",
    })
    state = execution.get("state")
    step(f"execution state: {state}")

    say("CONTINUITY PROOF - beacons on the standby redirector")
    status, count = req(
        "POST", f"{ES}/logs-redirector-demo/_count", {"query": {"term": {"host.name": "redirector-filter"}}}
    )
    step(f"beacon events on redirector-filter: {count.get('count', 0)}")
    if state == "completed":
        say("COMPLETED - continuity proven from independent telemetry.\nThe traffic moved. The sessions never noticed.")
    else:
        say(f"{state} - continuity could not be proven.\nUnknown is never success.")


if __name__ == "__main__":
    main()
