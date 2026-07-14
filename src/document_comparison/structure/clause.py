"""条款切分:编号识别 + 层级重建(§5.3)。

支持中文合同常见编号:第X条/章、X.X[.X]、(X)/（X）、一、/ 1.
单个文本块内若含多行/多编号,按行拆分后逐行判定。
编号模式要求后接分隔符或行尾,避免「第三条正文…」被误判。

无编号行额外做「键值块识别」(甲方/乙方/地址/电话/日期等字段名前缀),
命中则开新条款并填入 field_key,供对齐层按字段名锚定配对,
避免首部/签字页这类无编号键值块因两端切分边界不一致而被误判 added/deleted。
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

# —— 键值块字段名识别(无编号时启用)——
# 命中后返回归一化 field_key,用于对齐层字段锚定。
# 归一化策略:把多变体(「甲方(甲方主体)」「甲方(签字/盖章)」)映射到稳定 key,
# 使两端不同写法能配对。
# 角色(甲方/乙方...)可带括号限定语,如「甲方(甲方主体)」「甲方(签字/盖章)」。
_ROLE = (
    r"甲方|乙方|供方|需方|出租方|承租方|买方|卖方|发包方|承包方|"
    r"许可方|被许可方|委托方|受托方|转让方|受让方"
)
# 角色 + 可选括号限定语(如「(甲方主体)」「(签字/盖章)」)+ 冒号
# 注意:「签字/盖章」整体用 (?:...)? 而非字级可选,避免「签」被误当作必选。
_ROLE_FIELD_RE = re.compile(
    rf"^({_ROLE})(?:[（(][^)）]*[)）])?\s*(?:[（(]?(?:签字|盖章)[/／]?(?:签字|盖章)?[)）]?)?\s*[:：]"
)
# 纯字段名(无角色):统一信用代码/地址/电话/开户行/账号/日期 等。
# 「统一社会信用代码/身份证号」作为整体字段名(含「/」)。
_PLAIN_FIELD_RE = re.compile(
    r"^(统一社会信用代码(?:[/／]身份证号)?|身份证号|法定代表人|负责人|联系电话|联系地址|"
    r"开户行|开户银行|账户名|账户名称|账号|账\s*号|日期|签约日期|签订日期|"
    r"地址|电话|传真|邮编|邮箱|电子邮箱)\s*[:：]"
)

# 字段名归一化表:多变体 → 稳定 key(去空白后匹配)
_FIELD_NORMALIZE: dict[str, str] = {
    "联系电话": "电话",
    "联系地址": "地址",
    "开户银行": "开户行",
    "账户名称": "账户名",
    "账号": "账号",
    "统一社会信用代码/身份证号": "统一社会信用代码",
    "签约日期": "日期",
    "签订日期": "日期",
}


def _normalize_field_key(raw: str) -> str:
    """字段名归一化:去空白后查表,未命中则原样返回(去首尾空白)。"""
    key = re.sub(r"\s+", "", raw)
    return _FIELD_NORMALIZE.get(key, raw.strip())


def detect_field_key(text: str) -> tuple[str, str] | None:
    """识别行首键值字段名,返回 (字段名前缀原文, 归一化 field_key) 或 None。

    用于无编号行的字段锚定。优先级低于 detect_number(由调用方保证)。
    """
    t = text.lstrip()
    # 角色字段优先(甲方/乙方...),再退到纯字段名
    m = _ROLE_FIELD_RE.match(t)
    if m:
        prefix = m.group(0)
        # 归一化:只取角色名(去括号限定语与签字盖章修饰)
        role = m.group(1)
        return (prefix, _normalize_field_key(role))
    m = _PLAIN_FIELD_RE.match(t)
    if m:
        prefix = m.group(0)
        field = m.group(1)
        return (prefix, _normalize_field_key(field))
    return None


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
    """把 OCR 每页 Block 列表拍平为 RawItem 流(保留 bbox/page/table)。"""
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
                    table=b.table,
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

    def new_clause(number, level, title, text, item: RawItem, field_key: str = "") -> Clause:
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
            field_key=field_key,
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
            # 透传结构化表格到条款,供单元格级比对
            if item.table is not None:
                current.tables.append(item.table)
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

        # paragraph:按行拆分(单个 block 可能含多行/多编号/多字段)
        # 优先级:编号 > 字段名 > 续入当前条款
        for line_item in _split_lines_with_bbox(item, norm):
            line = line_item.text
            num = detect_number(line)
            if num:
                prefix, number, level = num
                body = _split_body(line, prefix)
                current = new_clause(number, level, body, body, line_item)
                clauses.append(current)
                continue
            # 无编号:尝试键值字段识别(甲方:/地址:/日期 等)
            field = detect_field_key(line)
            if field:
                prefix, field_key = field
                body = _split_body(line, prefix)
                # 字段块:正文为冒号后的值,标题用归一化 field_key 便于阅读
                current = new_clause("", 0, field_key, body or line, line_item, field_key)
                clauses.append(current)
                continue
            if current is None:
                current = new_clause("", 0, "", line, line_item)
                clauses.append(current)
            else:
                sep = "\n" if current.text else ""
                current.text += sep + line
                if line_item.bbox:
                    current.blocks.append(_raw_to_block(line_item))

    return clauses
