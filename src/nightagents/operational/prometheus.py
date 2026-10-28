import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from nightagents.db.models import MetricRecord

PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

_NAME_PATTERN = re.compile(r"[^a-zA-Z0-9_]")


def _sanitize_name(name: str) -> str:
    sanitized = _NAME_PATTERN.sub("_", name)
    if sanitized and sanitized[0].isdigit():
        sanitized = f"_{sanitized}"
    return sanitized


def _escape_label_value(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_prometheus(db: Session) -> str:
    """Renders durable MetricRecord rows as a Prometheus text exposition.

    Each metric name becomes a `nightagents_<name>_total` counter whose value
    is the sum of recorded values, split into series by dimension label sets.
    """
    records = list(db.scalars(select(MetricRecord)))
    totals: dict[str, dict[tuple[tuple[str, str], ...], float]] = {}
    for record in records:
        labels = tuple(sorted((str(key), str(value)) for key, value in (record.dimensions_json or {}).items()))
        series = totals.setdefault(record.name, {})
        series[labels] = series.get(labels, 0.0) + record.value

    lines: list[str] = []
    for name in sorted(totals):
        series_name = f"nightagents_{_sanitize_name(name)}_total"
        lines.append(f"# HELP {series_name} Total recorded value for operational metric '{name}'.")
        lines.append(f"# TYPE {series_name} counter")
        for labels, total in sorted(totals[name].items()):
            if labels:
                rendered = ",".join(f'{_sanitize_name(key)}="{_escape_label_value(value)}"' for key, value in labels)
                lines.append(f"{series_name}{{{rendered}}} {total}")
            else:
                lines.append(f"{series_name} {total}")
    return "\n".join(lines) + "\n"
