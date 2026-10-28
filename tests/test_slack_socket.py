import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from nightagents.config import Settings
from nightagents.db.base import Base
from nightagents.db.models import EngagementRecord, HandoffRecord, IncidentRecord
from nightagents.db.session import build_engine
from nightagents.notifications import slack_socket
from nightagents.notifications.slack_socket import SlackSocketListener, _ReconnectRequested
from nightagents.security.signing import sign_hash

HANDOFF_SECRET = "test-handoff-signing-secret-32-chars!!"
APPROVAL_SECRET = "test-approval-signing-secret-32chars!"


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))


def _factory_with_handoff():
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    engagement = EngagementRecord(name="socket-ops")
    db.add(engagement)
    db.flush()
    incident = IncidentRecord(
        engagement_id=engagement.id,
        external_id="alert-socket",
        severity="high",
        rule_id="rule",
        summary="synthetic",
        occurred_at=datetime.now(UTC),
    )
    db.add(incident)
    db.flush()
    payload_hash = hashlib.sha256(b"socket-handoff").hexdigest()
    handoff = HandoffRecord(
        engagement_id=engagement.id,
        incident_id=incident.id,
        payload_json={"prediction": {"event": "profiling"}},
        payload_hash=payload_hash,
        signature=sign_hash(HANDOFF_SECRET, payload_hash),
        status="proposed",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    db.add(handoff)
    db.commit()
    db.close()
    return factory, handoff.id


def _settings() -> Settings:
    return Settings(
        slack_app_token="xapp-test-token",
        handoff_signing_secret=HANDOFF_SECRET,
        approval_signing_secret=APPROVAL_SECRET,
    )


def _envelope(action_id: str, handoff_id: str) -> dict:
    return {
        "envelope_id": "env-1",
        "type": "interactive",
        "payload": {
            "type": "block_actions",
            "user": {"id": "U9", "username": "socket-operator"},
            "actions": [{"action_id": action_id, "value": handoff_id}],
        },
    }


def test_listener_requires_app_token() -> None:
    with pytest.raises(ValueError, match="slack_app_token"):
        SlackSocketListener(Settings())


async def test_interactive_envelope_acked_then_approves(monkeypatch) -> None:
    factory, handoff_id = _factory_with_handoff()
    monkeypatch.setattr(slack_socket, "SessionLocal", factory)
    ws = FakeWS()
    listener = SlackSocketListener(_settings())
    await listener._handle_envelope(ws, _envelope("nightagents_approve", handoff_id))
    assert ws.sent[0] == {"envelope_id": "env-1"}  # acked before any work
    db = factory()
    assert db.get(HandoffRecord, handoff_id).status == "approved"
    db.close()


async def test_non_interactive_envelope_acked_and_ignored(monkeypatch) -> None:
    factory, handoff_id = _factory_with_handoff()
    monkeypatch.setattr(slack_socket, "SessionLocal", factory)
    ws = FakeWS()
    listener = SlackSocketListener(_settings())
    await listener._handle_envelope(ws, {"envelope_id": "env-2", "type": "hello"})
    assert ws.sent == [{"envelope_id": "env-2"}]
    db = factory()
    assert db.get(HandoffRecord, handoff_id).status == "proposed"
    db.close()


async def test_disconnect_envelope_requests_reconnect() -> None:
    ws = FakeWS()
    listener = SlackSocketListener(_settings())
    with pytest.raises(_ReconnectRequested):
        await listener._handle_envelope(ws, {"envelope_id": "env-3", "type": "disconnect", "reason": "link_shared"})
    assert ws.sent == [{"envelope_id": "env-3"}]
