from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nightagents.db.models import MetricRecord


class OperationalMetrics:
    """Records durable operational metrics for monitoring and alerting."""

    def __init__(self, db: Session):
        self.db = db

    def record(self, name: str, value: float, dimensions: dict | None = None) -> MetricRecord:
        record = MetricRecord(
            name=name,
            value=value,
            dimensions_json=dimensions or {},
        )
        self.db.add(record)
        self.db.flush()
        return record

    def latest(self, name: str, dimensions: dict | None = None) -> float | None:
        statement = select(MetricRecord).where(MetricRecord.name == name)
        if dimensions:
            for key, value in dimensions.items():
                statement = statement.where(
                    MetricRecord.dimensions_json.contains({key: value})
                )
        record = self.db.scalars(statement.order_by(MetricRecord.recorded_at.desc())).first()
        return record.value if record else None

    def summary(self, names: list[str] | None = None) -> dict:
        statement = select(MetricRecord.name, func.count(), func.avg(MetricRecord.value))
        if names:
            statement = statement.where(MetricRecord.name.in_(names))
        statement = statement.group_by(MetricRecord.name)
        rows = self.db.execute(statement).all()
        return {
            row[0]: {"count": int(row[1]), "average": round(float(row[2]), 4) if row[2] is not None else None}
            for row in rows
        }
