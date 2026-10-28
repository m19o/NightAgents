"""Slack Socket Mode listener - inbound operator approvals without a public URL.

The platform keeps its HTTP interactivity endpoint, but laptops and range VMs
behind NAT cannot be reached by Slack's webhook delivery. Socket Mode flips
the direction: we dial out to Slack over an authenticated websocket (app-level
token, `connections:write` scope) and receive the same interaction envelopes.

Discipline preserved from the HTTP path: envelopes are acked within Slack's
3-second budget before any business logic runs, decisions go through the same
shared handler (same ApprovalService, same audit trail), and a handoff decided
once cannot be re-decided.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import websockets

from nightagents.config import Settings
from nightagents.db.session import SessionLocal
from nightagents.interactions import handle_interaction_payload

logger = logging.getLogger(__name__)

CONNECTIONS_OPEN_URL = "https://slack.com/api/apps.connections.open"


class SlackSocketListener:
    def __init__(self, settings: Settings):
        if not settings.slack_app_token:
            raise ValueError("slack_app_token is required for Socket Mode")
        self.settings = settings
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def _open_connection_url(self) -> str:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                CONNECTIONS_OPEN_URL,
                headers={"Authorization": f"Bearer {self.settings.slack_app_token}"},
            )
            response.raise_for_status()
            data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"apps.connections.open failed: {data.get('error', data)}")
        return str(data["url"])

    async def _handle_envelope(self, ws, envelope: dict) -> None:
        envelope_id = envelope.get("envelope_id")
        if envelope_id:
            # Slack requires the ack within 3 seconds - ack first, work after.
            await ws.send(json.dumps({"envelope_id": envelope_id}))
        if envelope.get("type") == "disconnect":
            # Slack asks us to reconnect (e.g. before a server restart).
            reason = envelope.get("reason", "unknown")
            raise _ReconnectRequested(f"slack requested disconnect: {reason}")
        if envelope.get("type") != "interactive":
            return
        payload = envelope.get("payload") or {}
        with SessionLocal() as db:
            text = await handle_interaction_payload(db, self.settings, payload)
        logger.info("slack socket interaction handled: %s", text)

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                url = await self._open_connection_url()
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    logger.info("slack socket mode connected")
                    backoff = 1.0
                    async for raw in ws:
                        if self._stop.is_set():
                            return
                        try:
                            envelope = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        try:
                            await self._handle_envelope(ws, envelope)
                        except _ReconnectRequested:
                            raise
                        except Exception:  # noqa: BLE001 - one bad envelope must not kill the link
                            logger.exception("failed to handle slack envelope")
            except _ReconnectRequested as exc:
                logger.info("slack socket reconnecting: %s", exc)
            except Exception:  # noqa: BLE001 - network failures are expected; keep trying
                if not self._stop.is_set():
                    logger.exception("slack socket connection failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except TimeoutError:
                pass
            backoff = min(backoff * 2, 60.0)


class _ReconnectRequested(Exception):
    pass
