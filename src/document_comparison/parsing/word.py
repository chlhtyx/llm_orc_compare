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

from ..models import RawItem, TableStructure
from ..structure.normalize import normalize_table_text

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


def _table_rows(table: Table) -> list[list[str]]:
    """提取表格各行单元格文本(已 strip),处理两类常见版式:

    1. 单元格内换行(表头如「数\\n量」):拍平为空格。否则下游 normalize_table_text
       会把 \\n 当行边界,把一行表头错拆成两行,导致 headers 列数与数据行不一致、
       列对齐崩溃、table_elements_changed 的 col_header 定位失效。
    2. 水平合并单元格(gridSpan):row.cells 对合并格会重复返回同一物理格(_tc),
       这里按 _tc 指针去重,使合并区只产出一次。否则底部合计行会被展平成
       ['含税合计','含税合计'×3, '2815元'×9] 这类重复,与 OCR 侧对比时产生伪 diff。

    边界:仅处理水平合并;垂直合并(vMerge)python-docx 默认在续行返回空串,
    无需在此特殊处理(空串参与比对无害)。
    """
    rows: list[list[str]] = []
    for row in table.rows:
        seen_tc: set[int] = set()
        cells: list[str] = []
        for c in row.cells:
            tc_id = id(c._tc)
            if tc_id in seen_tc:
                continue  # 跳过水平合并(gridSpan)的后续物理格
            seen_tc.add(tc_id)
            cells.append(c.text.replace("\n", " ").replace("\r", " ").strip())
        rows.append(cells)
    return rows


def _table_to_text(table: Table) -> str:
    rows = [" | ".join(cells) for cells in _table_rows(table)]
    # 过一遍表格规范化,与 OCR 侧(label=table 的 block)统一为同一格式,
    # 消除两端表达同一张表时的文本差异(否则 difflib 会产生大量伪差异)。
    # 对此处产出的「 | 」格式是幂等 no-op,但锁定格式契约、保证两端对称。
    return normalize_table_text("\n".join(rows))


def _table_to_structure(table: Table) -> TableStructure | None:
    """把 Word 表格提取为结构化 TableStructure。

    第一行视为表头(headers),其余为数据行(rows)。各行经 normalize_table_text
    规范化,与 OCR 侧对称。若表格为空或仅空单元格,返回 None。
    """
    raw_rows = _table_rows(table)
    if not raw_rows:
        return None
    # 过一遍纯文本规范化以对齐两端格式,再拆回单元格
    norm_text = normalize_table_text("\n".join(" | ".join(cells) for cells in raw_rows))
    if not norm_text.strip():
        return None
    norm_rows = [line.split(" | ") for line in norm_text.split("\n")]
    headers = norm_rows[0]
    rows = norm_rows[1:]
    return TableStructure(headers=headers, rows=rows)


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
                items.append(
                    RawItem(text=txt, kind="table", table=_table_to_structure(block))
                )
    return items
