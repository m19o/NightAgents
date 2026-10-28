import asyncio
import json
import time
from uuid import uuid4

import httpx

from nightagents.config import get_settings
from nightagents.db.models import OutboxEventRecord
from nightagents.security.webhook import sign_webhook
from nightagents.workers.scheduler import AgentScheduler


class SignedWebhookOutbox:
    def __init__(self, url: str, secret: str):
        if not url:
            raise ValueError("NIGHTAGENTS_OUTBOX_WEBHOOK_URL is required for the worker")
        if not url.startswith("https://") and get_settings().environment != "development":
            raise ValueError("production outbox delivery requires HTTPS")
        self.url = url
        self.secret = secret

    async def __call__(self, event: OutboxEventRecord) -> None:
        body = json.dumps(
            {
                "event_id": event.id,
                "event_type": event.event_type,
                "aggregate_type": event.aggregate_type,
                "aggregate_id": event.aggregate_id,
                "payload": event.payload_json,
                "available_at": event.available_at.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        timestamp = str(int(time.time()))
        nonce = str(uuid4())
        signature = sign_webhook(self.secret, timestamp, nonce, body)
        headers = {
            "Content-Type": "application/json",
            "X-NightAgents-Timestamp": timestamp,
            "X-NightAgents-Nonce": nonce,
            "X-NightAgents-Signature": f"sha256={signature}",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(self.url, content=body, headers=headers)
            response.raise_for_status()


async def serve() -> None:
    settings = get_settings()
    handler = SignedWebhookOutbox(settings.outbox_webhook_url, settings.outbox_webhook_secret)
    scheduler = AgentScheduler(handler, settings.worker_interval_seconds)
    if settings.slack_app_token:
        from nightagents.notifications.slack_socket import SlackSocketListener

        listener = SlackSocketListener(settings)
        await asyncio.gather(scheduler.run(), listener.run())
    else:
        await scheduler.run()


def main() -> None:
    asyncio.run(serve())
