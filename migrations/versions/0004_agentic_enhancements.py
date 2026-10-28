"""Add prediction horizon, calibration, adjudication, evaluation, claims, and operational metrics schema."""

import sqlalchemy as sa
from alembic import op

revision = "0004_agentic_enhancements"
down_revision = "0003_agent_run_control"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns(table)}
    return column in cols


def upgrade() -> None:
    if not _has_table("operator_review_tasks"):
        op.create_table(
            "operator_review_tasks",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("engagement_id", sa.String(36), sa.ForeignKey("engagements.id"), index=True, nullable=True),
            sa.Column("target_type", sa.String(64), nullable=False),
            sa.Column("target_id", sa.String(36), index=True, nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("status", sa.String(32), default="open"),
            sa.Column("resolution", sa.Text(), default=""),
            sa.Column("resolved_by", sa.String(255), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), default=sa.func.now()),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        )

    if not _has_table("predictions"):
        op.create_table(
            "predictions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("handoff_id", sa.String(36), sa.ForeignKey("handoffs.id"), index=True, nullable=False),
            sa.Column("engagement_id", sa.String(36), sa.ForeignKey("engagements.id"), index=True, nullable=False),
            sa.Column("predicted_event", sa.String(1024), nullable=False),
            sa.Column("predicted_probability", sa.Float(), nullable=False),
            sa.Column("horizon_seconds", sa.Integer(), nullable=False),
            sa.Column("confirmation_query_id", sa.String(255), nullable=False),
            sa.Column("status", sa.String(32), default="pending"),
            sa.Column("outcome_id", sa.String(36), sa.ForeignKey("prediction_outcomes.id"), nullable=True),
            sa.Column("review_task_id", sa.String(36), sa.ForeignKey("operator_review_tasks.id"), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), default=sa.func.now()),
        )

    with op.batch_alter_table("prediction_outcomes") as batch:
        for col, col_type in [
            ("prediction_id", sa.String(36)),
            ("model_version", sa.String(255)),
            ("prompt_version", sa.String(255)),
            ("skill_version", sa.String(255)),
            ("dataset_version", sa.String(255)),
        ]:
            if not _has_column("prediction_outcomes", col):
                if col == "prediction_id":
                    batch.add_column(sa.Column(col, col_type, sa.ForeignKey("predictions.id"), index=True, nullable=True))
                else:
                    batch.add_column(sa.Column(col, col_type, default=""))

    with op.batch_alter_table("agent_steps") as batch:
        for col in ("model_id", "model_version", "prompt_version"):
            if not _has_column("agent_steps", col):
                batch.add_column(sa.Column(col, sa.String(255), nullable=True))

    if not _has_table("calibration_buckets"):
        op.create_table(
            "calibration_buckets",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("engagement_id", sa.String(36), sa.ForeignKey("engagements.id"), index=True, nullable=False),
            sa.Column("bucket_low", sa.Float(), nullable=False),
            sa.Column("bucket_high", sa.Float(), nullable=False),
            sa.Column("count", sa.Integer(), default=0),
            sa.Column("observed_frequency", sa.Float(), nullable=True),
            sa.Column("model_version", sa.String(255), default=""),
            sa.Column("prompt_version", sa.String(255), default=""),
            sa.Column("skill_version", sa.String(255), default=""),
            sa.Column("dataset_version", sa.String(255), default=""),
            sa.Column("updated_at", sa.DateTime(timezone=True), default=sa.func.now()),
            sa.UniqueConstraint("engagement_id", "bucket_low", "bucket_high", "model_version", "skill_version"),
        )

    if not _has_table("disagreements"):
        op.create_table(
            "disagreements",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("run_id", sa.String(36), sa.ForeignKey("agent_runs.id"), index=True, nullable=False),
            sa.Column("step_ids_json", sa.JSON(), default=list),
            sa.Column("roles_json", sa.JSON(), default=list),
            sa.Column("disagreement_type", sa.String(64), nullable=False),
            sa.Column("threshold", sa.Float(), nullable=False),
            sa.Column("status", sa.String(32), default="open"),
            sa.Column("adjudicator_role", sa.String(64), nullable=True),
            sa.Column("resolution", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), default=sa.func.now()),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        )

    if not _has_table("evaluation_runs"):
        op.create_table(
            "evaluation_runs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("evaluation_id", sa.String(255), index=True, nullable=False),
            sa.Column("dataset_version", sa.String(255), default=""),
            sa.Column("model_version", sa.String(255), default=""),
            sa.Column("prompt_version", sa.String(255), default=""),
            sa.Column("skill_version", sa.String(255), default=""),
            sa.Column("passed", sa.Boolean(), nullable=False),
            sa.Column("run_status", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), default=sa.func.now()),
        )

    if not _has_table("evaluation_assertions"):
        op.create_table(
            "evaluation_assertions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("run_id", sa.String(36), sa.ForeignKey("evaluation_runs.id"), index=True, nullable=False),
            sa.Column("path", sa.String(1024), nullable=False),
            sa.Column("expected_json", sa.JSON(), default=dict),
            sa.Column("actual_json", sa.JSON(), default=dict),
            sa.Column("passed", sa.Boolean(), nullable=False),
        )

    if not _has_table("knowledge_claims"):
        op.create_table(
            "knowledge_claims",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("candidate_id", sa.String(36), sa.ForeignKey("knowledge_candidates.id"), index=True, nullable=False),
            sa.Column("claim_text", sa.Text(), nullable=False),
            sa.Column("lexical_signature", sa.String(255), default=""),
            sa.Column("compared_memory_id", sa.String(36), sa.ForeignKey("memories.id"), nullable=True),
            sa.Column("relationship", sa.String(32), default="unknown"),
            sa.Column("semantic_diff", sa.Text(), default=""),
            sa.Column("status", sa.String(32), default="review"),
            sa.Column("created_at", sa.DateTime(timezone=True), default=sa.func.now()),
        )

    if not _has_table("operational_metrics"):
        op.create_table(
            "operational_metrics",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("name", sa.String(255), index=True, nullable=False),
            sa.Column("dimensions_json", sa.JSON(), default=dict),
            sa.Column("value", sa.Float(), nullable=False),
            sa.Column("recorded_at", sa.DateTime(timezone=True), default=sa.func.now()),
        )


def downgrade() -> None:
    for table in (
        "operational_metrics",
        "knowledge_claims",
        "evaluation_assertions",
        "evaluation_runs",
        "disagreements",
        "calibration_buckets",
    ):
        if _has_table(table):
            op.drop_table(table)
    with op.batch_alter_table("agent_steps") as batch:
        for col in ("prompt_version", "model_version", "model_id"):
            if _has_column("agent_steps", col):
                batch.drop_column(col)
    with op.batch_alter_table("prediction_outcomes") as batch:
        for col in ("dataset_version", "skill_version", "prompt_version", "model_version", "prediction_id"):
            if _has_column("prediction_outcomes", col):
                batch.drop_column(col)
    for table in ("predictions", "operator_review_tasks"):
        if _has_table(table):
            op.drop_table(table)
