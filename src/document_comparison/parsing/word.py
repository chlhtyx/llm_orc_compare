"""Word(基准件)解析:python-docx → RawItem 流。

保留段落顺序、标题样式层级、表格结构(§5.1)。
"""
from __future__ import annotations

import re
from pathlib import Path

from docx import Document  # type: ignore[import-untyped]
from docx.document import Document as _Doc  # type: ignore[import-untyped]
from docx.table import Table  # type: ignore[import-untyped]
from docx.text.paragraph import Paragraph  # type: ignore[import-untyped]

from ..models import RawItem

_HEADING_RE = re.compile(r"heading\s*(\d+)", re.IGNORECASE)


def _heading_level(style_name: str) -> int:
    m = _HEADING_RE.search(style_name or "")
    return int(m.group(1)) if m else 1


def _iter_block_items(doc: _Doc):
    """按文档流顺序产出段落与表格(python-docx 默认不混序遍历)。"""
    from docx.oxml.ns import qn  # type: ignore[import-untyped]

    parent = doc.element.body
    for child in parent.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, doc)
        elif child.tag == qn("w:tbl"):
            yield Table(child, doc)


def _table_to_text(table: Table) -> str:
    rows = []
    for row in table.rows:
        cells = [c.text.strip() for c in row.cells]
        rows.append(" | ".join(cells))
    return "\n".join(rows)


def parse_word(path: str | Path) -> list[RawItem]:
    """把 Word 文档解析为 RawItem 列表(文档流顺序即阅读顺序)。"""
    doc = Document(str(path))
    items: list[RawItem] = []
    for block in _iter_block_items(doc):
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if not text:
                continue
            style = (block.style.name or "") if block.style else ""
            if "heading" in style.lower() or "title" in style.lower():
                items.append(
                    RawItem(
                        text=text,
                        kind="heading",
                        heading_level=_heading_level(style),
                    )
                )
            else:
                items.append(RawItem(text=text, kind="paragraph"))
        elif isinstance(block, Table):
            txt = _table_to_text(block)
            if txt.strip():
                items.append(RawItem(text=txt, kind="table"))
    return items
