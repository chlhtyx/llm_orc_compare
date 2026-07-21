"""add llm_config single-row table

Revision ID: 0002_llm_config_table
Revises: 0001_initial
Create Date: 2026-07-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002_llm_config_table"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 单行表(id 固定为 1),整份 LLM/OCR 配置作为一个 JSONB blob,镜像原
    # llm_config.json 语义。字段白名单由 config._LLM_CONFIG_FIELDS 把控,
    # schema 不必枚举。
    op.create_table(
        "llm_config",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("llm_config")
