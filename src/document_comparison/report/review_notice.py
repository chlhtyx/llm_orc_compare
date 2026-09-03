"""面向业务人员的人工核对说明。

OCR/印章恢复模块会保留细粒度技术诊断，报告不能把这些原文直接抛给业务人员。
这里仅根据已保存的诊断生成通俗说明，不新增推断，也不尝试恢复被遮挡的文字。
"""
from __future__ import annotations

import re

from ..models import PageRecognitionDiagnostic, TamperReport


def review_notice_messages(report: TamperReport) -> list[str]:
    """返回报告中应展示的人工核对事项，每页至多一条通俗说明。"""
    messages = [
        _diagnostic_message(item)
        for item in report.recognition_diagnostics
        if not item.reliable
    ]
    if report.summary.get("llm_parse_failed"):
        messages.append(
            "系统未能生成可供确认的差异明细，不能据此认定合同没有变化；"
            "请人工对照原始合同和回收件核对。"
        )
    if not messages and report.recognition_status == "needs_review":
        messages.append(
            "部分内容无法由系统自动确认。请人工对照原始合同和回收件核对；"
            "完成核对前，本报告不能作为“未发现内容变化”的依据。"
        )
    return list(dict.fromkeys(messages))


def review_notice_intro(report: TamperReport) -> str | None:
    """人工核对区的业务提示；无待复核事项时不输出。"""
    if not review_notice_messages(report):
        return None
    return (
        "以下内容无法由系统自动确认。请以合同原件和回收件为准进行核对；"
        "在完成核对前，请不要将本报告作为“未发现内容变化”的依据。"
    )


def empty_diff_message(report: TamperReport) -> str:
    """差异明细为空时的文案，避免待复核报告被误读为无变化。"""
    if review_notice_messages(report):
        return "未识别到可确认的内容变化；请先完成“请人工核对”中的事项。"
    return "未发现内容变化"


def _diagnostic_message(item: PageRecognitionDiagnostic) -> str:
    reasons = " ".join(item.reasons)
    page_label = _page_label(item)
    if "检测到印章" in reasons and any(
        marker in reasons
        for marker in ("不一致", "失败", "无法可靠恢复", "缺少完整坐标")
    ):
        return (
            f"{page_label}检测到印章遮挡。系统已对印章覆盖区域再次识别，"
            "但两次识别结果无法相互确认；请以原件为准，重点核对被遮挡的文字、金额、日期和编号。"
        )
    if "内容为空或字符过少" in reasons or "未返回任何文本" in reasons:
        return f"{page_label}可读取文字过少，系统无法可靠比对该页；请结合原件核对。"
    if "重复内容" in reasons:
        return f"{page_label}识别出的文字出现重复，可能影响比对结果；请结合原件核对。"
    if "表格" in reasons and any(marker in reasons for marker in ("缺少", "不一致", "字段缺失")):
        return f"{page_label}表格内容读取不完整，系统无法可靠判断表格是否变化；请结合原件核对。"
    if "输出上限" in reasons:
        return f"{page_label}识别结果可能不完整，页面后半部分未能可靠比对；请结合原件核对。"
    return f"{page_label}文字读取结果不够可靠，系统无法自动确认该页内容；请结合原件核对。"


def _page_label(item: PageRecognitionDiagnostic) -> str:
    """whole-document OCR 会把印章冲突页码写入 reason，优先展示真实页码。"""
    reasons = " ".join(item.reasons)
    match = re.search(r"页码:([0-9,，]+)", reasons)
    if match:
        pages = match.group(1).replace(",", "、").replace("，", "、")
        return f"第{pages}页："
    return f"第{item.page_index + 1}页："
