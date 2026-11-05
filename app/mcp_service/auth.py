"""Подпись доверенного _meta, недоступного модели и постороннему клиенту."""

import hashlib
import hmac
import json
import time
import uuid

from app.agent_store import ActionError


def sign(secret: str, owner: int, action: uuid.UUID, *, now=None) -> dict:
    claims = {
        "owner": owner,
        "action": str(action),
        "expires": int(time.time() if now is None else now) + 60,
    }
    payload = json.dumps(claims, sort_keys=True, separators=(",", ":"))
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return {"study_assistant": {"claims": claims, "signature": signature}}


def verify(secret: str, meta, *, now=None):
    try:
        item = meta["study_assistant"]
        claims = item["claims"]
        if not secret or set(claims) != {"owner", "action", "expires"}:
            raise ValueError
        expected = hmac.new(
            secret.encode(),
            json.dumps(claims, sort_keys=True, separators=(",", ":")).encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, item["signature"]):
            raise ValueError
        current = int(time.time() if now is None else now)
        if type(claims["owner"]) is not int or not current < claims["expires"] <= current + 65:
            raise ValueError
        return claims["owner"], uuid.UUID(claims["action"])
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ActionError("trusted_context_required") from None
