from importlib.metadata import entry_points

from sqlalchemy.orm import Session

from nightagents.adapters.database_infrastructure import DatabaseInfrastructure
from nightagents.adapters.lab_infrastructure import LabInfrastructureProvider
from nightagents.config import Settings
from nightagents.ports.infrastructure import InfrastructurePort


def build_infrastructure(db: Session, settings: Settings) -> InfrastructurePort:
    if settings.infrastructure_mode == "database":
        return DatabaseInfrastructure(db)
    if settings.infrastructure_mode == "lab":
        return LabInfrastructureProvider(
            victim_container=settings.lab_victim_container,
            hostname=settings.lab_hostname,
            redirector_map=settings.lab_redirector_map(),
            elastic_url=settings.elastic_url,
            data_stream=settings.lab_data_stream,
        )
    if settings.infrastructure_mode == "cloudflare":
        from nightagents.adapters.cloudflare import CloudflareInfrastructure

        return CloudflareInfrastructure(
            token=settings.cloudflare_api_token,
            redirector_map=settings.cloudflare_redirector_map(),
            route_map=settings.cloudflare_route_map(),
            sandbox=settings.cloudflare_sandbox,
            window_minutes=settings.cloudflare_window_minutes,
        )
    if settings.infrastructure_mode != "provider":
        raise ValueError(f"unsupported infrastructure mode: {settings.infrastructure_mode}")
    if not settings.infrastructure_provider:
        raise ValueError("provider mode requires NIGHTAGENTS_INFRASTRUCTURE_PROVIDER")
    candidates = {
        item.name: item
        for item in entry_points(group="nightagents.infrastructure")
    }
    selected = candidates.get(settings.infrastructure_provider)
    if selected is None:
        raise ValueError(f"infrastructure provider is not installed: {settings.infrastructure_provider}")
    factory = selected.load()
    adapter = factory(db=db, settings=settings)
    return adapter
