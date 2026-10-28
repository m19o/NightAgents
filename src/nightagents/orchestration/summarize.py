"""Deterministic evidence pre-aggregation for model-phase context.

Model phases historically received up to 100 raw evidence events, which
dominates LLM token cost (the investigation manifest allows a 64000-token
context largely because of raw evidence events). This module replaces long
lists of event dicts with compact, deterministic summaries before the context
is handed to a model. It is a pure function: no I/O, no model calls. Persisted
run state, step outputs, audit and tool-call records always keep the raw data.
"""

from datetime import datetime
from typing import Any

# Dotted event fields tallied (top values) when present in raw evidence items.
COUNT_FIELDS = (
    "url.path",
    "user_agent.original",
    "source.ip",
    "event.action",
    "kibana.alert.rule.name",
)

# Keyword set mirrored from the deterministic local model's phase detection
# (nightagents.models.local.LocalStructuredModel._phase). Surfacing which of
# these appear in the raw items keeps keyword-driven phase detection working
# when the raw events are aggregated away.
KEYWORDS = (
    "suspend",
    "takedown",
    "abuse notice",
    "dns disabled",
    "certificate transparency",
    "passive dns",
    "correlation",
    "fingerprint",
    "jarm",
    "ja4",
    "profil",
)

SAMPLE_SIZE = 3
TOP_COUNT_LIMIT = 10


def summarize_context(context: dict, max_raw_items: int = 10) -> dict:
    """Return a copy of ``context`` with long lists of event dicts summarized.

    Any list of dicts longer than ``max_raw_items`` (including lists nested one
    level down, e.g. ``{"events": [...]}``) is replaced by a summary dict with
    ``total_count``, ``counts``, ``time_bounds``, ``interval_seconds``,
    ``keyword_hits`` and ``sample`` keys. Small lists and non-list values pass
    through unchanged. ``max_raw_items <= 0`` disables summarization.
    """
    if max_raw_items <= 0:
        return context
    return {key: _summarize_value(value, max_raw_items) for key, value in context.items()}


def summarize_items(items: list[dict]) -> dict:
    """Build the deterministic summary dict for a list of raw event dicts."""
    timestamps = [
        (raw, parsed)
        for item in items
        if (raw := item.get("@timestamp")) is not None and (parsed := _parse_timestamp(raw)) is not None
    ]
    deltas = [
        (timestamps[index + 1][1] - timestamps[index][1]).total_seconds() for index in range(len(timestamps) - 1)
    ]
    text = " ".join(str(item).lower() for item in items)
    return {
        "total_count": len(items),
        "counts": _field_counts(items),
        "time_bounds": (
            {
                "min": min(timestamps, key=lambda pair: pair[1])[0],
                "max": max(timestamps, key=lambda pair: pair[1])[0],
            }
            if timestamps
            else None
        ),
        "interval_seconds": (
            {
                "min": round(min(deltas), 3),
                "max": round(max(deltas), 3),
                "mean": round(sum(deltas) / len(deltas), 3),
            }
            if deltas
            else None
        ),
        "keyword_hits": [keyword for keyword in KEYWORDS if keyword in text],
        "sample": items[:SAMPLE_SIZE],
    }


def _summarize_value(value: Any, max_raw_items: int) -> Any:
    if isinstance(value, dict):
        return {key: _summarize_value(item, max_raw_items) for key, item in value.items()}
    if isinstance(value, list) and len(value) > max_raw_items and all(isinstance(item, dict) for item in value):
        return summarize_items(value)
    return value


def _field_counts(items: list[dict]) -> dict:
    counts: dict[str, dict[str, int]] = {}
    for field in COUNT_FIELDS:
        tally: dict[str, int] = {}
        for item in items:
            value = _path(item, field)
            if value is None:
                continue
            key = str(value)
            tally[key] = tally.get(key, 0) + 1
        if tally:
            top = sorted(tally.items(), key=lambda pair: (-pair[1], pair[0]))[:TOP_COUNT_LIMIT]
            counts[field] = dict(top)
    return counts


def _path(item: dict, dotted: str) -> Any:
    current: Any = item
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _parse_timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
