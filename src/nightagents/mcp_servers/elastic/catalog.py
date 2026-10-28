from dataclasses import dataclass


@dataclass(frozen=True)
class QueryDefinition:
    query_id: str
    language: str
    maximum_lookback_seconds: int
    maximum_results: int
    description: str
    fixed_filters: tuple[dict, ...] = ()


QUERY_CATALOG = {
    "incident-evidence-v1": QueryDefinition(
        "incident-evidence-v1", "kql", 3600, 200, "Engagement-scoped evidence surrounding an incident"
    ),
    "callback-degradation-v1": QueryDefinition(
        "callback-degradation-v1", "esql", 86400, 100, "Aggregate authorized service traffic degradation"
    ),
    "blue-chain-profiling-v1": QueryDefinition(
        "blue-chain-profiling-v1", "eql", 3600, 100, "Prebuilt-rule sequence result for defensive review"
    ),
    "beacon-fingerprint-v1": QueryDefinition(
        "beacon-fingerprint-v1",
        "kql",
        3600,
        200,
        "Raw edge requests for pull-based beacon-fingerprint detection",
    ),
    "edge-errors-v1": QueryDefinition(
        "edge-errors-v1",
        "kql",
        3600,
        200,
        "HTTP client/server error observations at the registered edge",
        ({"range": {"http.response.status_code": {"gte": 400}}},),
    ),
    "tls-failures-v1": QueryDefinition(
        "tls-failures-v1",
        "kql",
        86400,
        200,
        "TLS negotiation or certificate failures",
        ({"term": {"event.category": "network"}}, {"term": {"tls.established": False}}),
    ),
    "dns-control-changes-v1": QueryDefinition(
        "dns-control-changes-v1",
        "kql",
        86400,
        200,
        "Audited DNS or route control-plane changes",
        ({"terms": {"event.action": ["dns_record_changed", "route_changed", "certificate_changed"]}},),
    ),
    "provider-abuse-signals-v1": QueryDefinition(
        "provider-abuse-signals-v1",
        "kql",
        604800,
        100,
        "Provider abuse, suspension, or takedown-status observations",
        ({"terms": {"event.action": ["abuse_reported", "resource_suspended", "takedown_notice"]}},),
    ),
    "source-health-events-v1": QueryDefinition(
        "source-health-events-v1",
        "kql",
        86400,
        200,
        "Telemetry source health and data-gap observations",
        ({"terms": {"event.action": ["agent_unhealthy", "data_stream_stale", "ingest_failure"]}},),
    ),
}
