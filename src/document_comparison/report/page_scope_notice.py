"""报告中供应商正文范围与排除页说明。"""
from __future__ import annotations

from ..models import TamperReport


def page_scope_notice(report: TamperReport) -> str | None:
    record = report.truncation
    if record is None:
        return None
    kept = record.truncated_pdf_page_count
    excluded = "、".join(str(page) for page in record.excluded_page_numbers)
    if record.truncation_reason == "auto_trailing_drawings":
        return f"自动排除尾部图纸第{excluded}页，保留前{kept}页正文参与比对。"
    if record.truncation_reason == "manual_target_body_end_page":
        return f"按调用方指定保留前{kept}页正文；第{excluded}页未参与比对。"
    source = "外部显式传入" if record.doc_page_count_source == "explicit" else "原件页数估算"
    return f"按{source}保留前{kept}页；超出部分未参与比对。"


__all__ = ["page_scope_notice"]
