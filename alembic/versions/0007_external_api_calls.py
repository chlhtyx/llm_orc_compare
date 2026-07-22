"""add external_api_calls table

Revision ID: 0007_external_api_calls
Revises: 0006_callback_status
Create Date: 2026-07-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_external_api_calls"
down_revision: Union[str, None] = "0006_callback_status"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """外部接口(/api/v1/external/*)入站 HTTP 请求审计记录表。

    每次 /api/v1/external/ 前缀请求(含 401 鉴权失败、422 校验失败)各一行。
    task_id 可空且**不加外键**——任务创建前的失败调用审计必须独立留存,
    任务删除时不级联清除。
    """
    op.create_table(
        "external_api_calls",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # 无外键:ondelete 不级联,任务删除后审计记录保留
        sa.Column("task_id", sa.String(length=32), nullable=True),
        sa.Column("endpoint", sa.String(length=64), nullable=False),
        sa.Column("method", sa.String(length=8), nullable=False),
        sa.Column("document_no", sa.String(length=255), nullable=True),
        sa.Column("client_ip", sa.String(length=64), nullable=True),
        sa.Column("api_key_sha256", sa.String(length=64), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("elapsed_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(length=512), nullable=True),
        sa.Column("request_id", sa.String(length=12), nullable=False),
        sa.Column("content_length", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_external_api_calls_task_id", "external_api_calls", ["task_id"])
    op.create_index("ix_external_api_calls_endpoint", "external_api_calls", ["endpoint"])
    op.create_index("ix_external_api_calls_document_no", "external_api_calls", ["document_no"])
    op.create_index("ix_external_api_calls_request_id", "external_api_calls", ["request_id"])
    op.create_index("ix_external_api_calls_created_at", "external_api_calls", ["created_at"])
    op.create_index(
        "ix_external_api_calls_endpoint_created",
        "external_api_calls",
        ["endpoint", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_external_api_calls_endpoint_created", table_name="external_api_calls")
    op.drop_index("ix_external_api_calls_created_at", table_name="external_api_calls")
    op.drop_index("ix_external_api_calls_request_id", table_name="external_api_calls")
    op.drop_index("ix_external_api_calls_document_no", table_name="external_api_calls")
    op.drop_index("ix_external_api_calls_endpoint", table_name="external_api_calls")
    op.drop_index("ix_external_api_calls_task_id", table_name="external_api_calls")
    op.drop_table("external_api_calls")
