"""Offline teaching sandbox for the Cloudflare adapter.

NOT production. An httpx MockTransport that speaks Cloudflare's real API v4 +
GraphQL request/response contract in memory, so the unmodified adapter code
runs without a live account for teaching and tests. Set
``NIGHTAGENTS_CLOUDFLARE_SANDBOX=false`` with a real scoped token and the same
code path hits api.cloudflare.com instead.

Ported from the NightCTL control plane.
"""

import json

import httpx

_ANALYTICS = {
    "healthy": {"total": 5000, "edge5xx": 6, "origin5xx": 12, "firewall": 4},
    "takedown": {"total": 4200, "edge5xx": 30, "origin5xx": 2700, "firewall": 520},
}


class SandboxState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.scenario = "healthy"
        self.zone_status = "active"
        self.security_level = "medium"
        self.dns = {
            "id": "rec_teach_1",
            "type": "A",
            "name": "cdn.example.com",
            "content": "203.0.113.10",
            "proxied": True,
            "comment": "",
        }


STATE = SandboxState()


def _ok(result) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "errors": [], "messages": [], "result": result})


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    method = request.method

    if path.endswith("/user/tokens/verify"):
        return _ok({"id": "tok_sandbox", "status": "active"})

    if path.endswith("/graphql"):
        body = json.loads(request.content or b"{}")
        zone = (body.get("variables") or {}).get("zone", "")
        counts = _ANALYTICS[STATE.scenario]
        data = {
            "viewer": {
                "zones": [
                    {
                        "total": [{"count": counts["total"]}],
                        "edge5xx": [{"count": counts["edge5xx"]}],
                        "origin5xx": [{"count": counts["origin5xx"]}],
                        "firewall": [{"count": counts["firewall"]}],
                    }
                ]
                if zone
                else []
            }
        }
        return httpx.Response(200, json={"data": data})

    if "/dns_records/" in path:
        if method == "GET":
            return _ok(dict(STATE.dns))
        if method == "PATCH":
            payload = json.loads(request.content or b"{}")
            allowed = ("content", "proxied", "type", "name", "comment")
            STATE.dns.update({key: value for key, value in payload.items() if key in allowed})
            return _ok(dict(STATE.dns))

    if path.endswith("/settings/security_level"):
        if method == "GET":
            return _ok({"id": "security_level", "value": STATE.security_level})
        if method == "PATCH":
            payload = json.loads(request.content or b"{}")
            STATE.security_level = payload.get("value", STATE.security_level)
            return _ok({"id": "security_level", "value": STATE.security_level})

    if "/zones/" in path and method == "GET":
        return _ok({"id": "zone_sandbox", "name": "example.com", "status": STATE.zone_status})

    return httpx.Response(
        404,
        json={
            "success": False,
            "errors": [{"message": f"sandbox: unhandled {method} {path}"}],
            "result": None,
        },
    )


def sandbox_transport() -> httpx.MockTransport:
    return httpx.MockTransport(_handler)


def set_scenario(scenario: str) -> None:
    """'healthy' | 'takedown' - used by teaching drivers and tests."""
    STATE.reset()
    STATE.scenario = scenario
