"""Reconcile durable agentic runtime and security state."""

import sqlalchemy as sa
from alembic import op

from nightagents.db import models  # noqa: F401
from nightagents.db.base import Base

revision = "0002_agentic_runtime"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)
    inspector = sa.inspect(bind)
    _add_columns(
        inspector,
        "knowledge_documents",
        {"original_text": sa.Column("original_text", sa.Text(), nullable=False, server_default="")},
    )
    _add_columns(
        inspector,
        "outbox_events",
        {
            "claimed_at": sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
            "claimed_by": sa.Column("claimed_by", sa.String(length=255), nullable=True),
        },
    )
    inspector = sa.inspect(bind)
    index_names = {item["name"] for item in inspector.get_indexes("outbox_events")}
    if "ix_outbox_events_claimed_at" not in index_names:
        op.create_index("ix_outbox_events_claimed_at", "outbox_events", ["claimed_at"])


def downgrade() -> None:
    pass


def _add_columns(inspector, table: str, columns: dict[str, sa.Column]) -> None:
    existing = {item["name"] for item in inspector.get_columns(table)}
    with op.batch_alter_table(table) as batch:
        for name, column in columns.items():
            if name not in existing:
                batch.add_column(column)
