import asyncio
import json

import httpx
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from nightagents.db.base import Base
from nightagents.db.models import MetricRecord
from nightagents.db.session import build_engine
from nightagents.notifications.slack import SlackNotifier


def _db():
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _metrics(db, name: str) -> list[MetricRecord]:
    return list(db.scalars(select(MetricRecord).where(MetricRecord.name == name)))


def test_disabled_without_webhook_url() -> None:
    db = _db()
    notifier = SlackNotifier(db, "")
    assert not notifier.enabled
    assert asyncio.run(notifier.notify("hello")) is False
    assert _metrics(db, "slack.notification") == []


def test_handoff_notification_posts_blocks_and_records_metric() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    db = _db()
    notifier = SlackNotifier(db, "https://hooks.slack.test/services/T/B/xxx", transport=transport)
    result = asyncio.run(
        notifier.handoff_pending_approval(
            {
                "handoff_id": "h-1",
                "disposition": "propose",
                "prediction": {"event": "profiling", "probability": 0.7, "phase": "discovery"},
            }
        )
    )
    assert result is True
    assert len(requests) == 1
    body = requests[0]
    assert "h-1" in body["text"]
    assert any("blocks" in body for _ in [1])
    metric = _metrics(db, "slack.notification")[0]
    assert metric.value == 1.0
    assert metric.dimensions_json["outcome"] == "sent"


def test_execution_notification_rolls_back_wording() -> None:
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, text="ok")

    db = _db()
    notifier = SlackNotifier(db, "https://hooks.slack.test/services/T/B/xxx", transport=httpx.MockTransport(handler))
    asyncio.run(notifier.execution_finished("h-9", {"state": "rolled_back"}))
    text = captured[0]["text"]
    assert "rolled_back" in text


def test_slack_failure_is_swallowed_and_recorded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    db = _db()
    notifier = SlackNotifier(db, "https://hooks.slack.test/services/T/B/xxx", transport=httpx.MockTransport(handler))
    assert asyncio.run(notifier.notify("hello")) is False
    metric = _metrics(db, "slack.notification")[0]
    assert metric.value == 0.0
    assert metric.dimensions_json["outcome"] == "failed"


def test_network_error_is_swallowed_and_recorded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    db = _db()
    notifier = SlackNotifier(db, "https://hooks.slack.test/services/T/B/xxx", transport=httpx.MockTransport(handler))
    assert asyncio.run(notifier.notify("hello")) is False
    assert _metrics(db, "slack.notification")[0].dimensions_json["outcome"] == "failed"


def test_web_api_token_posts_to_chat_postmessage() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "ts": "1.2"})

    db = _db()
    notifier = SlackNotifier(
        db, api_token="xoxp-test", channel="C123", transport=httpx.MockTransport(handler)
    )
    assert notifier.enabled
    assert asyncio.run(notifier.notify("hello", {"k": "v"})) is True
    request = requests[0]
    assert request.url.path == "/api/chat.postMessage"
    assert request.headers["Authorization"] == "Bearer xoxp-test"
    body = json.loads(request.content)
    assert body["channel"] == "C123"
    assert "blocks" in body


def test_web_api_error_response_marks_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})

    db = _db()
    notifier = SlackNotifier(
        db, api_token="xoxp-test", channel="C404", transport=httpx.MockTransport(handler)
    )
    assert asyncio.run(notifier.notify("hello")) is False
    assert _metrics(db, "slack.notification")[0].dimensions_json["outcome"] == "failed"


def test_per_engagement_channel_routing() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    db = _db()
    notifier = SlackNotifier(
        db,
        api_token="xoxp-test",
        channel="C-default",
        channel_map={"eng-alice": "C-alice", "eng-bob": "C-bob"},
        transport=httpx.MockTransport(handler),
    )
    assert notifier.enabled
    asyncio.run(notifier.notify("for alice", engagement_id="eng-alice"))
    asyncio.run(notifier.notify("for bob", engagement_id="eng-bob"))
    asyncio.run(notifier.notify("fallback", engagement_id="eng-unknown"))
    asyncio.run(notifier.notify("no engagement"))
    channels = [body["channel"] for body in requests]
    assert channels == ["C-alice", "C-bob", "C-default", "C-default"]
