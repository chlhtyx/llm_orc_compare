"""条款切分:编号识别 + 层级重建(§5.3)。

支持中文合同常见编号:第X条/章、X.X[.X]、(X)/（X）、一、/ 1.
单个文本块内若含多行/多编号,按行拆分后逐行判定。
编号模式要求后接分隔符或行尾,避免「第三条正文…」被误判。
"""
from __future__ import annotations

import re

from ..models import Block, Clause, DocType, RawItem
from .normalize import normalize_text

_SEP = r"(?=[\s，,。：:、]|$)"

# —— 编号模式(顺序敏感)—— 返回 (prefix, number, level) ——
_NUM_PATTERNS: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"^第([一二三四五六七八九十百千零〇\d]+)(?:条|章)" + _SEP), 1),
    (re.compile(r"^(\d+\.\d+(?:\.\d+)?)" + _SEP), 2),  # X.X / X.X.X
    (re.compile(r"^[（(]([一二三四五六七八九十\d]+)[)）]"), 3),
    (re.compile(r"^([一二三四五六七八九十]+)、"), 2),
    (re.compile(r"^(\d+)[\.、]"), 2),
]


def detect_number(text: str) -> tuple[str, str, int] | None:
    """识别行首编号,返回 (整个前缀, 编号值, 层级) 或 None。

    prefix 用于从文本剥离编号,number 用于两端锚定匹配。
    """
    t = text.lstrip()
    for pat, base_level in _NUM_PATTERNS:
        m = pat.match(t)
        if not m:
            continue
        prefix = m.group(0)
        number = m.group(1)
        level = base_level
        if "." in number:  # X.X.X 按点数提升层级
            level = 1 + number.count(".")
        return (prefix, number, level)
    return None


# —— Block → RawItem ——
_LABEL_KIND = {
    "doc_title": "title",
    "paragraph_title": "heading",
    "title": "heading",
    "table": "table",
}


def blocks_to_raw(pages: list[list[Block]]) -> list[RawItem]:
    """把 OCR 每页 Block 列表拍平为 RawItem 流(保留 bbox/page)。"""
    items: list[RawItem] = []
    for blocks in pages:
        for b in blocks:
            kind = _LABEL_KIND.get(b.label, "paragraph")
            items.append(
                RawItem(
                    text=b.content,
                    kind=kind,  # type: ignore[arg-type]
                    page_index=b.page_index,
                    bbox=list(b.bbox),
                )
            )
    return items


def _raw_to_block(item: RawItem) -> Block:
    return Block(
        block_id=f"raw-{item.page_index}-{id(item)}",
        page_index=item.page_index,
        label=item.kind,
        bbox=list(item.bbox),
        content=item.text,
    )


def _line_item(item: RawItem, text: str, line_index: int, line_count: int) -> RawItem:
    if not item.bbox or line_count <= 1:
        return RawItem(
            text=text,
            kind=item.kind,
            heading_level=item.heading_level,
            page_index=item.page_index,
            bbox=list(item.bbox),
        )
    x1, y1, x2, y2 = item.bbox[:4]
    line_h = (y2 - y1) / line_count
    return RawItem(
        text=text,
        kind=item.kind,
        heading_level=item.heading_level,
        page_index=item.page_index,
        bbox=[x1, y1 + line_h * line_index, x2, y1 + line_h * (line_index + 1)],
    )


def _split_lines_with_bbox(item: RawItem, norm: str) -> list[RawItem]:
    lines = [line.strip() for line in norm.split("\n") if line.strip()]
    if not lines:
        return []
    return [_line_item(item, line, idx, len(lines)) for idx, line in enumerate(lines)]


def _split_body(line: str, prefix: str) -> str:
    """用编号前缀长度切片,去掉编号后清理分隔符。"""
    rest = line[len(prefix):]
    return rest.lstrip(" .、．)）:：").strip()


def build_clauses(raw_items: list[RawItem], doc_type: DocType) -> list[Clause]:
    """把 RawItem 流切分为 Clause 列表(§5.3)。

    - heading/title → 尝试提编号;有则按编号开新条款,否则按标题层级
    - table → 归入最近条款正文
    - paragraph → 按行拆分,行首有编号则开新条款,否则续入当前条款
    """
    clauses: list[Clause] = []
    current: Clause | None = None
    counter = 0

    def new_clause(number, level, title, text, item: RawItem) -> Clause:
        nonlocal counter
        counter += 1
        blocks = [_raw_to_block(item)] if item.bbox else []
        return Clause(
            clause_id=f"{doc_type}-{counter}",
            doc_type=doc_type,
            level=level,
            number=number,
            title=title,
            text=text,
            blocks=blocks,
        )

    for item in raw_items:
        norm = normalize_text(item.text)
        if not norm:
            continue

        if item.kind == "table":
            if current is None:
                current = new_clause("", 0, "", norm, item)
                clauses.append(current)
            else:
                sep = "\n" if current.text else ""
                current.text += sep + norm
                if item.bbox:
                    current.blocks.append(_raw_to_block(item))
            continue

        if item.kind in ("heading", "title"):
            num = detect_number(norm)
            if num:
                prefix, number, level = num
                body = _split_body(norm, prefix)
                current = new_clause(number, level, body or norm, body or norm, item)
            else:
                current = new_clause("", item.heading_level or 1, norm, norm, item)
            clauses.append(current)
            continue

        # paragraph:按行拆分(单个 block 可能含多行/多编号)
        for line_item in _split_lines_with_bbox(item, norm):
            line = line_item.text
            num = detect_number(line)
            if num:
                prefix, number, level = num
                body = _split_body(line, prefix)
                current = new_clause(number, level, body, body, line_item)
                clauses.append(current)
            elif current is None:
                current = new_clause("", 0, "", line, line_item)
                clauses.append(current)
            else:
                sep = "\n" if current.text else ""
                current.text += sep + line
                if line_item.bbox:
                    current.blocks.append(_raw_to_block(line_item))

    return clauses
