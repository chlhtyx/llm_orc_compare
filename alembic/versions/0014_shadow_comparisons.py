"""Durable A/B quality comparisons and idempotent B receipts."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0014_shadow_comparisons"
down_revision = "0013_release_control"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "shadow_comparisons",
        sa.Column("comparison_id", sa.String(64), primary_key=True),
        sa.Column("role", sa.String(1), nullable=False),
        sa.Column("experiment", sa.String(100), nullable=False),
        sa.Column("a_task_id", sa.String(32), nullable=False),
        sa.Column("b_task_id", sa.String(32), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("review", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_shadow_role_experiment_created", "shadow_comparisons", ["role", "experiment", "created_at"])
    op.create_index("ix_shadow_role_experiment_task", "shadow_comparisons", ["role", "experiment", "a_task_id"])


def downgrade():
    op.drop_table("shadow_comparisons")
