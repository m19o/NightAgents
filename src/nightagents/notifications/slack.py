"""Slack operator notifications.

Delivers approval requests and execution outcomes to Slack through either:

- an incoming webhook (`NIGHTAGENTS_SLACK_WEBHOOK_URL`), or
- the Slack Web API (`NIGHTAGENTS_SLACK_API_TOKEN` + `NIGHTAGENTS_SLACK_CHANNEL`)
  via ``chat.postMessage``.

Notifications are fire-and-forget: a Slack outage must never break a workflow,
so failures are swallowed and recorded as operational metrics. Approval itself
stays where it belongs - the hash-bound API/CLI path; Slack is delivery, not
authorization.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from nightagents.operational.metrics import OperationalMetrics

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

SLACK_API_URL = "https://slack.com/api/chat.postMessage"


class SlackNotifier:
    def __init__(
        self,
        db: Session,
        webhook_url: str = "",
        api_token: str = "",
        channel: str = "",
        channel_map: dict[str, str] | None = None,
        timeout_seconds: float = 10.0,
        interactive: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.db = db
        self.webhook_url = webhook_url
        self.api_token = api_token
        self.channel = channel
        self.channel_map = channel_map or {}
        self.timeout_seconds = timeout_seconds
        self.interactive = interactive
        self._transport = transport

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url) or bool(self.api_token and (self.channel or self.channel_map))

    def _channel_for(self, engagement_id: str | None) -> str:
        """Per-engagement channel routing: attendees with their own engagement get
        their own Slack channel; everything else falls back to the default."""
        if engagement_id and engagement_id in self.channel_map:
            return self.channel_map[engagement_id]
        return self.channel

    async def notify(
        self,
        text: str,
        fields: dict[str, Any] | None = None,
        engagement_id: str | None = None,
    ) -> bool:
        """Post a message; return success. Never raises into the workflow."""
        if not self.enabled:
            return False
        blocks = None
        if fields:
            blocks = [
                {"type": "section", "text": {"type": "mrkdwn", "text": text}},
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*{key}:*\n{value}"} for key, value in fields.items()
                    ],
                },
            ]
        try:
            ok = await self._post(text, blocks, self._channel_for(engagement_id))
        except Exception:  # noqa: BLE001 - notification must never break a workflow
            ok = False
        OperationalMetrics(self.db).record(
            "slack.notification", 1.0 if ok else 0.0, {"outcome": "sent" if ok else "failed"}
        )
        return ok

    async def _post(self, text: str, blocks: list[dict] | None, channel: str = "") -> bool:
        channel = channel or self.channel
        async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self._transport) as client:
            if self.api_token and channel:
                payload: dict[str, Any] = {"channel": channel, "text": text}
                if blocks:
                    payload["blocks"] = blocks
                response = await client.post(
                    SLACK_API_URL,
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_token}"},
                )
                return response.status_code == 200 and response.json().get("ok") is True
            payload = {"text": text}
            if blocks:
                payload["blocks"] = blocks
            response = await client.post(self.webhook_url, json=payload)
            return response.status_code == 200 and response.text.strip() == "ok"

    async def handoff_pending_approval(self, result: dict, engagement_id: str | None = None) -> bool:
        prediction = result.get("prediction") or {}
        recommendation = result.get("recommendation") or {}
        evidence = result.get("evidence") or []
        sources = sorted({str(item.get("source", "?")) for item in evidence})
        horizon_seconds = prediction.get("horizon_seconds") or 0
        handoff_id = result.get("handoff_id")
        fields = {
            "incident": result.get("incident_id", "-"),
            "disposition": result.get("disposition"),
            "prediction": f"{prediction.get('event')} p={prediction.get('probability')}",
            "phase": result.get("phase") or prediction.get("phase", "-"),
            "confidence": result.get("confidence", "-"),
            "recommended action": (
                f"{recommendation.get('skill')} on `{recommendation.get('redirector_id')}`"
                if recommendation
                else "-"
            ),
            "evidence": f"{len(evidence)} refs ({', '.join(sources)})" if evidence else "none",
            "confirm within": f"{round(horizon_seconds / 3600, 1)}h" if horizon_seconds else "-",
        }
        if not self.interactive:
            fields["approve"] = f"nightagents handoff-approve --handoff {handoff_id} --operator-id <you>"
        text = f":rotating_light: NightAgents handoff `{handoff_id}` awaits approval"
        if self.interactive:
            return await self._notify_blocks(text, fields, self._approval_actions(handoff_id), engagement_id)
        return await self.notify(text, fields, engagement_id)

    @staticmethod
    def _approval_actions(handoff_id: str) -> list[dict]:
        return [
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "style": "primary",
                        "action_id": "nightagents_approve",
                        "value": handoff_id,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Reject"},
                        "style": "danger",
                        "action_id": "nightagents_reject",
                        "value": handoff_id,
                    },
                ],
            }
        ]

    async def _notify_blocks(
        self, text: str, fields: dict, extra_blocks: list[dict], engagement_id: str | None = None
    ) -> bool:
        if not self.enabled:
            return False
        blocks: list[dict] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*{key}:*\n{value}"} for key, value in fields.items()
                ],
            },
            *extra_blocks,
        ]
        try:
            ok = await self._post(text, blocks, self._channel_for(engagement_id))
        except Exception:  # noqa: BLE001 - notification must never break a workflow
            ok = False
        OperationalMetrics(self.db).record(
            "slack.notification", 1.0 if ok else 0.0, {"outcome": "sent" if ok else "failed"}
        )
        return ok

    async def execution_finished(self, handoff_id: str, outcome: dict, engagement_id: str | None = None) -> bool:
        state = outcome.get("state")
        icon = ":white_check_mark:" if state == "completed" else ":warning:"
        return await self.notify(
            f"{icon} NightAgents execution for handoff `{handoff_id}` finished: *{state}*",
            {
                "state": state,
                "note": (
                    "continuity unproven -> rolled back safely"
                    if state == "rolled_back"
                    else "continuity verified"
                ),
            },
            engagement_id,
        )
