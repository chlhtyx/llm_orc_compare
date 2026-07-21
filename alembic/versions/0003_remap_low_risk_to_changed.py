"""remap legacy overall_risk='low' to 'changed' (risk-off mode)

Revision ID: 0003_remap_low_risk_to_changed
Revises: 0002_llm_config_table
Create Date: 2026-07-21
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_remap_low_risk_to_changed"
down_revision: Union[str, None] = "0002_llm_config_table"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """一次性数据迁移:把历史 overall_risk='low' 改为 'changed'。

    背景:risk-off 模式下 overall_risk 不再产生 "low"(镜像 change_status)。
    旧的 low 历史记录属于「有确认差异但未做风险分级」的语义,统一重映射为
    'changed',与新的推导口径一致。只处理 compare 任务(raw/statement
    从不写 overall_risk,值为 NULL,不受影响)。

    注意:'low' 值本身不删除——risk-on 模式(enable_risk_assessment=True)
    仍可能产生 low,此处只清理历史风险关闭任务的遗留值。
    """
    op.execute(
        "UPDATE task_records SET overall_risk = 'changed' "
        "WHERE overall_risk = 'low' AND kind = 'compare'"
    )


def downgrade() -> None:
    """反向迁移:把本次迁移产生的 'changed' 改回 'low'。

    不完美:迁移后新产生的 'changed'(risk-off 默认模式的正常输出)也会被
    回滚成 'low'。仅在需要回到旧版本代码时使用,生产环境慎用。
    """
    op.execute(
        "UPDATE task_records SET overall_risk = 'low' "
        "WHERE overall_risk = 'changed' AND kind = 'compare'"
    )
