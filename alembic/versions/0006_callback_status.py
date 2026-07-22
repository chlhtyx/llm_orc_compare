"""add callback delivery status columns

Revision ID: 0006_callback_status
Revises: 0005_external_contract_api
Create Date: 2026-07-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_callback_status"
down_revision: Union[str, None] = "0005_external_contract_api"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "task_records",
        sa.Column("callback_url", sa.String(length=1024), nullable=True),
    )
    op.add_column(
        "task_records",
        sa.Column("callback_status", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "task_records",
        sa.Column("callback_http_status", sa.Integer(), nullable=True),
    )
    op.add_column(
        "task_records",
        sa.Column("callback_error", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "task_records",
        sa.Column("callback_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_task_records_callback_status",
        "task_records",
        ["callback_status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_task_records_callback_status", table_name="task_records")
    op.drop_column("task_records", "callback_at")
    op.drop_column("task_records", "callback_error")
    op.drop_column("task_records", "callback_http_status")
    op.drop_column("task_records", "callback_status")
    op.drop_column("task_records", "callback_url")
