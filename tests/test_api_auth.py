import json

import pytest
from fastapi import HTTPException

from nightagents.api.auth import require_operator
from nightagents.config import Settings


def test_development_operator_is_explicitly_local_only() -> None:
    assert require_operator(settings=Settings(environment="development")) == "development-operator"


def test_production_requires_configured_bearer_token() -> None:
    settings = Settings(environment="production", operator_tokens_json=json.dumps({"token": "alice"}))
    with pytest.raises(HTTPException):
        require_operator(settings, "Bearer wrong")
    assert require_operator(settings, "Bearer token") == "alice"
