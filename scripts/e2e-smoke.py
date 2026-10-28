#!/usr/bin/env python3
"""End-to-end smoke test for the NightAgents Compose stack.

Runs against the locally published ports (API on 127.0.0.1:8000, Elasticsearch
on 127.0.0.1:9200) and drives the full defensive workflow:

engagement -> redirectors -> seeded Elastic evidence -> signed incident webhook
-> NightWatcher investigation -> signed handoff -> operator approval ->
NightRouter execution -> continuity-driven rollback -> audit/ops verification.

Stdlib only. Exits non-zero on the first failed expectation.
"""

import hashlib
import hmac
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

API = "http://127.0.0.1:8000"
ELASTIC = "http://127.0.0.1:9200"
WEBHOOK_SECRET = "e2e-test-elastic-webhook-secret-0123456789abcdef0"
OPERATOR = "development-operator"

CHECKS: list[str] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    CHECKS.append(f"[{status}] {name}" + (f" - {detail}" if detail else ""))
    print(CHECKS[-1], flush=True)
    if not ok:
        print("\nE2E FAILED", file=sys.stderr)
        sys.exit(1)


def request(method: str, url: str, body: dict | None = None, headers: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            raw = response.read() or b"{}"
            return response.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read() or b"{}"
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw.decode(errors="replace")}


def wait_for(url: str, timeout: float = 180) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status == 200:
                    return
        except Exception:
            pass
        time.sleep(3)
    record(f"wait for {url}", False, "timed out")


def sign(secret: str, timestamp: str, nonce: str, body: bytes) -> str:
    message = timestamp.encode() + b"." + nonce.encode() + b"." + body
    return "sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def main() -> None:
    wait_for(f"{API}/health")
    record("api health", True)

    wait_for(f"{ELASTIC}/_cluster/health")
    record("elasticsearch health", True)

    _, ready = request("GET", f"{API}/health/ready")
    record("readiness", ready.get("status") == "ready", json.dumps(ready.get("mcp_issues", [])))
    record("mcp contract validation at readiness", ready.get("mcp_ready") is True)

    _, mcp = request("GET", f"{API}/api/v2/operations/mcp-health")
    record("mcp discovery", mcp.get("validation", {}).get("valid") is True)
    record(
        "mcp ping health",
        all(item.get("healthy") for item in mcp.get("health_check", {}).values()),
        json.dumps({k: v.get("healthy") for k, v in mcp.get("health_check", {}).items()}),
    )

    suffix = uuid.uuid4().hex[:8]
    engagement_name = f"e2e-engagement-{suffix}"
    _, engagement = request("POST", f"{API}/api/v2/engagements", {"name": engagement_name})
    engagement_id = engagement["engagement_id"]
    record("engagement created", bool(engagement_id), engagement_name)

    redirectors = {}
    for label, external in (("old", f"redir-e2e-old-{suffix}"), ("new", f"redir-e2e-new-{suffix}")):
        # The new redirector registers under a standby route so the primary
        # logical route keeps targeting the old redirector until execution.
        route = f"route-e2e-{suffix}" if label == "old" else f"route-e2e-standby-{suffix}"
        _, asset = request(
            "POST",
            f"{API}/api/v2/engagements/{engagement_id}/redirectors",
            {
                "redirector_id": external,
                "domain": f"{label}.e2e.example.test",
                "logical_route_id": route,
                "provider_resource_id": f"res-{label}-{suffix}",
                "mythic_profile": "http",
                "failover_group": "e2e",
            },
        )
        redirectors[label] = {"asset_id": asset["asset_id"], "external_id": external}
        request(
            "PUT",
            f"{API}/api/v2/engagements/{engagement_id}/redirectors/{external}/health",
            {"status": "healthy", "evidence": {"source": "e2e"}},
        )
    record("redirectors registered and healthy", len(redirectors) == 2)

    # Point the Mythic stub at this engagement so the callback inventory
    # contains one active callback and continuity verification is exercised.
    stub_dir = Path("/tmp/mythic-stub-data")
    stub_dir.mkdir(parents=True, exist_ok=True)
    (stub_dir / "operation.txt").write_text(engagement_name)

    now = datetime.now(UTC).isoformat()
    source = f"logs-nightagents-e2e-{suffix}"
    request(
        "PUT",
        f"{ELASTIC}/_index_template/{source}",
        {
            "index_patterns": [f"{source}*"],
            "data_stream": {},
            "template": {"mappings": {"properties": {"@timestamp": {"type": "date"}}}},
        },
    )
    status, _ = request("PUT", f"{ELASTIC}/_data_stream/{source}")
    record("evidence data stream created", status in (200, 201))
    for index in range(3):
        request(
            "POST",
            f"{ELASTIC}/{source}/_doc?refresh=true",
            {
                "@timestamp": now,
                "nightagents": {
                    "engagement_id": engagement_id,
                    "redirector_id": redirectors["old"]["asset_id"],
                },
                "event": {"action": "http_request", "category": "web"},
                "message": f"e2e evidence event {index}",
            },
        )

    alert_id = f"alert-e2e-{suffix}"
    status, _ = request(
        "PUT",
        f"{ELASTIC}/.alerts-security.alerts-e2e/_doc/{alert_id}?refresh=true",
        {
            "@timestamp": now,
            "nightagents": {"engagement_id": engagement_id},
            "kibana": {"alert": {"rule": {"name": "e2e-detection-rule"}}},
            "event": {"action": "detection", "category": "intrusion_detection"},
        },
    )
    record("alert document seeded", status in (200, 201))

    webhook_body = json.dumps(
        {
            "schema_version": "1.0",
            "external_alert_id": alert_id,
            "engagement_id": engagement_id,
            "redirector_asset_id": redirectors["old"]["asset_id"],
            "severity": "high",
            "rule_id": "e2e-detection-rule",
            "summary": "E2E simulated detection for authorized infrastructure",
            "evidence_refs": [f"elastic:{alert_id}"],
            "source_health": {source: {}},
            "occurred_at": now,
        }
    ).encode()
    timestamp = str(int(time.time()))
    nonce = f"e2e-{uuid.uuid4().hex}"

    def post_signed_webhook(body: bytes, ts: str, nc: str) -> tuple[int, dict]:
        req = urllib.request.Request(f"{API}/api/v2/webhooks/elastic/incidents", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-NightAgents-Timestamp", ts)
        req.add_header("X-NightAgents-Nonce", nc)
        req.add_header("X-NightAgents-Signature", sign(WEBHOOK_SECRET, ts, nc, body))
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    status, accepted = post_signed_webhook(webhook_body, timestamp, nonce)
    record("signed incident webhook accepted", status == 202 and not accepted.get("duplicate"), json.dumps(accepted))
    incident_id = accepted["incident_id"]

    status, replay = post_signed_webhook(webhook_body, timestamp, nonce)
    record("webhook nonce replay rejected", status == 409, f"status={status} body={json.dumps(replay)[:120]}")

    bad_sig_body = webhook_body
    bad_ts = str(int(time.time()))
    bad_nonce = f"e2e-{uuid.uuid4().hex}"
    req = urllib.request.Request(f"{API}/api/v2/webhooks/elastic/incidents", data=bad_sig_body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-NightAgents-Timestamp", bad_ts)
    req.add_header("X-NightAgents-Nonce", bad_nonce)
    req.add_header("X-NightAgents-Signature", "sha256=" + "0" * 64)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            bad_status = response.status
    except urllib.error.HTTPError as exc:
        bad_status = exc.code
    record("unsigned webhook rejected", bad_status == 401, f"status={bad_status}")

    status, handoff = request("POST", f"{API}/api/v2/incidents/{incident_id}/investigate")
    record("investigation executed", status == 200, handoff.get("disposition", ""))
    handoff_id = handoff.get("handoff_id")
    record("signed handoff produced", bool(handoff_id), f"disposition={handoff.get('disposition')}")

    status, approval = request(
        "POST",
        f"{API}/api/v2/handoffs/{handoff_id}/approve",
        {"operator_id": OPERATOR},
    )
    record("operator approval", status == 200 and approval.get("status") == "approved", json.dumps(approval))

    status, execution = request(
        "POST",
        f"{API}/api/v2/handoffs/{handoff_id}/execute",
        {
            "logical_route_id": f"route-e2e-{suffix}",
            "old_redirector_id": redirectors["old"]["external_id"],
            "new_redirector_id": redirectors["new"]["external_id"],
        },
    )
    record("execution attempted", status == 200, json.dumps(execution.get("state")))
    record(
        "unknown continuity triggers rollback",
        execution.get("state") in {"rolled_back", "rollback_completed", "completed_with_rollback"}
        or "rollback" in str(execution.get("state", "")),
        f"state={execution.get('state')}",
    )

    _, audit = request("GET", f"{API}/api/v2/audit/verify")
    record("audit chain intact", audit.get("valid") is True, json.dumps(audit))

    _, summary = request("GET", f"{API}/api/v2/operations/summary")
    record("operations summary", isinstance(summary, dict), json.dumps(summary)[:300])

    _, calibration = request("GET", f"{API}/api/v2/engagements/{engagement_id}/prediction-calibration")
    record("calibration endpoint", "buckets" in calibration, json.dumps(calibration.get("warnings", [])))

    print("\nE2E OK -", len(CHECKS), "checks passed")


if __name__ == "__main__":
    main()
