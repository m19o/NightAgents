import json
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NIGHTAGENTS_", env_file=".env", extra="ignore")

    environment: str = "development"
    database_url: str = "sqlite:///./nightagents.db"
    handoff_signing_secret: str = Field(default="development-handoff-secret-change-me", min_length=32)
    approval_signing_secret: str = Field(default="development-approval-secret-change-me", min_length=32)
    elastic_webhook_secret: str = Field(default="development-elastic-webhook-secret-change-me", min_length=32)
    execution_grant_secret: str = Field(default="development-execution-secret-change-me", min_length=32)
    elastic_url: str = "https://127.0.0.1:9200"
    elastic_api_key: str = ""
    elastic_verify_tls: bool = True
    elastic_source_max_age_seconds: int = Field(default=900, ge=30, le=86400)
    mythic_url: str = "https://127.0.0.1:7443"
    mythic_api_key: str = ""
    mythic_verify_tls: bool = True
    infrastructure_mode: str = "database"
    infrastructure_provider: str = ""
    lab_victim_container: str = ""
    lab_hostname: str = "redirector-pass.demo"
    lab_redirector_map_json: str = "{}"
    lab_data_stream: str = "logs-redirector-demo"
    cloudflare_api_token: str = ""
    cloudflare_redirector_map_json: str = "{}"  # redirector_id -> {zone_id, dns_record_id, target}
    cloudflare_route_map_json: str = "{}"  # logical_route_id -> {zone_id, dns_record_id}
    cloudflare_sandbox: bool = False  # offline teaching sandbox instead of api.cloudflare.com
    cloudflare_window_minutes: int = Field(default=15, ge=1, le=1440)
    mcp_identity_header: str = "X-NightAgents-Identity"
    mcp_tokens_json: str = "{}"
    mcp_endpoints_json: str = "{}"
    safety_mcp_endpoints_json: str = "{}"
    research_mcp_endpoints_json: str = "{}"
    mcp_transport: str = "streamable-http"
    mcp_host: str = "127.0.0.1"
    mcp_port: int = Field(default=8100, ge=1, le=65535)
    workflow_max_tool_calls: int = Field(default=20, ge=1, le=100)
    workflow_max_iterations: int = Field(default=4, ge=1, le=20)
    workflow_max_context_tokens: int = Field(default=12000, ge=1000, le=200000)
    workflow_deadline_seconds: int = Field(default=45, ge=5, le=3600)
    model_context_max_raw_items: int = Field(default=10, ge=0, le=1000)
    model_api_url: str = ""
    model_api_key: str = ""
    model_name: str = "local"
    model_request_timeout_seconds: float = Field(default=120.0, ge=5.0, le=1800.0)
    model_roles_json: str = "{}"
    operator_tokens_json: str = "{}"
    outbox_webhook_url: str = ""
    outbox_webhook_secret: str = Field(default="development-outbox-secret-change-me", min_length=32)
    slack_webhook_url: str = ""
    slack_api_token: str = ""
    slack_channel: str = ""
    slack_signing_secret: str = ""
    slack_app_token: str = ""  # app-level token (xapp-...) for Socket Mode
    slack_channels_json: str = "{}"
    worker_interval_seconds: float = Field(default=5.0, ge=0.5, le=3600)
    detection_interval_seconds: float = Field(default=0.0, ge=0.0, le=86400.0)
    detection_source: str = "logs-nginx.access-*"
    auto_investigate: bool = False
    agentic_hunt: bool = False  # the agent hunts telemetry itself, not a detection engine
    third_party_monitor_interval_seconds: float = Field(default=0.0, ge=0.0, le=86400.0)
    third_party_feeds_json: str = "{}"
    third_party_feeds_verify_tls: bool = True
    api_url: str = "http://127.0.0.1:8000"
    api_host: str = "127.0.0.1"
    operator_token: str = ""
    operator_id: str = "development-operator"
    handoff_ttl_minutes: int = Field(default=15, ge=1, le=1440)

    def deployment_readiness(self) -> dict[str, list[str] | bool]:
        issues: list[str] = []
        if self.environment == "production":
            secrets = {
                "handoff signing secret": self.handoff_signing_secret,
                "approval signing secret": self.approval_signing_secret,
                "Elastic webhook secret": self.elastic_webhook_secret,
                "execution grant secret": self.execution_grant_secret,
                "outbox webhook secret": self.outbox_webhook_secret,
            }
            for name, value in secrets.items():
                if "development" in value or "change-me" in value or "replace-with" in value:
                    issues.append(f"{name} still uses a development value")
            if self.operator_tokens_json == "{}":
                issues.append("operator authentication is not configured")
            if self.mcp_tokens_json == "{}":
                issues.append("MCP service identities are not configured")
            endpoint_maps = {
                "agent MCP endpoints": self.mcp_endpoints_json,
                "Safety-Kernel MCP endpoints": self.safety_mcp_endpoints_json,
                "Research Agent MCP endpoints": self.research_mcp_endpoints_json,
            }
            for name, value in endpoint_maps.items():
                try:
                    endpoints = json.loads(value or "{}")
                except json.JSONDecodeError:
                    issues.append(f"{name} are invalid JSON")
                    continue
                if not endpoints:
                    issues.append(f"{name} are not configured")
                elif any(not str(item.get("url", "")).startswith("https://") for item in endpoints.values()):
                    issues.append(f"{name} include a non-HTTPS URL")
            if not self.elastic_api_key:
                issues.append("Elastic API key is not configured")
            if not self.mythic_api_key:
                issues.append("Mythic API key is not configured")
            if not self.outbox_webhook_url.startswith("https://"):
                issues.append("signed HTTPS outbox delivery is not configured")
            if not self.elastic_verify_tls or not self.mythic_verify_tls:
                issues.append("TLS verification is disabled for an external integration")
        if self.infrastructure_mode not in {"database", "provider", "lab", "cloudflare"}:
            issues.append("infrastructure mode is not persistent")
        return {"ready": not issues, "issues": issues}

    def lab_redirector_map(self) -> dict[str, str]:
        return json.loads(self.lab_redirector_map_json or "{}")

    def cloudflare_redirector_map(self) -> dict[str, dict]:
        return json.loads(self.cloudflare_redirector_map_json or "{}")

    def cloudflare_route_map(self) -> dict[str, dict]:
        return json.loads(self.cloudflare_route_map_json or "{}")


@lru_cache
def get_settings() -> Settings:
    return Settings()
