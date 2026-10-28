"""Add source reputation history table."""

import sqlalchemy as sa
from alembic import op

revision = "0005_source_reputation"
down_revision = "0004_agentic_enhancements"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("source_reputation"):
        op.create_table(
            "source_reputation",
            sa.Column("source_id", sa.String(255), primary_key=True),
            sa.Column("publisher", sa.String(255), nullable=False),
            sa.Column("success_count", sa.Integer(), default=0),
            sa.Column("failure_count", sa.Integer(), default=0),
            sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("trust_tier_history_json", sa.JSON(), default=list),
            sa.Column("current_trust_tier", sa.String(32), default="unreviewed"),
            sa.Column("updated_at", sa.DateTime(timezone=True), default=sa.func.now()),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("source_reputation"):
        op.drop_table("source_reputation")
