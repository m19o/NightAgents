# hunt-redirector-telemetry

The NightWatcher agent's hunting graph: the agent itself monitors redirector
telemetry instead of relying on a deterministic detection engine.

Flow: source health gate -> query catalog discovery -> model plans up to four
catalog-allowlisted hunts (grounded in `playbook.md`, the distilled 29-pattern
field knowledge) -> the hunts execute read-only -> the model correlates results
into evidence-backed findings.

Guardrails are deterministic: unhealthy sources skip the hunt (absence of
evidence is never a negative outcome), only catalog queries may run, model
outputs must satisfy `hunt-plan-v1` / `hunt-correlator-v1`, and findings
without elastic evidence ids are discarded by the platform before any incident
is opened.
