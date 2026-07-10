"""Mock OCR:直接读 PDF 文本层。

适合开发与"文本 PDF"(非扫描件)。对扫描件(无文本层)返回空块——
此时须在 UI 设置页切换 ocr_backend=vllm 走多模态 LLM 识别。
"""
from __future__ import annotations

from pathlib import Path

from ..models import Block, PageMeta
from ..parsing.pdf import extract_text_blocks


class TextLayerOCR:
    def recognize(
        self,
        pdf_path: Path,
        page_metas: list[PageMeta],
        *,
        on_progress=None,
    ) -> list[list[Block]]:
        if on_progress:
            on_progress("ocr", 0.40)
        return extract_text_blocks(pdf_path)
