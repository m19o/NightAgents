import pytest
from sqlalchemy.orm import sessionmaker

from nightagents.adapters.database_infrastructure import DatabaseInfrastructure
from nightagents.adapters.infrastructure_factory import build_infrastructure
from nightagents.config import Settings
from nightagents.db.base import Base
from nightagents.db.session import build_engine


def test_infrastructure_factory_builds_persistent_database_adapter() -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        assert isinstance(build_infrastructure(db, Settings()), DatabaseInfrastructure)


def test_infrastructure_factory_rejects_uninstalled_provider() -> None:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        settings = Settings(infrastructure_mode="provider", infrastructure_provider="missing")
        with pytest.raises(ValueError, match="not installed"):
            build_infrastructure(db, settings)
