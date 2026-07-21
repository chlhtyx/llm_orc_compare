"""add task_llm_calls table

Revision ID: 0004_task_llm_calls
Revises: 0003_remap_low_risk_to_changed
Create Date: 2026-07-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004_task_llm_calls"
down_revision: Union[str, None] = "0003_remap_low_risk_to_changed"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """对话型 LLM 调用的输入/输出记录表(一对多挂 task_records)。

    每次 HTTP 调用(含重试中间态)各一行,kind 与 observability._COLLECTED_KINDS 对齐。
    payload 中的图片 base64 已在收集阶段脱敏为 sha256 摘要,response 截断 64KB。
    """
    op.create_table(
        "task_llm_calls",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "task_id",
            sa.String(length=32),
            sa.ForeignKey("task_records.task_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("elapsed_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(length=512), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_task_llm_calls_task_id", "task_llm_calls", ["task_id"])
    op.create_index("ix_task_llm_calls_kind", "task_llm_calls", ["kind"])
    op.create_index("ix_task_llm_calls_created_at", "task_llm_calls", ["created_at"])
    op.create_index("ix_task_llm_calls_task_id_id", "task_llm_calls", ["task_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_task_llm_calls_task_id_id", table_name="task_llm_calls")
    op.drop_index("ix_task_llm_calls_created_at", table_name="task_llm_calls")
    op.drop_index("ix_task_llm_calls_kind", table_name="task_llm_calls")
    op.drop_index("ix_task_llm_calls_task_id", table_name="task_llm_calls")
    op.drop_table("task_llm_calls")
