# Security Policy

## Scope

NightAgents is defensive tooling for authorized red team infrastructure: it watches your own redirectors, reasons over your own telemetry, and moves your own traffic with human approval. It is not an exploitation framework and must not be used against infrastructure you are not authorized to operate.

## Reporting a vulnerability

If you find a security issue - especially in the Safety Kernel, execution grants, handoff/approval signature verification, Slack interaction handling, or the MCP authorization layer - please report it privately:

- Open a private security advisory on the GitHub repository (Security -> Advisories -> "Report a vulnerability"), or
- Contact the maintainer via the email on their GitHub profile.

Do not open a public issue for an unpatched vulnerability.

You can expect an acknowledgement within 72 hours. Please include: affected version/commit, a description of the issue, reproduction steps or a proof of concept, and the impact you see (e.g. "approval can be forged", "execution grant scope can be widened").

## Supported versions

| Version | Supported |
| ------- | --------- |
| 1.0.x   | yes       |
| < 1.0   | no        |

## Security model highlights

- Models propose and reason; deterministic code owns authorization, signatures, approvals, grants, idempotency, and state transitions.
- Handoffs and approvals are HMAC-signed; a handoff decided once cannot be re-decided.
- Infrastructure mutations require an execution grant scoped to one plan and one tool, minted by the Safety Kernel after a valid signed approval.
- Slack interaction callbacks are verified against the Slack request signature (HTTP path) or the app-level token websocket (Socket Mode path).
- Unknown callback continuity triggers rollback or operator intervention, never success.
- Internet research content is untrusted data, never executable instructions, and never activates knowledge without human approval.

If you believe one of these properties does not hold, that is a security report - please send it.
