"""① 文档解析层。"""
from __future__ import annotations

from .pdf import count_pages, extract_text_blocks, get_page_metas, render_pages, slice_pdf
from .word import estimate_page_count, parse_word
from .word_render import DocxRenderError, attach_docx_layout, render_docx_to_pdf

__all__ = [
    "parse_word",
    "estimate_page_count",
    "get_page_metas",
    "render_pages",
    "extract_text_blocks",
    "count_pages",
    "slice_pdf",
    "DocxRenderError",
    "attach_docx_layout",
    "render_docx_to_pdf",
]
