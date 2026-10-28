"""Add durable agent run deadline and cancellation state."""

import sqlalchemy as sa
from alembic import op

revision = "0003_agent_run_control"
down_revision = "0002_agentic_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {item["name"] for item in inspector.get_columns("agent_runs")}
    columns = {
        "deadline_at": sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        "cancelled_at": sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        "wait_reason": sa.Column("wait_reason", sa.Text(), nullable=True),
    }
    with op.batch_alter_table("agent_runs") as batch:
        for name, column in columns.items():
            if name not in existing:
                batch.add_column(column)


def downgrade() -> None:
    pass
