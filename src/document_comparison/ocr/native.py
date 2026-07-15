"""PDF 原生文本与表格读取。

该 adapter 只处理带可靠文本层的页面：正文按视觉行恢复阅读顺序，表格通过
PyMuPDF 的表格检测恢复为 TableStructure。表格区域内的普通文本会被排除，
避免同一单元格同时以 text 和 table 两种形式重复进入比对链路。
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz  # type: ignore[no-redef]

from ..models import Block, PageMeta, PageRecognitionDiagnostic, TableStructure

_MIN_NATIVE_CHARS = 16
_Y_TOLERANCE = 3.0
_NUMBERED_START = re.compile(
    r"^(?:第[一二三四五六七八九十百千零〇\d]+(?:条|章)|\d+(?:\.\d+)*[.、]|[一二三四五六七八九十]+、)"
)
_FIELD_START = re.compile(
    r"^(?:(?:甲方|乙方|供方|需方|买方|卖方)(?:[（(][^)）]*[)）])?|"
    r"申购单编号|合同编号|地址|开户行|开户银行|帐号|账号|电话|传真|"
    r"签约代表|时间|日期|Contract\s+No)\s*[:：]",
    re.IGNORECASE,
)


@dataclass
class NativePageRead:
    blocks: list[Block]
    reliable: bool
    diagnostic: PageRecognitionDiagnostic


class NativePDFEngine:
    """直接读取 PDF 文本层的 adapter，主要供测试与可信读取 module 复用。"""

    def __init__(self) -> None:
        self.last_diagnostics: list[PageRecognitionDiagnostic] = []

    def recognize(
        self,
        pdf_path: Path,
        page_metas: list[PageMeta],
        *,
        on_progress=None,
    ) -> list[list[Block]]:
        pages: list[list[Block]] = []
        diagnostics: list[PageRecognitionDiagnostic] = []
        with fitz.open(str(pdf_path)) as doc:
            total = max(1, len(doc))
            for page_index, page in enumerate(doc):
                result = read_native_page(page, page_index)
                pages.append(result.blocks)
                diagnostics.append(result.diagnostic)
                if on_progress:
                    on_progress("ocr", 0.10 + 0.60 * ((page_index + 1) / total))
        self.last_diagnostics = diagnostics
        return pages


def read_native_page(page, page_index: int) -> NativePageRead:
    raw_text = page.get_text("text") or ""
    raw_char_count = len(_compact(raw_text))

    table_blocks, table_boxes = _extract_tables(page, page_index)
    text_blocks = _extract_text_outside_tables(page, page_index, table_boxes)
    blocks = sorted([*text_blocks, *table_blocks], key=_block_order)

    extracted_chars = len(_compact("\n".join(block.content for block in blocks)))
    coverage = extracted_chars / raw_char_count if raw_char_count else 0.0
    reliable = bool(table_blocks) or (
        raw_char_count >= _MIN_NATIVE_CHARS and coverage >= 0.80
    )
    reasons: list[str] = []
    if not reliable:
        if raw_char_count < _MIN_NATIVE_CHARS:
            reasons.append("PDF 页面文本层为空或字符过少")
        elif coverage < 0.80:
            reasons.append("PDF 文本层恢复完整度不足")

    diagnostic = PageRecognitionDiagnostic(
        page_index=page_index,
        source="native",
        reliable=reliable,
        reasons=reasons,
        char_count=extracted_chars,
        table_count=len(table_blocks),
    )
    return NativePageRead(blocks=blocks, reliable=reliable, diagnostic=diagnostic)


def _extract_tables(page, page_index: int) -> tuple[list[Block], list[list[float]]]:
    blocks: list[Block] = []
    boxes: list[list[float]] = []
    try:
        found = page.find_tables()
        tables = found.tables
    except Exception:  # pragma: no cover - PyMuPDF 旧版/异常页面降级到正文
        tables = []

    for table_index, table in enumerate(tables):
        extracted = table.extract() or []
        rows = [
            [_normalize_cell(cell) for cell in row]
            for row in extracted
            if row is not None
        ]
        if not rows or max((len(row) for row in rows), default=0) < 2:
            continue
        canonical = _canonicalize_table_rows(rows)
        structure = TableStructure(headers=canonical[0], rows=canonical[1:])
        bbox = [float(value) for value in table.bbox]
        content = "\n".join(" | ".join(row) for row in canonical)
        blocks.append(
            Block(
                block_id=f"p{page_index}-native-table-{table_index}",
                page_index=page_index,
                label="table",
                bbox=bbox,
                content=content,
                table=structure,
            )
        )
        boxes.append(bbox)
    return blocks, boxes


def _extract_text_outside_tables(
    page, page_index: int, table_boxes: list[list[float]]
) -> list[Block]:
    records: list[tuple[str, list[float]]] = []
    document = page.get_text("dict")
    for raw_block in document.get("blocks", []):
        lines = raw_block.get("lines")
        if not lines:
            continue
        line_records: list[tuple[str, list[float]]] = []
        for line in lines:
            bbox = [float(value) for value in line.get("bbox", [])[:4]]
            if len(bbox) < 4 or _inside_any_table(bbox, table_boxes):
                continue
            text = "".join(span.get("text", "") for span in line.get("spans", []))
            text = text.strip()
            if text:
                line_records.append((text, bbox))
        records.extend(_merge_block_lines(line_records))

    records.sort(key=lambda item: (round(item[1][1], 1), item[1][0]))
    records = _merge_continuations(records)
    return [
        Block(
            block_id=f"p{page_index}-native-text-{index}",
            page_index=page_index,
            label="text",
            bbox=bbox,
            content=text,
        )
        for index, (text, bbox) in enumerate(records)
        if text
    ]


def _merge_block_lines(
    lines: list[tuple[str, list[float]]]
) -> list[tuple[str, list[float]]]:
    if not lines:
        return []
    lines = sorted(lines, key=lambda item: (item[1][1], item[1][0]))
    visual_rows: list[list[tuple[str, list[float]]]] = []
    for item in lines:
        if not visual_rows or abs(item[1][1] - visual_rows[-1][0][1][1]) > _Y_TOLERANCE:
            visual_rows.append([item])
        else:
            visual_rows[-1].append(item)

    row_records: list[tuple[str, list[float]]] = []
    for row in visual_rows:
        row.sort(key=lambda item: item[1][0])
        # 签字区常把左右两列字段放在同一个 PyMuPDF block；字段必须保持独立。
        if len(row) > 1 and all(_is_field_start(text) for text, _ in row):
            row_records.extend(row)
            continue
        text = ""
        bbox = list(row[0][1])
        previous_box: list[float] | None = None
        for fragment, fragment_box in row:
            if text:
                text += _fragment_separator(text, fragment, previous_box, fragment_box)
            text += fragment
            bbox = _union_bbox(bbox, fragment_box)
            previous_box = fragment_box
        row_records.append((text, bbox))

    merged: list[tuple[str, list[float]]] = []
    for text, bbox in row_records:
        if not merged or _is_logical_start(text):
            merged.append((text, bbox))
            continue
        previous_text, previous_bbox = merged[-1]
        separator = "\n" if _NUMBERED_START.match(previous_text) and len(previous_text) < 30 else ""
        merged[-1] = (previous_text + separator + text, _union_bbox(previous_bbox, bbox))
    return merged


def _merge_continuations(
    records: list[tuple[str, list[float]]]
) -> list[tuple[str, list[float]]]:
    merged: list[tuple[str, list[float]]] = []
    for text, bbox in records:
        if not merged or _is_logical_start(text):
            merged.append((text, bbox))
            continue
        previous_text, previous_bbox = merged[-1]
        # 大段正文的视觉换行不应进入语义文本；短编号标题与正文之间保留换行。
        separator = "\n" if _NUMBERED_START.match(previous_text) and len(previous_text) < 30 else ""
        merged[-1] = (previous_text + separator + text, _union_bbox(previous_bbox, bbox))
    return merged


def _is_logical_start(text: str) -> bool:
    return bool(_NUMBERED_START.match(text) or _is_field_start(text))


def _is_field_start(text: str) -> bool:
    return bool(_FIELD_START.match(text))


def _fragment_separator(
    left: str,
    right: str,
    left_box: list[float] | None,
    right_box: list[float],
) -> str:
    if not left or not right:
        return ""
    gap = right_box[0] - left_box[2] if left_box else 0.0
    if gap <= 2:
        return ""
    if left[-1].isascii() and left[-1].isalnum() and right[0].isascii() and right[0].isalnum():
        return " "
    return ""


def _normalize_cell(value) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    # PDF 表格中的换行通常只是单元格内自动折行，不具有段落语义。
    text = "".join(part.strip() for part in text.splitlines())
    return re.sub(r"[ \t\u3000]+", " ", text).strip()


def _canonicalize_table_rows(rows: list[list[str]]) -> list[list[str]]:
    """对齐 python-docx 的合并单元格表示。

    PyMuPDF 会把横向合并格展开为「值 + 多个空格」，python-docx 则只保留
    物理单元格。对仅含一两个值的合计/说明行压缩空格；普通数据行只移除尾部
    空格，避免空备注列制造结构性差异。
    """
    canonical: list[list[str]] = []
    for row in rows:
        trimmed = list(row)
        while trimmed and not trimmed[-1]:
            trimmed.pop()
        nonempty = [cell for cell in trimmed if cell]
        if len(nonempty) <= 2:
            trimmed = nonempty
        canonical.append(trimmed)
    return canonical


def _inside_any_table(bbox: list[float], table_boxes: list[list[float]]) -> bool:
    cx = (bbox[0] + bbox[2]) / 2
    cy = (bbox[1] + bbox[3]) / 2
    return any(box[0] <= cx <= box[2] and box[1] <= cy <= box[3] for box in table_boxes)


def _union_bbox(left: list[float], right: list[float]) -> list[float]:
    return [
        min(left[0], right[0]),
        min(left[1], right[1]),
        max(left[2], right[2]),
        max(left[3], right[3]),
    ]


def _block_order(block: Block) -> tuple[float, float]:
    if len(block.bbox) < 4:
        return (float(block.page_index), 0.0)
    return (block.bbox[1], block.bbox[0])


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)
