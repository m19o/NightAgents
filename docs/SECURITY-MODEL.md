# NightAgents Security Model

The design assumption is that models are fallible, prompt-injectable, and occasionally wrong. Everything that can change the world is deterministic code with signatures and grants.

## What models may do

- Read evidence through allowlisted MCP tools (cataloged, bounded queries).
- Select skills from an allowlist; a selection outside the allowlist is rejected (`PermissionError`).
- Produce structured outputs validated against versioned JSON schemas; invalid output is retried with error feedback, then fails the step.
- Propose: dispositions, predictions, recommended actions, knowledge candidates, hunting queries (catalog ids only).

## What models may never do

- Authorize or execute an infrastructure mutation.
- Mint or widen an execution grant.
- Approve their own handoffs.
- Activate knowledge into governed memory.
- Treat internet research content as instructions.
- Declare success when callback continuity is unknown.

## Signatures and grants

| Artifact | Mechanism | Property |
| --- | --- | --- |
| Handoff | HMAC over payload hash | tamper-evident proposal |
| Approval | HMAC over the handoff hash, operator identity recorded | unforgeable decision, single-decision enforcement |
| Execution grant | scoped to one plan id + one tool + one engagement, expiring | least-privilege mutation window |
| Audit chain | hash-linked events with a global head | tamper-evident history, verifiable via API |
| Slack HTTP interactions | Slack request signature + timestamp window | fail-closed 401 |
| Slack Socket Mode | app-level token authenticated websocket | no inbound attack surface |

## Evidence discipline

- Absence of evidence is never a negative outcome: unhealthy evidence sources skip detection, hunting, and verification runs.
- Negative evidence is valid only when required sources are independently healthy at decision time.
- Findings without elastic evidence ids are discarded before any incident opens.
- Unknown callback continuity triggers rollback or an operator-review task, never a success report.

## Isolation

- Engagements are isolated: queries, callbacks, assets, and handoffs are engagement-scoped.
- Planning (nightwatcher) and mutation (safety-kernel) use separate MCP tokens; research uses a third.
- The API binds loopback by default; operator bearer tokens gate every management endpoint (`NIGHTAGENTS_OPERATOR_TOKENS_JSON`) outside development mode.

## Defensive scope

NightAgents is built for authorized infrastructure only. The project does not implement guardrail bypasses, stealth/evasion automation, uncontrolled C2 deployment, or victim execution. The lab runs entirely in local containers.
