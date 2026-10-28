from nightagents.config import Settings


def test_development_database_mode_is_ready() -> None:
    report = Settings().deployment_readiness()
    assert report["ready"] is True


def test_production_defaults_fail_readiness() -> None:
    report = Settings(environment="production").deployment_readiness()
    assert report["ready"] is False
    assert "operator authentication is not configured" in report["issues"]


def test_non_persistent_infrastructure_fails_readiness() -> None:
    report = Settings(infrastructure_mode="in-memory").deployment_readiness()
    assert report["ready"] is False
