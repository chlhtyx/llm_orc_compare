"""add task_records.document_type column

Revision ID: 0009_task_document_type
Revises: 0008_callback_payload
Create Date: 2026-08-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009_task_document_type"
down_revision: Union[str, None] = "0008_callback_payload"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """金额统计任务的单据类型("1"=发票 | "2"=对帐单,字符串枚举)。

    nullable=True:历史任务与 compare/raw 任务无此细分,保持 NULL;
    外部 amountStat 提交在应用层已默认归一为 "1"(发票)/"2"(对帐单)。

    顺带清理:个别环境曾被一个未发布的实验构建加上过 task_records.doc_type
    (integer)孤儿列,正式代码从未使用;存在则删除,保证 autogenerate/
    alembic check 不误报 diff。干净库无此列,跳过。
    """
    op.add_column(
        "task_records",
        sa.Column("document_type", sa.String(length=16), nullable=True),
    )
    inspector = sa.inspect(op.get_bind())
    columns = {col["name"] for col in inspector.get_columns("task_records")}
    if "doc_type" in columns:
        op.drop_column("task_records", "doc_type")


def downgrade() -> None:
    op.drop_column("task_records", "document_type")
