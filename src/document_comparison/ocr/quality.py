"""识别质量门禁：低质量读取结果不得产生确定性高风险结论。"""
from __future__ import annotations

from ..models import PageRecognitionDiagnostic, TamperReport, TextDiffReport

_QUALITY_REASON = "识别质量不足，当前差异仅供人工复核"


def apply_recognition_gate(
    report: TamperReport,
    diagnostics: list[PageRecognitionDiagnostic],
) -> TamperReport:
    report.recognition_diagnostics = diagnostics
    unreliable = [item for item in diagnostics if not item.reliable]
    if not unreliable:
        report.recognition_status = "reliable"
        return report

    report.recognition_status = "needs_review"
    report.overall_risk = "needs_review"
    report.summary["recognition_quality"] = "needs_review"
    report.summary["unreliable_pages"] = [item.page_index + 1 for item in unreliable]

    # 保留差异供人工定位，但撤销自动化的高/中风险断言。
    for diff in [*report.diffs, *report.unmatched_clauses]:
        if diff.risk_level in {"high", "medium"}:
            diff.risk_level = "low"
        if _QUALITY_REASON not in diff.risk_reasons:
            diff.risk_reasons.append(_QUALITY_REASON)
    return report


def attach_raw_recognition_diagnostics(
    report: TextDiffReport,
    diagnostics: list[PageRecognitionDiagnostic],
) -> TextDiffReport:
    report.recognition_diagnostics = diagnostics
    unreliable = [item for item in diagnostics if not item.reliable]
    if unreliable:
        report.recognition_status = "needs_review"
        report.stats["recognition_quality"] = "needs_review"
        report.stats["unreliable_pages"] = [
            item.page_index + 1 for item in unreliable
        ]
    return report
