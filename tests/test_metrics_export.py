from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from nightagents.api.main import create_app
from nightagents.db.base import Base
from nightagents.db.session import build_engine, get_session
from nightagents.operational import OperationalMetrics


def test_metrics_endpoint_exports_recorded_metrics() -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = create_app()

    def session_override():
        with factory() as db:
            yield db

    app.dependency_overrides[get_session] = session_override

    with factory() as db:
        metrics = OperationalMetrics(db)
        metrics.record("mcp.failure", 1.0, {"server": "elastic"})
        metrics.record("mcp.failure", 1.0, {"server": "elastic"})
        metrics.record("mcp.failure", 1.0, {"server": "mythic"})
        metrics.record("circuit.open", 1.0)
        db.commit()

    with TestClient(app) as client:
        response = client.get("/metrics")
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
        body = response.text
        assert "# HELP nightagents_mcp_failure_total" in body
        assert "# TYPE nightagents_mcp_failure_total counter" in body
        assert 'nightagents_mcp_failure_total{server="elastic"} 2.0' in body
        assert 'nightagents_mcp_failure_total{server="mythic"} 1.0' in body
        assert "# TYPE nightagents_circuit_open_total counter" in body
        assert "nightagents_circuit_open_total 1.0" in body


def test_metrics_endpoint_empty_when_no_metrics() -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = create_app()

    def session_override():
        with factory() as db:
            yield db

    app.dependency_overrides[get_session] = session_override

    with TestClient(app) as client:
        response = client.get("/metrics")
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
        assert response.text == "\n"
