import hashlib
import hmac
import time


def sign_webhook(secret: str, timestamp: str, nonce: str, body: bytes) -> str:
    message = timestamp.encode() + b"." + nonce.encode() + b"." + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_webhook(
    secret: str,
    timestamp: str,
    nonce: str,
    signature: str,
    body: bytes,
    max_age_seconds: int = 300,
) -> None:
    try:
        supplied_time = int(timestamp)
    except ValueError as exc:
        raise ValueError("invalid webhook timestamp") from exc
    if abs(int(time.time()) - supplied_time) > max_age_seconds:
        raise ValueError("expired webhook")
    expected = sign_webhook(secret, timestamp, nonce, body)
    if not hmac.compare_digest(expected, signature.removeprefix("sha256=")):
        raise ValueError("invalid webhook signature")

