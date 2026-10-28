import json
import time
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from nightagents.api.main import create_app
from nightagents.config import Settings, get_settings
from nightagents.db.base import Base
from nightagents.db.session import build_engine, get_session
from nightagents.security.webhook import sign_webhook


def test_signed_incident_webhook_is_idempotent_and_nonce_cannot_replay() -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(elastic_webhook_secret="integration-elastic-webhook-secret-long")
    app = create_app()

    def session_override():
        with factory() as db:
            yield db

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as client:
        engagement = client.post("/api/v2/engagements", json={"name": "integration"}).json()
        body = json.dumps(
            {
                "schema_version": "1.0",
                "external_alert_id": "alert-1",
                "engagement_id": engagement["engagement_id"],
                "severity": "high",
                "rule_id": "edge-errors-v1",
                "summary": "synthetic integration alert",
                "evidence_refs": [],
                "source_health": {"logs-edge": {}},
                "occurred_at": datetime.now(UTC).isoformat(),
            },
            separators=(",", ":"),
        ).encode()
        timestamp = str(int(time.time()))
        nonce = "integration-nonce"
        signature = sign_webhook(settings.elastic_webhook_secret, timestamp, nonce, body)
        headers = {
            "Content-Type": "application/json",
            "X-NightAgents-Timestamp": timestamp,
            "X-NightAgents-Nonce": nonce,
            "X-NightAgents-Signature": signature,
        }
        first = client.post("/api/v2/webhooks/elastic/incidents", content=body, headers=headers)
        replay = client.post("/api/v2/webhooks/elastic/incidents", content=body, headers=headers)
        assert first.status_code == 202
        assert replay.status_code == 409


def test_operator_lifecycle_and_audit_verification() -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = create_app()

    def session_override():
        with factory() as db:
            yield db

    app.dependency_overrides[get_session] = session_override
    with TestClient(app) as client:
        engagement = client.post("/api/v2/engagements", json={"name": "operator-flow"}).json()
        registered = client.post(
            f"/api/v2/engagements/{engagement['engagement_id']}/redirectors",
            json={
                "redirector_id": "edge-1",
                "domain": "edge.example.test",
                "logical_route_id": "primary",
                "provider_resource_id": "provider-1",
                "mythic_profile": "http",
                "failover_group": "group-1",
            },
        )
        health = client.put(
            f"/api/v2/engagements/{engagement['engagement_id']}/redirectors/edge-1/health",
            json={"status": "healthy", "evidence": {"probe": "synthetic"}},
        )
        audit = client.get("/api/v2/audit/verify")
        summary = client.get("/api/v2/operations/summary")
        assert registered.status_code == 201
        assert health.json()["state"] == "active"
        assert audit.json()["valid"] is True
        assert summary.json()["audit"]["valid"] is True
