import json

import httpx

from nightagents.cli import OperatorClient, build_parser, dispatch


def test_cli_redirector_registration_builds_expected_request() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        captured["authorization"] = request.headers.get("Authorization")
        return httpx.Response(201, json={"asset_id": "asset-1", "state": "registered"})

    args = build_parser().parse_args(
        ["redirector-register", "eng-1", "r-1", "edge.test", "route-1", "provider-1", "http", "fg-1"]
    )
    client = OperatorClient("https://nightagents.test", "token", httpx.MockTransport(handler))
    try:
        result = dispatch(client, args, "operator")
    finally:
        client.close()
    assert result["state"] == "registered"
    assert captured["path"] == "/api/v2/engagements/eng-1/redirectors"
    assert captured["body"]["logical_route_id"] == "route-1"
    assert captured["authorization"] == "Bearer token"


def test_cli_surfaces_api_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "dependency unavailable"})

    client = OperatorClient("https://nightagents.test", transport=httpx.MockTransport(handler))
    try:
        try:
            client.request("GET", "/health/ready")
        except RuntimeError as exc:
            assert "dependency unavailable" in str(exc)
        else:
            raise AssertionError("API error was not raised")
    finally:
        client.close()
