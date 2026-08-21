"""⑥ 报告生成层。"""
from __future__ import annotations

from .builder import build_report
from .html_report import render_html_report
from .pdf_report import render_pdf_report

__all__ = ["build_report", "render_html_report", "render_pdf_report"]
