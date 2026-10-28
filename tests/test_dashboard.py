from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from nightagents.api.main import create_app
from nightagents.config import Settings, get_settings
from nightagents.db.base import Base
from nightagents.db.session import build_engine, get_session


def _client(settings: Settings | None = None) -> TestClient:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = create_app()

    def session_override():
        with factory() as db:
            yield db

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_settings] = lambda: settings or Settings()
    return TestClient(app)


def test_dashboard_page_serves_static_shell_without_auth() -> None:
    client = _client()
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "NightAgents" in response.text
    assert "/api/v2/dashboard/live" in response.text


def test_dashboard_live_returns_operations_view() -> None:
    client = _client()
    response = client.get("/api/v2/dashboard/live")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"summary", "incidents", "handoffs", "executions"}
    assert "audit" in payload["summary"]
    assert payload["incidents"] == []
    assert payload["handoffs"] == []


def test_dashboard_live_requires_operator_token_when_configured() -> None:
    settings = Settings(environment="production", operator_tokens_json='{"secret-token": "operator-1"}')
    client = _client(settings)
    assert client.get("/api/v2/dashboard/live").status_code == 401
    authed = client.get("/api/v2/dashboard/live", headers={"Authorization": "Bearer secret-token"})
    assert authed.status_code == 200
    # the static shell stays public - it carries no data
    assert client.get("/dashboard").status_code == 200
