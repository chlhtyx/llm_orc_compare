"""可信 PDF 读取 module：原生读取优先，视觉 OCR 逐页降级。"""
from __future__ import annotations

from collections import Counter
import logging
from pathlib import Path
import re
import tempfile

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz  # type: ignore[no-redef]

from ..config import settings
from ..models import Block, PageMeta, PageRecognitionDiagnostic
from ..parsing.pdf import get_page_metas
from .base import OCREngine, ProgressCb
from .native import read_native_page

logger = logging.getLogger(__name__)


class TrustedPDFReader:
    """按页选择原生解析或视觉 OCR，并保留逐页质量诊断。"""

    def __init__(self, fallback: OCREngine) -> None:
        self.fallback = fallback
        self.last_diagnostics: list[PageRecognitionDiagnostic] = []

    def recognize(
        self,
        pdf_path: Path,
        page_metas: list[PageMeta],
        *,
        on_progress: ProgressCb | None = None,
    ) -> list[list[Block]]:
        pages: list[list[Block]] = [[] for _ in page_metas]
        diagnostics: list[PageRecognitionDiagnostic | None] = [None] * len(page_metas)
        fallback_indexes: list[int] = []

        with fitz.open(str(pdf_path)) as doc:
            total = max(1, len(doc))
            for page_index, page in enumerate(doc):
                native = read_native_page(page, page_index)
                if native.reliable:
                    pages[page_index] = native.blocks
                    diagnostics[page_index] = native.diagnostic
                else:
                    fallback_indexes.append(page_index)
                if on_progress:
                    on_progress("ocr", 0.10 + 0.15 * ((page_index + 1) / total))

        if fallback_indexes:
            fallback_pages = self._recognize_fallback_pages(
                pdf_path, fallback_indexes, on_progress=on_progress
            )
            truncated_fallback_pages = set(
                getattr(self.fallback, "last_truncated_pages", set())
            )
            for local_index, original_index in enumerate(fallback_indexes):
                remapped = [
                    block.model_copy(
                        update={
                            "page_index": original_index,
                            "block_id": f"p{original_index}-fallback-{block_index}",
                        }
                    )
                    for block_index, block in enumerate(fallback_pages[local_index])
                ]
                pages[original_index] = remapped
                diagnostic = assess_fallback_page(
                    remapped, original_index
                )
                if local_index in truncated_fallback_pages:
                    diagnostic = diagnostic.model_copy(
                        update={
                            "reliable": False,
                            "reasons": [
                                *diagnostic.reasons,
                                "OCR 模型输出达到输出上限，页面尾部可能缺失",
                            ],
                        }
                    )
                diagnostics[original_index] = diagnostic

        self.last_diagnostics = [
            item
            for item in diagnostics
            if item is not None
        ]
        logger.info(
            "trusted pdf read pages=%s native=%s fallback=%s unreliable=%s",
            len(page_metas),
            len(page_metas) - len(fallback_indexes),
            len(fallback_indexes),
            sum(not item.reliable for item in self.last_diagnostics),
        )
        return pages

    def _recognize_fallback_pages(
        self,
        pdf_path: Path,
        page_indexes: list[int],
        *,
        on_progress: ProgressCb | None,
    ) -> list[list[Block]]:
        # 保持 fallback adapter 的既有 interface：把需要 OCR 的页面组成临时 PDF，
        # adapter 只会渲染并发送这些页面，不会重复处理可靠的原生文本页。
        with tempfile.TemporaryDirectory(prefix="dc-ocr-") as temp_dir:
            subset_path = Path(temp_dir) / "fallback-pages.pdf"
            source = fitz.open(str(pdf_path))
            subset = fitz.open()
            try:
                for page_index in page_indexes:
                    subset.insert_pdf(source, from_page=page_index, to_page=page_index)
                subset.save(str(subset_path))
            finally:
                subset.close()
                source.close()
            subset_metas = get_page_metas(subset_path, settings.pdf_render_dpi)
            return self.fallback.recognize(
                subset_path, subset_metas, on_progress=on_progress
            )


def assess_fallback_page(
    blocks: list[Block], page_index: int
) -> PageRecognitionDiagnostic:
    """对视觉 OCR 结果做保守质量检查。"""
    reasons: list[str] = []
    content = "\n".join(block.content for block in blocks if block.content)
    char_count = len(re.sub(r"\s+", "", content))
    located_chars = sum(
        len(re.sub(r"\s+", "", block.content))
        for block in blocks
        if block.content and len(block.bbox) >= 4
    )
    bbox_coverage = located_chars / char_count if char_count else 0.0
    if bbox_coverage >= 0.95:
        location_status = "complete"
    elif bbox_coverage > 0:
        location_status = "partial"
    else:
        location_status = "missing"
    tables = [block.table for block in blocks if block.table is not None]
    if char_count < 8:
        reasons.append("OCR 返回内容为空或字符过少")

    lines = [
        re.sub(r"\s+", "", line)
        for line in content.splitlines()
        if re.sub(r"\s+", "", line)
    ]
    if lines and max(Counter(lines).values()) >= 4:
        reasons.append("OCR 返回连续或大量重复内容")

    for table in tables:
        width = len(table.headers)
        if width < 2:
            reasons.append("OCR 表格缺少有效列")
            continue
        uneven = sum(len(row) != width for row in table.rows)
        if uneven:
            reasons.append("OCR 表格行列数量不一致")
        if table.rows:
            sparse = sum(
                sum(bool(cell.strip()) for cell in row) / width < 0.35
                for row in table.rows
            )
            if sparse / len(table.rows) > 0.5:
                reasons.append("OCR 表格多数数据行字段缺失")

    return PageRecognitionDiagnostic(
        page_index=page_index,
        source="fallback",
        reliable=not reasons,
        reasons=list(dict.fromkeys(reasons)),
        char_count=char_count,
        table_count=len(tables),
        location_status=location_status,
        bbox_coverage=bbox_coverage,
    )
