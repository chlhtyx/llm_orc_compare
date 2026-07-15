"""⑥ 报告生成层。"""
from __future__ import annotations

from .builder import build_report
from .docx_burn import build_docx_preview, burn_docx

__all__ = ["build_report", "burn_docx", "build_docx_preview"]
