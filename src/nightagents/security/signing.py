import hashlib
import hmac
import json


def canonical_json(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def content_hash(value: dict) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def sign_hash(secret: str, digest: str) -> str:
    return hmac.new(secret.encode(), digest.encode(), hashlib.sha256).hexdigest()


def verify_hash_signature(secret: str, digest: str, signature: str) -> bool:
    return hmac.compare_digest(sign_hash(secret, digest), signature)

