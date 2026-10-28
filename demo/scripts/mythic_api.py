#!/usr/bin/env python3
"""Small Mythic 3.x API client for the NightAgents live demo.

Covers exactly what the demo needs: login, API-token creation, GraphQL
queries, payload creation, build polling and payload download. Lab use only.
"""

import json
import ssl
import time
import urllib.error
import urllib.request

MYTHIC_URL = "https://127.0.0.1:7443"

_SSL_CONTEXT = ssl.create_default_context()
_SSL_CONTEXT.check_hostname = False
_SSL_CONTEXT.verify_mode = ssl.CERT_NONE


class MythicError(RuntimeError):
    pass


def _request(method: str, path: str, body: dict | None = None, token: str | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{MYTHIC_URL}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60, context=_SSL_CONTEXT) as response:
            raw = response.read() or b"{}"
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"raw": raw.decode(errors="replace")}
    except urllib.error.HTTPError as exc:
        raw = exc.read() or b"{}"
        try:
            return {"http_status": exc.code, **json.loads(raw)}
        except json.JSONDecodeError:
            return {"http_status": exc.code, "raw": raw.decode(errors="replace")}


def login(username: str, password: str) -> str:
    result = _request("POST", "/auth", {"username": username, "password": password})
    token = result.get("access_token")
    if not token:
        raise MythicError(f"Mythic login failed: {result}")
    return token


def generate_apitoken(jwt: str, name: str) -> str:
    result = _request(
        "POST",
        "/api/v1.4/generate_apitoken_webhook",
        {"input": {"token_type": "User", "name": name}},
        token=jwt,
    )
    if result.get("status") != "success":
        raise MythicError(f"API token generation failed: {result}")
    return result["token_value"]


def graphql(query: str, token: str, variables: dict | None = None) -> dict:
    result = _request("POST", "/graphql/", {"query": query, "variables": variables or {}}, token=token)
    if result.get("errors"):
        raise MythicError(f"GraphQL error: {result['errors']}")
    return result.get("data", {})


def create_payload(jwt: str, definition: dict) -> str:
    result = _request(
        "POST",
        "/api/v1.4/createpayload_webhook",
        {"input": {"payloadDefinition": json.dumps(definition)}},
        token=jwt,
    )
    if result.get("status") != "success":
        raise MythicError(f"payload creation failed: {result}")
    return result["uuid"]


PAYLOAD_QUERY = """
query PayloadStatus($uuid: String!) {
  payload(where: {uuid: {_eq: $uuid}}) {
    uuid
    build_phase
    build_stderr
    file_id
    deleted
  }
}
"""


FILEMETA_QUERY = """
query FileMeta($id: Int!) {
  filemeta(where: {id: {_eq: $id}}) {
    agent_file_id
  }
}
"""


def wait_for_payload(token: str, uuid: str, timeout_seconds: float = 600) -> str:
    """Wait for the build to finish; return the agent file id for download.

    ``payload.file_id`` is the numeric filemeta primary key; the download
    endpoint expects the ``agent_file_id`` UUID instead, so resolve it here.
    """
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        data = graphql(PAYLOAD_QUERY, token, {"uuid": uuid})
        rows = data.get("payload", [])
        if not rows:
            raise MythicError(f"payload {uuid} not found")
        row = rows[0]
        phase = row.get("build_phase", "")
        if phase.lower() == "success":
            if not row.get("file_id"):
                raise MythicError(f"payload {uuid} built without a file id")
            files = graphql(FILEMETA_QUERY, token, {"id": row["file_id"]}).get("filemeta", [])
            if not files or not files[0].get("agent_file_id"):
                raise MythicError(f"payload {uuid} has no downloadable file")
            return files[0]["agent_file_id"]
        if phase.lower() in {"error", "failed"}:
            raise MythicError(f"payload build failed: {row.get('build_stderr')}")
        time.sleep(5)
    raise MythicError(f"timed out waiting for payload {uuid} to build")


def download_payload(token: str, file_id: str) -> bytes:
    req = urllib.request.Request(f"{MYTHIC_URL}/direct/download/{file_id}")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=120, context=_SSL_CONTEXT) as response:
        return response.read()


CALLBACK_QUERY = """
query Callbacks($operation: String!) {
  callback(where: {operation: {name: {_eq: $operation}}}) {
    id
    agent_callback_id
    active
    dead
    last_checkin
    integrity_level
    ip
    host
    user
  }
}
"""


def list_callbacks(token: str, operation: str) -> list[dict]:
    return graphql(CALLBACK_QUERY, token, {"operation": operation}).get("callback", [])
