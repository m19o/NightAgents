class LocalStructuredModel:
    model_name = "local-structured"

    async def invoke(self, role: str, context: dict) -> dict:
        if role == "skill_selector":
            eligible = context.get("eligible_skills", [])
            if not eligible:
                raise ValueError("no eligible skills")
            return {
                "skill_id": eligible[0]["skill_id"],
                "reason": "selected from the workflow node allowlist",
            }
        if role == "evidence_verifier":
            source_health = context.get("source_health", {}) or {}
            unhealthy = [
                source for source, state in source_health.items() if state.get("status") != "healthy"
            ]
            unknowns = list(unhealthy)
            callback_inventory = context.get("callback_inventory")
            if callback_inventory is not None:
                callback_status = callback_inventory.get("status")
                if callback_status in {"unavailable", "partial", None}:
                    unknowns.append("callback inventory is incomplete")
            callback_verification = context.get("callback_verification")
            if callback_verification is not None:
                if callback_verification.get("status") in {"unavailable", "partial", None}:
                    unknowns.append("callback verification is incomplete")
                callbacks = callback_verification.get("callbacks", [])
                expected_ids = set(context.get("callback_ids", []))
                verified_ids = {cb.get("callback_id") for cb in callbacks if isinstance(cb, dict)}
                if expected_ids and not expected_ids.issubset(verified_ids):
                    unknowns.append("not all expected callbacks verified on new route")
            route_health = context.get("route_health")
            if route_health is not None and not route_health.get("healthy"):
                unknowns.append("replacement route health is not confirmed")
            return {
                "supported": not unknowns,
                "unknowns": unknowns,
                "negative_evidence_status": "verified" if not unhealthy else "invalid",
            }
        if role == "reasoner":
            evidence = context.get("elastic_evidence", {}) or {}
            events = context.get("events", []) or evidence.get("events", [])
            severity = context.get("incident", {}).get("severity", "medium")
            phase = self._phase(events)
            confidence = {"critical": 0.9, "high": 0.8, "medium": 0.65, "low": 0.5}.get(severity, 0.5)
            return {
                "phase": phase,
                "confidence": confidence,
                "hypotheses": [
                    {"name": "blue-team investigation", "confidence": confidence},
                    {"name": "benign automated scanning", "confidence": round(1 - confidence, 2)},
                ],
                "prediction": {
                    "event": self._next_phase(phase),
                    "probability": max(confidence - 0.1, 0.1),
                    "horizon_seconds": 43200,
                    "confirmation_query_id": "blue-chain-profiling-v1",
                },
                "recommended_skill": (
                    "plan-callback-migration"
                    if phase in {"correlation", "takedown"}
                    else "cool-down-redirector"
                ),
            }
        if role == "predictor":
            reasoning = context.get("reasoning", {})
            prediction = reasoning.get("prediction", {})
            return {
                "event": prediction.get("event", "profiling"),
                "probability": float(prediction.get("probability", 0.5)),
                "horizon_seconds": int(prediction.get("horizon_seconds", 43200)),
                "confirmation_query_id": prediction.get("confirmation_query_id", "blue-chain-profiling-v1"),
            }
        if role == "critic":
            assessment = context.get("assessment") or context.get("reasoning", {})
            unsupported = []
            evidence = context.get("evidence_refs") or (context.get("elastic_evidence") or {}).get("events")
            if not evidence:
                unsupported.append("assessment has no evidence references")
            if assessment.get("confidence", 0) > 0.9:
                unsupported.append("confidence exceeds local evidence policy")
            return {
                "unsupported_claims": unsupported,
                "alternative_explanations": ["benign scanner", "sensor degradation"],
                "fundamentally_supported": not unsupported,
            }
        if role == "router_planner":
            health = context.get("replacement_health", {}) or {}
            plan = context.get("plan", {}) or {}
            allowed = bool(health.get("healthy")) and bool(plan.get("plan_id"))
            return {
                "supported": allowed,
                "invariant_failures": [] if allowed else ["replacement health or plan is unverified"],
                "plan_id": plan.get("plan_id"),
            }
        if role == "researcher":
            document = context.get("document", {}) or {}
            text = str(document.get("content_untrusted", ""))
            title = next((line[:200] for line in text.splitlines() if line.strip()), "Knowledge candidate")
            risk_flags = document.get("risk_flags", [])
            return {
                "candidate_type": (
                    "DETECTION_QUERY"
                    if any(term in text.lower() for term in ("detect", "query", "kql", "sigma"))
                    else "KNOWLEDGE_CLAIM"
                ),
                "title": title,
                "content": {
                    "normalized_excerpt": text[:4000],
                    "risk_flags": risk_flags,
                    "untrusted_source": True,
                },
                "citations": [
                    f"{document.get('source_uri')}#sha256={document.get('content_hash')}"
                ],
                "confidence": 0.4 if risk_flags else 0.7,
            }
        if role == "research_critic":
            conflicts = context.get("conflicts", []) or []
            if isinstance(conflicts, dict):
                conflicts = conflicts.get("items", [])
            references = [
                str(item.get("source_reference", item.get("memory_id", "unknown")))
                for item in conflicts
                if isinstance(item, dict)
            ]
            return {
                "possible_conflicts": references,
                "requires_human_review": bool(references),
            }
        if role == "adjudicator":
            steps = context.get("steps", [])
            votes = [bool(step.get("supported")) for step in steps if isinstance(step, dict)]
            if not votes:
                return {"resolution": "escalate_to_operator", "confidence": 0.0}
            supported = sum(1 for vote in votes if vote)
            if supported == len(votes):
                return {"resolution": "supported", "confidence": 1.0}
            if supported == 0:
                return {"resolution": "unsupported", "confidence": 1.0}
            return {"resolution": "escalate_to_operator", "confidence": 0.0}
        raise ValueError(f"unsupported local model role: {role}")

    @staticmethod
    def _phase(events) -> str:
        if isinstance(events, dict):
            # Summarized evidence context (see orchestration.summarize): the
            # preserved keyword hits plus the raw sample carry the same
            # signals as the full event list.
            text = " ".join(
                [str(hit).lower() for hit in events.get("keyword_hits", [])]
                + [str(item).lower() for item in events.get("sample", [])]
            )
        else:
            text = " ".join(str(item).lower() for item in events)
        if any(term in text for term in ("suspend", "takedown", "abuse notice", "dns disabled")):
            return "takedown"
        if any(term in text for term in ("certificate transparency", "passive dns", "correlation")):
            return "correlation"
        if any(term in text for term in ("fingerprint", "jarm", "ja4", "profil")):
            return "profiling"
        return "discovery"

    @staticmethod
    def _next_phase(phase: str) -> str:
        return {
            "discovery": "profiling",
            "profiling": "correlation",
            "correlation": "takedown",
            "takedown": "service disruption",
        }[phase]
