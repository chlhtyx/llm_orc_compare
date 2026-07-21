"""initial schema: task_records + task_events

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "task_records",
        sa.Column("task_id", sa.String(length=32), primary_key=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source_name", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("target_names", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("ocr_backend", sa.String(length=16), nullable=True),
        sa.Column("overall_risk", sa.String(length=16), nullable=True),
        sa.Column("change_status", sa.String(length=16), nullable=True),
        sa.Column("elapsed", sa.Float(), nullable=True),
        sa.Column("stage_timings", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("error", sa.String(length=2048), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("report_compare", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("report_raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("report_statement", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_index("ix_task_records_status", "task_records", ["status"])
    op.create_index("ix_task_records_kind", "task_records", ["kind"])
    op.create_index("ix_task_records_created_at", "task_records", ["created_at"])
    op.create_index("ix_task_records_overall_risk", "task_records", ["overall_risk"])
    op.create_index("ix_task_records_status_created", "task_records", ["status", "created_at"])
    op.create_index("ix_task_records_kind_created", "task_records", ["kind", "created_at"])

    op.create_table(
        "task_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "task_id",
            sa.String(length=32),
            sa.ForeignKey("task_records.task_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("progress", sa.Float(), nullable=False, server_default="0"),
        sa.Column("stage_timings", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("milestone", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_task_events_task_id", "task_events", ["task_id"])
    op.create_index("ix_task_events_stage", "task_events", ["stage"])
    op.create_index("ix_task_events_created_at", "task_events", ["created_at"])
    op.create_index("ix_task_events_task_id_id", "task_events", ["task_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_task_events_task_id_id", table_name="task_events")
    op.drop_index("ix_task_events_created_at", table_name="task_events")
    op.drop_index("ix_task_events_stage", table_name="task_events")
    op.drop_index("ix_task_events_task_id", table_name="task_events")
    op.drop_table("task_events")

    op.drop_index("ix_task_records_kind_created", table_name="task_records")
    op.drop_index("ix_task_records_status_created", table_name="task_records")
    op.drop_index("ix_task_records_overall_risk", table_name="task_records")
    op.drop_index("ix_task_records_created_at", table_name="task_records")
    op.drop_index("ix_task_records_kind", table_name="task_records")
    op.drop_index("ix_task_records_status", table_name="task_records")
    op.drop_table("task_records")
