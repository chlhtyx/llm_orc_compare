"""篡改判定与风险分级(§5.5、§8.3)。"""
from __future__ import annotations

from ..models import DiffStatus, KeyElement, RiskLevel

_RISK_ORDER: dict[RiskLevel, int] = {"none": 0, "low": 1, "medium": 2, "high": 3}


def max_risk(levels: list[RiskLevel]) -> RiskLevel:
    if not levels:
        return "none"
    return max(levels, key=lambda lv: _RISK_ORDER[lv])


def overall_risk_from_diffs(high_or_modify_present: bool, levels: list[RiskLevel]) -> str:
    """汇总为 overall_risk:无任何 diff → clean,否则取最高风险。"""
    if not levels:
        return "clean"
    worst = max_risk(levels)
    return "clean" if worst == "none" else worst


def classify_diff(
    *,
    word_text: str,
    pdf_text: str,
    similarity: float,
    key_elements: list[KeyElement],
    sim_identical: float,
    sim_modified: float,
    table_change_reason: str = "",
) -> tuple[DiffStatus, RiskLevel, list[str]]:
    """对一对配对条款做篡改判定。

    返回 (status, risk_level, reasons)。
    - added/deleted 由调用方在未配对时单独处理。
    """
    reasons: list[str] = []

    # 归一化后字符一致 → 无差异
    if word_text == pdf_text and not table_change_reason:
        return "identical", "none", []

    changed_kinds = [e.kind for e in key_elements if e.changed]

    # 高风险要素变化 → 必报
    if changed_kinds:
        return "modified", "high", [f"高风险要素变更:{', '.join(changed_kinds)}"]

    # 表格结构来自确定性的行列/单元格比对，不能被整条语义高相似度覆盖。
    if table_change_reason:
        return "modified", "medium", [table_change_reason]

    # sim_identical 仅为旧调用方兼容参数。零容忍策略下，语义相似度无权
    # 把已经确认存在的字符差异抹成 identical。
    _ = sim_identical

    # 相似度显著下降 → 实质修改
    if similarity < sim_modified:
        return "modified", "medium", [f"语义相似度显著下降({similarity:.2f})"]

    # 中间区间:仍是确认字符变化；相似度仅用于严重度排序。
    return "modified", "low", [f"已确认字符差异,相似度 {similarity:.2f}"]
