import json
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from nightagents.config import Settings, get_settings


def require_operator(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    current = settings
    tokens = json.loads(current.operator_tokens_json or "{}")
    if not tokens and current.environment == "development":
        return "development-operator"
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="operator bearer token required")
    operator = tokens.get(authorization.removeprefix("Bearer "))
    if not operator:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid operator token")
    return str(operator)
