"""Create the initial NightAgents control-plane schema."""

from alembic import op

from nightagents.db import models  # noqa: F401
from nightagents.db.base import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
