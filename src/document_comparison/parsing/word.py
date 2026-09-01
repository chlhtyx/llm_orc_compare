"""Word(基准件)解析:python-docx → RawItem 流。

保留段落顺序、标题样式层级、表格结构(§5.1)。
"""
from __future__ import annotations

import logging
import re
import time
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from docx import Document  # type: ignore[import-untyped]
from docx.document import Document as _Doc  # type: ignore[import-untyped]
from docx.table import Table  # type: ignore[import-untyped]
from docx.text.paragraph import Paragraph  # type: ignore[import-untyped]

from ..models import RawItem, TableStructure
from ..structure.normalize import normalize_table_text

logger = logging.getLogger(__name__)

_HEADING_RE = re.compile(r"heading\s*(\d+)", re.IGNORECASE)
_APP_PROPERTIES_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
)


def _heading_level(style_name: str) -> int:
    m = _HEADING_RE.search(style_name or "")
    return int(m.group(1)) if m else 1


def _accepted_text(element) -> str:
    """返回 oxml 元素(``<w:p>`` 段落或 ``<w:tc>`` 单元格)「接受所有修订」的文本视图。

    python-docx 的 ``paragraph.text`` / ``cell.text`` 只取段落**直接** ``<w:r>``
    子元素的文本;被修订标记(``<w:ins>``/``<w:del>``)包裹的 run 不在
    ``paragraph.runs`` 中,于是带修订的段落会被整段丢弃成空串(实测 python-docx 1.2.0)。

    OOXML 中普通文本与删除痕迹用**不同标签**:``<w:t>``(普通 + 插入痕迹)vs
    ``<w:delText>``(删除痕迹)。因此遍历 ``<w:p>`` 内的 ``<w:t>`` 与 ``<w:br>`` 后代、
    按文档顺序拼接(``<w:br>`` 产出 ``\\n``,``<w:delText>`` 天然被排除),即得到
    「接受修订」视图——删除痕迹丢弃,插入痕迹(在 ``<w:ins>`` 内的 ``<w:t>``)并入,
    普通文本与单元格内换行保留。分页符(``<w:br w:type="page"/>``)不计入文本
    (由 ``_paragraph_page_break_count`` 单独处理页序)。

    单元格(``<w:tc>``)可含多个段落,段落间用 ``\\n`` 分隔,与 python-docx ``cell.text``
    对称(下游 ``_table_rows`` 再把 ``\\n`` 拍平为空格)。
    """
    from docx.oxml.ns import qn  # type: ignore[import-untyped]

    w_p = qn("w:p")
    w_t = qn("w:t")
    w_br = qn("w:br")

    def _para_text(p) -> str:
        parts: list[str] = []
        for el in p.iter():
            if el.tag == w_t:
                parts.append(el.text or "")
            elif el.tag == w_br and el.get(qn("w:type")) != "page":
                parts.append("\n")
        return "".join(parts)

    if element.tag == w_p:
        return _para_text(element)
    # 容器(如 <w:tc>):按直接子 <w:p> 顺序拼接,段间插换行
    paras = [c for c in element.iterchildren() if c.tag == w_p]
    if not paras:
        # 兜底:无直接子段落(异常结构),退化到全量遍历
        return _para_text(element)
    return "\n".join(_para_text(p) for p in paras)


def _count_revisions(doc: _Doc) -> int:
    """统计文档体中未接受的修订标记数(``<w:ins>`` + ``<w:del>``),用于摘要日志。

    仅计数,不影响解析;>0 提示运维「这份 docx 带修订,文本已按接受视图产出」。
    """
    from docx.oxml.ns import qn  # type: ignore[import-untyped]

    body = doc.element.body
    return sum(1 for _ in body.iter(qn("w:ins"))) + sum(
        1 for _ in body.iter(qn("w:del"))
    )


def _iter_block_items(doc: _Doc):
    """按文档流顺序产出段落与表格(python-docx 默认不混序遍历)。"""
    from docx.oxml.ns import qn  # type: ignore[import-untyped]

    parent = doc.element.body
    for child in parent.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, doc)
        elif child.tag == qn("w:tbl"):
            yield Table(child, doc)


def _paragraph_page_break_count(paragraph: Paragraph) -> int:
    """返回段落中的显式或 Word 保存时记录的分页标记数。"""
    from docx.oxml.ns import qn  # type: ignore[import-untyped]

    count = 0
    for element in paragraph._p.iter():
        if element.tag == qn("w:br") and element.get(qn("w:type")) == "page":
            count += 1
        elif element.tag == qn("w:lastRenderedPageBreak"):
            count += 1
    return count


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
            cells.append(
                _accepted_text(c._tc).replace("\n", " ").replace("\r", " ").strip()
            )
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


def estimate_page_count(path: str | Path) -> int:
    """估算 Word 文档页数(用于回收件页数截取)。

    优先读取 Word 保存时写入 ``docProps/app.xml`` 的 ``Pages`` 属性，并与
    OOXML 分页标记推导值取较大者:
    - `<w:br w:type="page"/>`:显式手动分页符。
    - `<w:lastRenderedPageBreak/>`:Word 上次保存时记录的软分页位置
      (仅由 Word 写入,LibreOffice 等不一定生成,缺失时估算会偏少)。

    分页标记页数 = 命中数 + 1(末尾内容默认占一页)。
    两者都缺失时返回 0 表示"无法估算"(此时按 1 截取会截掉真实内容,
    调用方必须跳过截取而非采信);调用方已知真实页数时应直接传
    original_page_count 覆盖。
    """
    from docx.oxml.ns import qn  # type: ignore[import-untyped]

    doc = Document(str(path))
    body = doc.element.body
    breaks = 0
    for child in body.iter():
        if child.tag == qn("w:br") and child.get(qn("w:type")) == "page":
            breaks += 1
        elif child.tag == qn("w:lastRenderedPageBreak"):
            breaks += 1
    saved_pages = _saved_word_page_count(path)
    break_pages = breaks + 1 if breaks else 0
    selected_pages = max(break_pages, saved_pages)
    logger.info(
        "docx page count resolved saved_pages=%s break_pages=%s selected=%s",
        saved_pages,
        break_pages,
        selected_pages,
    )
    return selected_pages


def _saved_word_page_count(path: str | Path) -> int:
    """读取 Word 最近一次保存时记录的页数；缺失或非法时返回 0。"""
    try:
        with ZipFile(path) as archive:
            app_properties = archive.read("docProps/app.xml")
        root = ElementTree.fromstring(app_properties)
    except (BadZipFile, KeyError, OSError, ElementTree.ParseError):
        return 0
    pages = root.find(f"{{{_APP_PROPERTIES_NS}}}Pages")
    if pages is None or pages.text is None:
        return 0
    try:
        value = int(pages.text)
    except ValueError:
        return 0
    return value if value > 0 else 0


def parse_word(path: str | Path) -> list[RawItem]:
    """把 Word 文档解析为 RawItem 列表(文档流顺序即阅读顺序)。"""
    started_at = time.perf_counter()
    doc = Document(str(path))
    items: list[RawItem] = []
    skipped_empty = 0
    page_index = 0
    for block in _iter_block_items(doc):
        if isinstance(block, Paragraph):
            text = _accepted_text(block._p).strip()
            if not text:
                skipped_empty += 1
            else:
                style = (block.style.name or "") if block.style else ""
                if "heading" in style.lower() or "title" in style.lower():
                    items.append(
                        RawItem(
                            text=text,
                            kind="heading",
                            heading_level=_heading_level(style),
                            page_index=page_index,
                        )
                    )
                else:
                    items.append(
                        RawItem(text=text, kind="paragraph", page_index=page_index)
                    )
            page_index += _paragraph_page_break_count(block)
        elif isinstance(block, Table):
            txt = _table_to_text(block)
            if txt.strip():
                items.append(
                    RawItem(
                        text=txt,
                        kind="table",
                        table=_table_to_structure(block),
                        page_index=page_index,
                    )
                )
    _log_parse_summary(path, doc, items, skipped_empty, started_at)
    return items


def _log_parse_summary(
    path: str | Path,
    doc: _Doc,
    items: list[RawItem],
    skipped_empty: int,
    started_at: float,
) -> None:
    """记录 docx 解析摘要:文档体量、各类块计数、表格规模、总字符数、耗时。

    排查对齐/比对异常时,先看这条日志确认 Word 侧解析输入是否正常
    (块数是否为 0、标题层级是否丢失、表格是否被空跳过等)。
    """
    kind_counts = Counter(item.kind for item in items)
    total_chars = sum(len(item.text) for item in items)
    # 标题层级分布:level → 数量,便于核对编号切分前的原始层级
    heading_levels = Counter(
        item.heading_level for item in items if item.kind == "heading"
    )
    table_dims = [
        f"{len(t.rows)}x{len(t.headers)}"
        for item in items
        if item.kind == "table" and (t := item.table) is not None
    ]
    n_tables = kind_counts.get("table", 0)
    doc_paragraphs = len(doc.paragraphs)
    doc_tables = len(doc.tables)
    # 修订痕迹计数(<w:ins>/<w:del>):>0 说明这份 docx 带未接受修订,已按「接受修订」
    # 视图解析(删除痕迹丢弃、插入痕迹并入)。便于排查「解析文本为何与原始 docx 截图不同」。
    revision_count = _count_revisions(doc)
    logger.info(
        "word parsed path=%s items=%s paragraphs=%s tables=%s headings=%s "
        "heading_levels=%s table_dims=%s total_chars=%s skipped_empty=%s "
        "revisions=%s elapsed=%.3fs",
        Path(path).name,
        len(items),
        doc_paragraphs,
        n_tables,
        kind_counts.get("heading", 0),
        dict(heading_levels) if heading_levels else "{}",
        ",".join(table_dims) if table_dims else "-",
        total_chars,
        skipped_empty,
        revision_count,
        time.perf_counter() - started_at,
    )
    if doc_tables and n_tables == 0:
        logger.warning(
            "word 文档含 %s 张表格但解析后 0 张入库,可能全部为空表格 path=%s",
            doc_tables, Path(path).name,
        )
