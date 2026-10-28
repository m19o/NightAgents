#!/usr/bin/env python3
"""Redirector access-log shipper for the NightAgents live demo.

Tails the shared nginx access log and bulk-indexes entries into the
logs-redirector-demo data stream in Elasticsearch, tagging every event with
the NightAgents engagement and redirector asset so the investigation
catalog queries can find it. This is the "third-party monitoring" feed:
the defender only ever sees redirector/proxy telemetry, never the implant.
"""

import json
import os
import time
import urllib.request
from pathlib import Path

ES_URL = os.environ.get("NIGHTAGENTS_DEMO_ES_URL", "http://host.docker.internal:9200")
LOG_PATH = Path("/redirector-logs/access.log")
CONFIG_PATH = Path("/shipper-config/config.json")
DATA_STREAM = "logs-redirector-demo"
STATE_PATH = Path("/shipper-config/shipper.offset")


def wait_for_config() -> dict:
    while True:
        try:
            return json.loads(CONFIG_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            time.sleep(2)


def post_bulk(lines: list[str]) -> int:
    body = "".join(f'{{"create":{{}}}}\n{line}\n' for line in lines)
    req = urllib.request.Request(
        f"{ES_URL}/{DATA_STREAM}/_bulk",
        data=body.encode(),
        method="POST",
    )
    req.add_header("Content-Type", "application/x-ndjson")
    with urllib.request.urlopen(req, timeout=30) as response:
        payload = json.loads(response.read() or b"{}")
    return 0 if not payload.get("errors") else 1


def main() -> None:
    config = wait_for_config()
    engagement_id = config["engagement_id"]
    redirector_id = config["redirector_asset_id"]
    print(f"shipper: tagging events with engagement={engagement_id} redirector={redirector_id}", flush=True)
    offset = 0
    if STATE_PATH.exists():
        try:
            offset = int(STATE_PATH.read_text().strip())
        except ValueError:
            offset = 0
    while True:
        try:
            size = LOG_PATH.stat().st_size
        except OSError:
            time.sleep(2)
            continue
        if size < offset:
            offset = 0
        if size == offset:
            time.sleep(1)
            continue
        with LOG_PATH.open("rb") as handle:
            handle.seek(offset)
            raw = handle.read()
            offset = handle.tell()
        docs = []
        for line in raw.decode(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            event["nightagents"] = {"engagement_id": engagement_id, "redirector_id": redirector_id}
            event["event"] = {"kind": "event", "category": "web", "dataset": "demo.redirector"}
            docs.append(json.dumps(event))
        if docs:
            delivered = False
            for attempt in range(5):
                try:
                    post_bulk(docs)
                    delivered = True
                    break
                except Exception as exc:  # noqa: BLE001 - demo resilience
                    print(f"shipper: bulk post failed ({exc}); retrying", flush=True)
                    time.sleep(2 * (attempt + 1))
            if not delivered:
                # Keep the offset so the next iteration retries the same batch
                # instead of silently dropping evidence.
                offset -= len(raw)
                time.sleep(5)
                continue
        STATE_PATH.write_text(str(offset))


if __name__ == "__main__":
    main()
