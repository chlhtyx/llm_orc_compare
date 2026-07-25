"""识别质量门禁：低质量读取结果不得产生确定性高风险结论。"""
from __future__ import annotations

from ..compare.risk import max_risk
from ..models import PageRecognitionDiagnostic, TamperReport

_QUALITY_REASON = "识别质量不足，当前差异仅供人工复核"


def apply_recognition_gate(
    report: TamperReport,
    diagnostics: list[PageRecognitionDiagnostic],
    *,
    enable_risk_assessment: bool = False,
) -> TamperReport:
    report.recognition_diagnostics = diagnostics
    location_gaps = [
        item for item in diagnostics if item.location_status != "complete"
    ]
    if any(item.location_status == "missing" for item in location_gaps):
        report.location_status = "missing"
    elif location_gaps:
        report.location_status = "partial"
    else:
        report.location_status = "complete"
    if location_gaps:
        report.summary["location_quality"] = report.location_status
        report.summary["unlocated_pages"] = [
            item.page_index + 1 for item in location_gaps
        ]

    unreliable = [item for item in diagnostics if not item.reliable]
    if not unreliable:
        report.recognition_status = "reliable"
        return report

    report.recognition_status = "needs_review"
    report.summary["recognition_quality"] = "needs_review"
    report.summary["unreliable_pages"] = [item.page_index + 1 for item in unreliable]

    unreliable_pages = {item.page_index for item in unreliable}

    # 识别可信度只作用于关联页面的差异；严重度保持不变。
    for diff in [*report.diffs, *report.unmatched_clauses]:
        diff_pages = {region.page_index for region in diff.page_regions}
        # 没有 target bbox 时无法证明差异来自可靠页；存在坏页就必须复核。
        affected = bool(diff_pages & unreliable_pages) or not diff_pages
        if affected:
            diff.verdict = "needs_review"
            diff.confidence = "low"
            if _QUALITY_REASON not in diff.risk_reasons:
                diff.risk_reasons.append(_QUALITY_REASON)

    all_diffs = [*report.diffs, *report.unmatched_clauses]
    confirmed = [diff for diff in all_diffs if diff.verdict == "changed"]
    review = [diff for diff in all_diffs if diff.verdict == "needs_review"]
    if confirmed:
        report.change_status = "changed"
        if enable_risk_assessment:
            # 风险开启:按 diff.risk_level 取最高;全为 none 时占位 low。
            levels = [diff.risk_level for diff in all_diffs if diff.risk_level != "none"]
            report.overall_risk = max_risk(levels) if levels else "low"
        else:
            # 风险关闭:overall_risk 镜像 change_status,不出现 low。
            report.overall_risk = "changed"
    elif review or unreliable:
        # 即使未观察到差异，坏页也意味着无法证明整份回收件 clean。
        report.change_status = "needs_review"
        report.overall_risk = "needs_review"
    else:
        report.change_status = "clean"
        report.overall_risk = "clean"
    report.summary["confirmed_changes"] = len(confirmed)
    report.summary["needs_review"] = len(review)
    return report

