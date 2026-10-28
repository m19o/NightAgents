"""Add label_source to prediction outcomes for trust-separated weak labels."""

import sqlalchemy as sa
from alembic import op

revision = "0006_outcome_label_source"
down_revision = "0005_source_reputation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("prediction_outcomes")}
    if "label_source" not in columns:
        op.add_column(
            "prediction_outcomes",
            sa.Column("label_source", sa.String(32), server_default="operator", nullable=False),
        )
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("prediction_outcomes")}
    if "ix_prediction_outcomes_label_source" not in indexes:
        op.create_index("ix_prediction_outcomes_label_source", "prediction_outcomes", ["label_source"])


def downgrade() -> None:
    bind = op.get_bind()
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("prediction_outcomes")}
    if "ix_prediction_outcomes_label_source" in indexes:
        op.drop_index("ix_prediction_outcomes_label_source", table_name="prediction_outcomes")
    columns = {column["name"] for column in sa.inspect(bind).get_columns("prediction_outcomes")}
    if "label_source" in columns:
        op.drop_column("prediction_outcomes", "label_source")
