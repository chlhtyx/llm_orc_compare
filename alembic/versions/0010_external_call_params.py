"""add request_params to external_api_calls

Revision ID: 0010_external_call_params
Revises: 0009_task_document_type
Create Date: 2026-08-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0010_external_call_params"
down_revision: Union[str, None] = "0009_task_document_type"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """外部接口审计记录补充请求参数快照(JSONB)。

    端点入口把解析后的请求参数(文件只记文件名)写入 request.state.audit_params,
    审计中间件落库到此处。nullable=True:401/422 等端点体未执行的场景无参数,
    历史记录同样为空。
    """
    op.add_column(
        "external_api_calls",
        sa.Column("request_params", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("external_api_calls", "request_params")
