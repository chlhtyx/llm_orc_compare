"""add external contract request metadata

Revision ID: 0005_external_contract_api
Revises: 0004_task_llm_calls
Create Date: 2026-07-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_external_contract_api"
down_revision: Union[str, None] = "0004_task_llm_calls"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "task_records",
        sa.Column("document_no", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "task_records",
        sa.Column(
            "external_request",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_index(
        "ix_task_records_document_no", "task_records", ["document_no"], unique=False
    )
    op.create_index(
        "ix_task_records_external_request",
        "task_records",
        ["external_request"],
        unique=False,
    )
    op.alter_column("task_records", "external_request", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_task_records_external_request", table_name="task_records")
    op.drop_index("ix_task_records_document_no", table_name="task_records")
    op.drop_column("task_records", "external_request")
    op.drop_column("task_records", "document_no")
