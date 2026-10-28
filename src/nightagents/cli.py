import argparse
import asyncio
import json
from datetime import UTC, datetime

import httpx

from nightagents.config import get_settings
from nightagents.evaluation import ReplayEvaluator


class OperatorClient:
    def __init__(
        self,
        base_url: str,
        token: str = "",
        transport: httpx.BaseTransport | None = None,
    ):
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.client = httpx.Client(
            base_url=base_url.rstrip("/"), headers=headers, timeout=30, transport=transport
        )

    def close(self) -> None:
        self.client.close()

    def request(self, method: str, path: str, payload: dict | None = None):
        response = self.client.request(method, path, json=payload)
        try:
            data = response.json()
        except ValueError:
            data = {"detail": response.text}
        if response.is_error:
            raise RuntimeError(f"NightAgents API {response.status_code}: {data.get('detail', data)}")
        return data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nightagents", description="NightAgents defensive operator CLI")
    parser.add_argument("--api-url")
    parser.add_argument("--token")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("readiness")
    commands.add_parser("audit-verify")
    commands.add_parser("ops-summary")
    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("--fixtures", default="evaluations/fixtures")
    evaluate.add_argument("--skills", default="skills")
    evaluate.add_argument("--max-failures", type=int)
    evaluate.add_argument("--min-passed", type=int)

    run_status = commands.add_parser("run-status")
    run_status.add_argument("run_id")
    run_cancel = commands.add_parser("run-cancel")
    run_cancel.add_argument("run_id")
    run_pause = commands.add_parser("run-pause")
    run_pause.add_argument("run_id")
    run_pause.add_argument("--reason", default="operator pause")
    run_resume = commands.add_parser("run-resume")
    run_resume.add_argument("run_id")

    engagement = commands.add_parser("engagement-create")
    engagement.add_argument("name")

    assets = commands.add_parser("assets")
    assets.add_argument("engagement_id")

    register = commands.add_parser("redirector-register")
    register.add_argument("engagement_id")
    register.add_argument("redirector_id")
    register.add_argument("domain")
    register.add_argument("logical_route_id")
    register.add_argument("provider_resource_id")
    register.add_argument("mythic_profile")
    register.add_argument("failover_group")

    health = commands.add_parser("redirector-health")
    health.add_argument("engagement_id")
    health.add_argument("redirector_id")
    health.add_argument("status", choices=["healthy", "unhealthy"])
    health.add_argument("--evidence-json", default="{}")

    reconcile = commands.add_parser("mythic-reconcile")
    reconcile.add_argument("engagement_id")

    source = commands.add_parser("knowledge-source-add")
    source.add_argument("source_id")
    source.add_argument("publisher")
    source.add_argument("source_uri")
    source.add_argument("source_type")
    source.add_argument("trust_tier")
    source.add_argument("allowed_host")
    source.add_argument("--license-policy", default="review")
    source.add_argument("--poll-interval-seconds", type=int, default=86400)

    commands.add_parser("knowledge-sources")
    documents = commands.add_parser("knowledge-documents")
    documents.add_argument("--status")
    refresh = commands.add_parser("knowledge-refresh")
    refresh.add_argument("source_id")
    approve_document = commands.add_parser("knowledge-approve")
    approve_document.add_argument("document_id")
    review_document = commands.add_parser("knowledge-review")
    review_document.add_argument("document_id")
    review_document.add_argument("decision", choices=["approve", "reject"])
    review_document.add_argument("--risk-acceptance", action="store_true")
    review_document.add_argument("--notes", default="")
    commands.add_parser("knowledge-candidates")
    review_candidate = commands.add_parser("knowledge-candidate-review")
    review_candidate.add_argument("candidate_id")
    review_candidate.add_argument("decision", choices=["approve", "reject"])
    review_candidate.add_argument("--notes", default="")

    investigate = commands.add_parser("investigate")
    investigate.add_argument("incident_id")

    approve = commands.add_parser("handoff-approve")
    approve.add_argument("handoff_id")
    approve.add_argument("--operator-id")

    execute = commands.add_parser("handoff-execute")
    execute.add_argument("handoff_id")
    execute.add_argument("logical_route_id")
    execute.add_argument("old_redirector_id")
    execute.add_argument("new_redirector_id")

    outcome = commands.add_parser("prediction-outcome")
    outcome.add_argument("handoff_id")
    outcome.add_argument("observed_event")
    outcome.add_argument("matched", choices=["true", "false"])
    outcome.add_argument("--notes", default="")

    calibration = commands.add_parser("calibration")
    calibration.add_argument("engagement_id")

    horizon_status = commands.add_parser("prediction-horizon-status")
    horizon_status.add_argument("prediction_id")
    verify_horizon = commands.add_parser("verify-horizon")
    verify_horizon.add_argument("prediction_id")
    review_list = commands.add_parser("operator-review-list")
    review_list.add_argument("--status")
    review_resolve = commands.add_parser("operator-review-resolve")
    review_resolve.add_argument("task_id")
    review_resolve.add_argument("--resolution", default="")
    return parser


def dispatch(client: OperatorClient, args: argparse.Namespace, operator_id: str) -> object:
    command = args.command
    if command == "readiness":
        return client.request("GET", "/health/ready")
    if command == "audit-verify":
        return client.request("GET", "/api/v2/audit/verify")
    if command == "ops-summary":
        return client.request("GET", "/api/v2/operations/summary")
    if command == "run-status":
        return client.request("GET", f"/api/v2/agent-runs/{args.run_id}")
    if command == "run-cancel":
        return client.request("POST", f"/api/v2/agent-runs/{args.run_id}/cancel")
    if command == "run-pause":
        return client.request("POST", f"/api/v2/agent-runs/{args.run_id}/pause?reason={args.reason}")
    if command == "run-resume":
        return client.request("POST", f"/api/v2/agent-runs/{args.run_id}/resume")
    if command == "engagement-create":
        return client.request("POST", "/api/v2/engagements", {"name": args.name})
    if command == "assets":
        return client.request("GET", f"/api/v2/engagements/{args.engagement_id}/assets")
    if command == "redirector-register":
        return client.request(
            "POST",
            f"/api/v2/engagements/{args.engagement_id}/redirectors",
            {
                "redirector_id": args.redirector_id,
                "domain": args.domain,
                "logical_route_id": args.logical_route_id,
                "provider_resource_id": args.provider_resource_id,
                "mythic_profile": args.mythic_profile,
                "failover_group": args.failover_group,
            },
        )
    if command == "redirector-health":
        return client.request(
            "PUT",
            f"/api/v2/engagements/{args.engagement_id}/redirectors/{args.redirector_id}/health",
            {"status": args.status, "evidence": json.loads(args.evidence_json)},
        )
    if command == "mythic-reconcile":
        return client.request("POST", f"/api/v2/engagements/{args.engagement_id}/reconcile/mythic")
    if command == "knowledge-source-add":
        return client.request(
            "POST",
            "/api/v2/knowledge/sources",
            {
                "source_id": args.source_id,
                "publisher": args.publisher,
                "source_uri": args.source_uri,
                "source_type": args.source_type,
                "trust_tier": args.trust_tier,
                "allowed_host": args.allowed_host,
                "license_policy": args.license_policy,
                "poll_interval_seconds": args.poll_interval_seconds,
                "enabled": True,
            },
        )
    if command == "knowledge-sources":
        return client.request("GET", "/api/v2/knowledge/sources")
    if command == "knowledge-documents":
        suffix = f"?status={args.status}" if args.status else ""
        return client.request("GET", f"/api/v2/knowledge/documents{suffix}")
    if command == "knowledge-refresh":
        return client.request("POST", f"/api/v2/knowledge/sources/{args.source_id}/refresh")
    if command == "knowledge-approve":
        return client.request("POST", f"/api/v2/knowledge/documents/{args.document_id}/approve")
    if command == "knowledge-review":
        return client.request(
            "POST",
            f"/api/v2/knowledge/documents/{args.document_id}/review",
            {
                "decision": args.decision,
                "risk_acceptance": args.risk_acceptance,
                "notes": args.notes,
            },
        )
    if command == "knowledge-candidates":
        return client.request("GET", "/api/v2/knowledge/candidates")
    if command == "knowledge-candidate-review":
        return client.request(
            "POST",
            f"/api/v2/knowledge/candidates/{args.candidate_id}/review",
            {"decision": args.decision, "notes": args.notes},
        )
    if command == "investigate":
        return client.request("POST", f"/api/v2/incidents/{args.incident_id}/investigate")
    if command == "handoff-approve":
        return client.request(
            "POST",
            f"/api/v2/handoffs/{args.handoff_id}/approve",
            {"operator_id": args.operator_id or operator_id},
        )
    if command == "handoff-execute":
        return client.request(
            "POST",
            f"/api/v2/handoffs/{args.handoff_id}/execute",
            {
                "logical_route_id": args.logical_route_id,
                "old_redirector_id": args.old_redirector_id,
                "new_redirector_id": args.new_redirector_id,
            },
        )
    if command == "prediction-outcome":
        return client.request(
            "POST",
            f"/api/v2/handoffs/{args.handoff_id}/outcome",
            {
                "observed_event": args.observed_event,
                "matched": args.matched == "true",
                "observed_at": datetime.now(UTC).isoformat(),
                "notes": args.notes,
            },
        )
    if command == "calibration":
        return client.request("GET", f"/api/v2/engagements/{args.engagement_id}/prediction-calibration")
    if command == "prediction-horizon-status":
        return client.request("GET", f"/api/v2/predictions/{args.prediction_id}/status")
    if command == "verify-horizon":
        return client.request("POST", f"/api/v2/predictions/{args.prediction_id}/verify-horizon")
    if command == "operator-review-list":
        suffix = f"?status={args.status}" if args.status else ""
        return client.request("GET", f"/api/v2/operator-review-tasks{suffix}")
    if command == "operator-review-resolve":
        return client.request(
            "POST",
            f"/api/v2/operator-review-tasks/{args.task_id}/resolve",
            {"resolution": args.resolution},
        )
    raise RuntimeError(f"unsupported command: {command}")


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "evaluate":
        from pathlib import Path

        threshold = {}
        if args.max_failures is not None:
            threshold["max_failures"] = args.max_failures
        if args.min_passed is not None:
            threshold["min_passed"] = args.min_passed
        result = asyncio.run(
            ReplayEvaluator(Path(args.skills)).run_directory(Path(args.fixtures), threshold=threshold or None)
        )
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        raise SystemExit(0 if result["passed"] else 1)
    settings = get_settings()
    client = OperatorClient(args.api_url or settings.api_url, args.token or settings.operator_token)
    try:
        result = dispatch(client, args, settings.operator_id)
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    finally:
        client.close()
