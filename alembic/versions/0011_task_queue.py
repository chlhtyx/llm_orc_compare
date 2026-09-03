"""add durable task queue payload and worker lease

Revision ID: 0011_task_queue
Revises: 0010_external_call_params
Create Date: 2026-09-03
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0011_task_queue"
down_revision: Union[str, None] = "0010_external_call_params"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "task_records",
        sa.Column("queue_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "task_records", sa.Column("lease_owner", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "task_records",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_task_queue_pending_created",
        "task_records",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending' AND queue_payload IS NOT NULL"),
    )
    op.create_index("ix_task_records_lease_expires_at", "task_records", ["lease_expires_at"])


def downgrade() -> None:
    op.drop_index("ix_task_records_lease_expires_at", table_name="task_records")
    op.drop_index("ix_task_queue_pending_created", table_name="task_records")
    op.drop_column("task_records", "lease_expires_at")
    op.drop_column("task_records", "lease_owner")
    op.drop_column("task_records", "queue_payload")
