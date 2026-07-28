"""add callback_payload column

Revision ID: 0008_callback_payload
Revises: 0007_external_api_calls
Create Date: 2026-07-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0008_callback_payload"
down_revision: Union[str, None] = "0007_external_api_calls"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """首次回调交付的业务 payload,供「重新推送」端点 100% 还原原始内容。

    nullable=True:功能上线前的历史任务无此字段,重新推送时回退为最小 envelope。
    """
    op.add_column(
        "task_records",
        sa.Column("callback_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("task_records", "callback_payload")
