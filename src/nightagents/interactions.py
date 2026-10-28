"""Shared Slack block_actions handling.

Used by both inbound paths: the HTTP interactivity endpoint
(`POST /api/v2/slack/interactions`, signature-verified) and the Socket Mode
listener (authenticated by the Slack app-level token over the websocket).
One handler, one audit trail, identical operator experience.
"""

from __future__ import annotations

import httpx
from sqlalchemy.orm import Session

from nightagents.approval import ApprovalService
from nightagents.config import Settings


async def handle_interaction_payload(db: Session, settings: Settings, payload: dict) -> str:
    """Apply a Slack interaction payload (approve/reject) and return the
    operator-facing summary text. Never raises for business-rule failures -
    they are reported back to the operator as warning text."""
    if payload.get("type") != "block_actions":
        return "ignored"
    user = payload.get("user") or {}
    operator_id = f"slack:{user.get('username') or user.get('id') or 'unknown'}"
    service = ApprovalService(db, settings)
    outcomes = []
    for action in payload.get("actions", []):
        handoff_id = action.get("value", "")
        try:
            if action.get("action_id") == "nightagents_approve":
                service.approve(handoff_id, operator_id)
                outcomes.append(f":white_check_mark: handoff `{handoff_id}` approved by {operator_id}")
            elif action.get("action_id") == "nightagents_reject":
                service.reject(handoff_id, operator_id)
                outcomes.append(f":no_entry: handoff `{handoff_id}` rejected by {operator_id}")
        except ValueError as exc:
            outcomes.append(f":warning: handoff `{handoff_id}`: {exc}")
    text = "\n".join(outcomes) or "no action"
    response_url = payload.get("response_url")
    if response_url:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(response_url, json={"replace_original": True, "text": text})
        except Exception:  # noqa: BLE001 - message update is best-effort
            pass
    return text
