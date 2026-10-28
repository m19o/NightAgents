import json

from nightagents.config import Settings
from nightagents.models.local import LocalStructuredModel
from nightagents.models.openai_compatible import OpenAICompatibleModel
from nightagents.models.router import RoleModelRouter


def build_model(settings: Settings):
    default = (
        OpenAICompatibleModel(
            settings.model_api_url,
            settings.model_api_key,
            settings.model_name,
            request_timeout=settings.model_request_timeout_seconds,
        )
        if settings.model_api_url
        else LocalStructuredModel()
    )
    role_config = json.loads(settings.model_roles_json or "{}")
    if not role_config:
        return default
    role_models = {}
    for role, config in role_config.items():
        provider = config.get("provider", "local")
        if provider == "local":
            role_models[role] = LocalStructuredModel()
        elif provider == "openai-compatible":
            role_models[role] = OpenAICompatibleModel(
                config.get("api_url", settings.model_api_url),
                config.get("api_key", settings.model_api_key),
                config.get("model_name", settings.model_name),
                request_timeout=float(config.get("timeout", settings.model_request_timeout_seconds)),
            )
        else:
            raise ValueError(f"unsupported model provider for role {role}: {provider}")
    return RoleModelRouter(default, role_models)
