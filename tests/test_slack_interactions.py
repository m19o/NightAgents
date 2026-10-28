import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from nightagents.api.main import create_app
from nightagents.config import Settings, get_settings
from nightagents.db.base import Base
from nightagents.db.models import EngagementRecord, HandoffRecord, IncidentRecord
from nightagents.db.session import build_engine, get_session
from nightagents.security.signing import sign_hash
from nightagents.security.slack_signing import verify_slack_signature

SIGNING_SECRET = "test-slack-signing-secret"
HANDOFF_SECRET = "test-handoff-signing-secret-32-chars!!"
APPROVAL_SECRET = "test-approval-signing-secret-32chars!"


def _signed_headers(body: bytes, timestamp: str | None = None) -> dict:
    ts = timestamp or str(int(time.time()))
    base = b"v0:" + ts.encode() + b":" + body
    signature = "v0=" + hmac.new(SIGNING_SECRET.encode(), base, hashlib.sha256).hexdigest()
    return {
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": signature,
        "Content-Type": "application/x-www-form-urlencoded",
    }


def _client_with_handoff(status: str = "proposed"):
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(
        slack_signing_secret=SIGNING_SECRET,
        handoff_signing_secret=HANDOFF_SECRET,
        approval_signing_secret=APPROVAL_SECRET,
    )
    app = create_app()

    def session_override():
        with factory() as db:
            yield db

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_settings] = lambda: settings

    db = factory()
    engagement = EngagementRecord(name="slack-ops")
    db.add(engagement)
    db.flush()
    incident = IncidentRecord(
        engagement_id=engagement.id,
        external_id="alert-slack",
        severity="high",
        rule_id="rule",
        summary="synthetic",
        occurred_at=datetime.now(UTC),
    )
    db.add(incident)
    db.flush()
    payload_hash = hashlib.sha256(b"handoff-payload").hexdigest()
    handoff = HandoffRecord(
        engagement_id=engagement.id,
        incident_id=incident.id,
        payload_json={"prediction": {"event": "profiling"}},
        payload_hash=payload_hash,
        signature=sign_hash(HANDOFF_SECRET, payload_hash),
        status=status,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    db.add(handoff)
    db.commit()
    db.close()
    return TestClient(app), handoff.id


def _payload(action_id: str, handoff_id: str) -> bytes:
    payload = json.dumps(
        {
            "type": "block_actions",
            "user": {"id": "U1", "username": "operator"},
            "response_url": "https://hooks.slack.test/response",
            "actions": [{"action_id": action_id, "value": handoff_id}],
        }
    )
    return f"payload={quote(payload)}".encode()


def test_signature_verification_accepts_valid_and_rejects_forged() -> None:
    body = b"payload={}"
    ts = str(int(time.time()))
    base = b"v0:" + ts.encode() + b":" + body
    good = "v0=" + hmac.new(SIGNING_SECRET.encode(), base, hashlib.sha256).hexdigest()
    assert verify_slack_signature(SIGNING_SECRET, ts, body, good) is True
    assert verify_slack_signature(SIGNING_SECRET, ts, body, "v0=" + "0" * 64) is False
    stale = str(int(time.time()) - 400)
    assert verify_slack_signature(SIGNING_SECRET, stale, body, good) is False


def test_slack_approve_action_approves_handoff() -> None:
    client, handoff_id = _client_with_handoff()
    body = _payload("nightagents_approve", handoff_id)
    response = client.post("/api/v2/slack/interactions", content=body, headers=_signed_headers(body))
    assert response.status_code == 200
    assert "approved by slack:operator" in response.json()["text"]
    assert client.get(f"/api/v2/handoffs/{handoff_id}").json()["status"] == "approved"


def test_slack_reject_action_rejects_handoff_and_blocks_double_decision() -> None:
    client, handoff_id = _client_with_handoff()
    body = _payload("nightagents_reject", handoff_id)
    response = client.post("/api/v2/slack/interactions", content=body, headers=_signed_headers(body))
    assert response.status_code == 200
    assert client.get(f"/api/v2/handoffs/{handoff_id}").json()["status"] == "rejected"
    again = client.post("/api/v2/slack/interactions", content=body, headers=_signed_headers(body))
    assert "not awaiting decision" in again.json()["text"]


def test_slack_interaction_rejects_bad_signature() -> None:
    client, handoff_id = _client_with_handoff()
    body = _payload("nightagents_approve", handoff_id)
    headers = _signed_headers(body)
    headers["X-Slack-Signature"] = "v0=" + "0" * 64
    response = client.post("/api/v2/slack/interactions", content=body, headers=headers)
    assert response.status_code == 401
    assert client.get(f"/api/v2/handoffs/{handoff_id}").json()["status"] == "proposed"
