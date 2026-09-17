"""Durable admission and drain barriers for release switching."""
from alembic import op
import sqlalchemy as sa

revision = "0013_release_control"
down_revision = "0012_task_stop_requested"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "release_deployments",
        sa.Column("deployment_id", sa.String(100), primary_key=True),
        sa.Column("version", sa.String(100), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "release_activities",
        sa.Column("activity_id", sa.String(32), primary_key=True),
        sa.Column("deployment_id", sa.String(100),
                  sa.ForeignKey("release_deployments.deployment_id"), nullable=False),
        sa.Column("owner", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("detail", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_release_activities_deployment_id", "release_activities", ["deployment_id"])


def downgrade():
    op.drop_table("release_activities")
    op.drop_table("release_deployments")
