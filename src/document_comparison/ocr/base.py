"""OCR 引擎接口约定。"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from ..models import Block, PageMeta


@runtime_checkable
class OCREngine(Protocol):
    """把 PDF 识别为每页的版面 Block 列表。

    Block.bbox 统一使用 PDF 点坐标(pt,72 DPI),与 pdf.js viewport 同坐标系,
    便于报告层归一化为 [0,1] 相对坐标(§5.6)。
    """

    def recognize(
        self, pdf_path: Path, page_metas: list[PageMeta]
    ) -> list[list[Block]]:
        ...
