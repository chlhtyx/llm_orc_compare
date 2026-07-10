"""① 文档解析层。"""
from __future__ import annotations

from .pdf import extract_text_blocks, get_page_metas, render_pages
from .word import parse_word

__all__ = ["parse_word", "get_page_metas", "render_pages", "extract_text_blocks"]
