# Predict Blue-Team Chain

Version: 1
Agent: NightWatcher

## Purpose

Produce competing hypotheses, the most likely next observable event, a probability, a time horizon and confirmation query.

## Preconditions

- Correlated Elastic incident exists.
- Material negative claims have confirmed, refuted or unknown status.
- Current source-health snapshot is attached.

## Output

- Current phase.
- Competing hypotheses.
- Prediction with probability and horizon.
- Unknowns.
- Proportional action recommendation or abstention.

## Calibration

- Report confidence as a calibrated estimate: stay at or below 0.9. Confidence above 0.9 is
  reserved for overwhelming multi-source evidence and is rejected by the critic policy.
- Hypotheses must reference the supplied evidence (event counts, callback inventory, source
  health), not generic threat narratives.
- No invented attribution: never claim botnets, exfiltration, named actors, campaigns, or
  intent that is not directly stated in the supplied evidence. Describe only what is
  observable: beacon rhythm, URIs, user agent, callback inventory, source health.

## Critique scope

- Your job is to block **fabricated** assessments, not to demand proof. Uncertainty is not
  grounds for a false verdict - only fabrication or contradiction of evidence is. When in
  doubt, the verdict stays true and your doubts go in `unsupported_claims`.
- The assessment standard is **consistency, not proof**. Hypotheses are not required to prove
  malicious intent; they must only be consistent with the observable pattern. Do not set the
  verdict to false because intent is not "directly proven" - intent is never directly provable
  from telemetry, and that is expected.
- Example SUPPORTED claim: "pattern consistent with C2 beaconing through the redirector" when
  the telemetry shows exactly that rhythm. Example UNSUPPORTED claim: "the source is part of
  a larger botnet" (no such evidence exists).
- A hypothesis is supported when it is framed as an assessment and is consistent with the
  observable evidence (periodic requests, fixed user agent, small URI set, callback
  inventory).
- Flag only: fabricated specifics (victim counts, actors, campaigns, payloads, intent beyond
  the evidence), claims that contradict the evidence, or assessments with no evidence
  references at all.
- Set `fundamentally_supported` to false ONLY when the whole assessment collapses under those
  rules (fabricated or contradicted evidence). Hedged wording and minor nitpicks belong in
  `unsupported_claims` while the verdict stays true.

