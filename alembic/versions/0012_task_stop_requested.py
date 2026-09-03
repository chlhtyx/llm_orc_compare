"""add task stop request flag

Revision ID: 0012_task_stop_requested
Revises: 0011_task_queue
Create Date: 2026-09-03
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0012_task_stop_requested"
down_revision: Union[str, None] = "0011_task_queue"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "task_records",
        sa.Column(
            "stop_requested", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )


def downgrade() -> None:
    op.drop_column("task_records", "stop_requested")
