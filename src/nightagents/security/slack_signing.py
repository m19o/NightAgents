"""Slack interaction request verification.

Slack signs every interactivity payload with the app's signing secret:
``X-Slack-Signature = "v0=" + HMAC-SHA256(secret, "v0:timestamp:body")``.
Deterministic verification - same discipline as the platform's webhook
nonces and signed handoffs.
"""

import hashlib
import hmac
import time


def verify_slack_signature(
    signing_secret: str,
    timestamp: str,
    body: bytes,
    signature: str,
    max_age_seconds: int = 300,
) -> bool:
    if not signing_secret or not timestamp or not signature:
        return False
    try:
        age = abs(time.time() - float(timestamp))
    except ValueError:
        return False
    if age > max_age_seconds:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
