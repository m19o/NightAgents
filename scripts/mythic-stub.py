#!/usr/bin/env python3
"""Minimal Mythic GraphQL stub for local end-to-end testing.

Serves POST /graphql/ with a callback inventory so NightWatcher's evidence
verifier sees a complete inventory without a real Mythic deployment.

If /data/operation.txt exists, the inventory contains one active callback for
the operation named in that file (the NightAgents engagement name), which
drives the callback-continuity verification path. Otherwise the inventory is
empty. Lab/testing use only - never a Mythic replacement.
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

OPERATION_FILE = Path("/data/operation.txt")


def inventory() -> dict:
    try:
        operation = OPERATION_FILE.read_text().strip()
    except OSError:
        operation = ""
    if not operation:
        return {"data": {"callback": []}}
    return {
        "data": {
            "callback": [
                {
                    "agent_callback_id": "stub-callback-1",
                    "operation": {"name": operation},
                    "payload": {
                        "payloadtype": {"name": "stub-payload"},
                        "payloadc2profiles": [{"c2profile": {"name": "http"}}],
                    },
                    "last_seen": None,
                    "active": True,
                    "dead": False,
                    "sleep_info": "",
                }
            ]
        }
    }


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        body = json.dumps(inventory()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        return


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
