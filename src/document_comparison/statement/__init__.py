"""对帐单金额统计子模块。

- amount_column:列定位 + 金额抽取 + 确定性求和(主逻辑,零 LLM)。
- llm_column_detect:仅在启发式列定位失败时让多模态 LLM 指认金额列(列索引+角色)。
  LLM 绝不参与数值识别或求和。

设计原则(贯彻项目 AGENTS.md「确定性优先,LLM 辅助」):
  金额抽取复用 compare.elements 的扫描;求和用 Decimal 精确累加;
  LLM 只能补充列定位,不能撤销确定性的求和结论。
"""
from .amount_column import (
    detect_amount_columns,
    extract_amounts_from_cell,
    is_total_row,
    summarize_table,
    merge_cross_page_tables,
)

__all__ = [
    "detect_amount_columns",
    "extract_amounts_from_cell",
    "is_total_row",
    "summarize_table",
    "merge_cross_page_tables",
]
