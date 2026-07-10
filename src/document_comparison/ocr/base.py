"""OCR 引擎接口约定。"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

from ..models import Block, PageMeta

ProgressCb = Callable[[str, float], None]


@runtime_checkable
class OCREngine(Protocol):
    def recognize(
        self,
        pdf_path: Path,
        page_metas: list[PageMeta],
        *,
        on_progress: ProgressCb | None = None,
   ) -> list[list[Block]]:
       ...
