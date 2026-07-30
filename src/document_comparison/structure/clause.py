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

# PaddleOCR-VL 的 Markdown fallback 会给标题加 ``# `` / ``## `` 等语法标记。
# 这些是识别结果的版式元数据，不是合同正文；若直接参与编号识别，
# ``## 第一条`` 无法与 Word 侧的 ``第一条`` 锚定，进而产生一增一删。
# 必须要求 # 后存在空白，避免误删 ``#合同编号`` 等真实文本。
_MARKDOWN_HEADING_RE = re.compile(r"^#{1,6}[ \t]+")

# PDF 文本层有时会把多个视觉逻辑行压成同一个 text line，例如
# ``...3.2 支付方式...``、``开户行:...账户名:...``。这些起点必须在
# 条款切分前恢复，否则字段/编号锚定会把多个条款错误合并。
_INLINE_NUMBER_RE = re.compile(
    # 合同编号通常为 1~2 位段号；限制每段长度避免把 50000.00、2026.08
    # 等金额/日期小数误识别为新条款。
    r"(?:(?<!\d)\d{1,2}\.\d{1,2}(?:\.\d{1,2})?[.、]?\s+|[（(][一二三四五六七八九十\d]+[)）])"
)
_INLINE_CN_NUMBER_RE = re.compile(
    r"[一二三四五六七八九十]{1,3}\s*[、.．]\s*(?=\S)"
)
_INLINE_CHAPTER_RE = re.compile(
    r"第[一二三四五六七八九十百千零〇\d]+(?:条|章)"
)
_INLINE_FIELD_RE = re.compile(
    r"(?:统一社会信用代码(?:[/／]身份证号)?|身份证号|法定代表人|负责人|"
    r"联系电话|联系地址|开户行|开户银行|账户名|账户名称|账号|帐号|账\s*号|"
    r"日期|时间|签约日期|签订日期|签约代表|地址|电话|传真|邮编|邮箱|电子邮箱)\s*[:：]"
)

# —— 编号模式(顺序敏感)—— 返回 (prefix, number, level) ——
_NUM_PATTERNS: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"^第([一二三四五六七八九十百千零〇\d]+)(?:条|章)" + _SEP), 1),
    (re.compile(r"^(\d+\.\d+(?:\.\d+)?)" + _SEP), 2),  # X.X / X.X.X
    (re.compile(r"^[（(]([一二三四五六七八九十\d]+)[)）]"), 3),
    # Word 自动编号转纯文本后可能在中文序号与顿号间留下排版空格，
    # 如「一 、 合同标的」。只放宽行首编号前缀，不改动原文参与差异裁决。
    (re.compile(r"^([一二三四五六七八九十]+)\s*[、.．]\s*"), 2),
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
    r"开户行|开户银行|账户名|账户名称|账号|帐号|账\s*号|日期|时间|签约日期|签订日期|"
    r"签约代表|地址|电话|传真|邮编|邮箱|电子邮箱)\s*[:：]"
)

# 字段名归一化表:多变体 → 稳定 key(去空白后匹配)
_FIELD_NORMALIZE: dict[str, str] = {
    "联系电话": "电话",
    "联系地址": "地址",
    "开户银行": "开户行",
    "账户名称": "账户名",
    "账号": "账号",
    "帐号": "账号",
    "统一社会信用代码/身份证号": "统一社会信用代码",
    "签约日期": "日期",
    "签订日期": "日期",
    "时间": "日期",
}

# 业务条款标题在 Word 中经常没有显式编号，而盖章 PDF 会显示自动编号。
# 以稳定标题作为 field_key 锚点，可跨越这种版式差异进行配对。
_SECTION_TITLES = (
    "采购产品明细",
    "质量要求",
    "产品包装",
    "交货地点",
    "产品验收",
    "付款方式",
    "违约责任",
    "争议解决",
    "其他约定",
)
_SECTION_RE = re.compile(
    rf"^({'|'.join(map(re.escape, _SECTION_TITLES))})(?:\s*[:：]|$)"
)


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


def detect_section_key(text: str) -> tuple[str, str] | None:
    """识别稳定的合同业务条款标题，返回 (标题, section field_key)。"""
    match = _SECTION_RE.match(text.lstrip())
    if not match:
        return None
    title = match.group(1)
    return title, f"section:{title}"


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
    lines = _split_logical_lines(norm)
    if not lines:
        return []
    return [_line_item(item, line, idx, len(lines)) for idx, line in enumerate(lines)]


def _split_logical_lines(norm: str) -> list[str]:
    """按显式换行及块内编号/字段起点恢复逻辑行。

    这是对 PDF 原生文本层的保守修复：只在强锚点处拆分，不按普通中文标点
    拆正文。紧凑章标题（如 ``第一条合作内容``）只在逻辑行开头接受，避免
    把正文中的「第一条」引用误识别为新条款。
    """
    logical_lines: list[str] = []
    for raw_line in norm.split("\n"):
        line = raw_line.strip()
        if not line:
            continue

        starts = [0]
        for match in _INLINE_NUMBER_RE.finditer(line):
            index = match.start()
            if index == 0:
                continue
            previous = line[index - 1]
            # (2) 通常跟在分号/顿号后；数字编号要求后面有空格，避免
            # 把金额小数或日期中的点拆开。
            if match.group(0).startswith(("(", "（")) and not (
                previous.isspace() or previous in "。；;、:："
            ):
                continue
            starts.append(index)

        for match in _INLINE_CN_NUMBER_RE.finditer(line):
            index = match.start()
            if index == 0:
                continue
            previous = line[index - 1]
            if not (previous.isspace() or previous in "。；;、:："):
                continue
            starts.append(index)

        for match in _INLINE_FIELD_RE.finditer(line):
            if match.start() > 0:
                starts.append(match.start())

        # 紧凑中文章标题可能没有空格，但只接受行首或标点后的起点。
        for match in _INLINE_CHAPTER_RE.finditer(line):
            index = match.start()
            if index == 0:
                starts.append(index)
            elif line[index - 1] in "。；;、:：":
                starts.append(index)

        starts = sorted(set(starts))
        for start, end in zip(starts, [*starts[1:], len(line)]):
            part = line[start:end].strip()
            if not part:
                continue
            # 仅对条款行首的紧凑章节补一个解析用分隔符；输出正文仍不含该空格。
            chapter = _INLINE_CHAPTER_RE.match(part)
            compact_body = part[chapter.end():].lstrip() if chapter else ""
            if (
                chapter
                and chapter.end() < len(part)
                and not part[chapter.end()].isspace()
                and _looks_like_compact_chapter_heading(compact_body)
            ):
                part = f"{part[:chapter.end()]} {part[chapter.end():]}"
            logical_lines.append(part)
    return logical_lines


def _looks_like_compact_chapter_heading(body: str) -> bool:
    """判断无分隔符章标题，避免把「第三条正文内容。」当作新编号。"""
    if not body or len(body) > 20:
        return False
    return not any(char in body for char in "，,。；;:：、!?！？")


def _split_body(line: str, prefix: str) -> str:
    """用编号前缀长度切片,去掉编号后清理分隔符。"""
    rest = line[len(prefix):]
    return rest.lstrip(" .、．)）:：").strip()


def _comparison_text(text: str, doc_type: DocType) -> str:
    """返回结构识别使用的文本，不改动 Word 原文或 OCR 定位证据。"""
    if doc_type != "pdf":
        return text
    return _MARKDOWN_HEADING_RE.sub("", text, count=1)


def _raw_evidence_item(item: RawItem, text: str) -> RawItem:
    """为已清理的单行比较文本恢复 OCR 原文，同时保留其定位信息。"""
    return RawItem(
        text=text,
        kind=item.kind,
        heading_level=item.heading_level,
        page_index=item.page_index,
        bbox=list(item.bbox),
        table=item.table,
    )


def build_clauses(raw_items: list[RawItem], doc_type: DocType) -> list[Clause]:
    """把 RawItem 流切分为 Clause 列表(§5.3)。

    - heading/title → 尝试提编号;有则按编号开新条款,否则按标题层级
    - table → 归入最近条款正文
    - paragraph → 按行拆分,行首有编号则开新条款,否则续入当前条款
    """
    clauses: list[Clause] = []
    current: Clause | None = None
    counter = 0
    hierarchy: dict[int, str] = {}

    def new_clause(number, level, title, text, item: RawItem, field_key: str = "") -> Clause:
        nonlocal counter
        counter += 1
        blocks = [_raw_to_block(item)] if item.bbox else []
        parent_path = [
            hierarchy[parent_level]
            for parent_level in sorted(hierarchy)
            if level <= 0 or parent_level < level
        ]
        clause = Clause(
            clause_id=f"{doc_type}-{counter}",
            doc_type=doc_type,
            level=level,
            number=number,
            title=title,
            text=text,
            source_page_index=item.page_index if doc_type == "word" else None,
            blocks=blocks,
            field_key=field_key,
            parent_path=parent_path,
        )
        if level > 0:
            for child_level in [value for value in hierarchy if value >= level]:
                hierarchy.pop(child_level, None)
            label = " ".join(value for value in (number, title) if value).strip()
            hierarchy[level] = label or text[:80]
        return clause

    for item in raw_items:
        norm = normalize_text(item.text)
        if not norm:
            continue

        if item.kind == "table":
            field_cells = _field_cells_from_table(item)
            if field_cells:
                for cell_item, prefix, field_key in field_cells:
                    body = _split_body(cell_item.text, prefix)
                    current = new_clause(
                        "", 0, field_key, body or cell_item.text, cell_item, field_key
                    )
                    clauses.append(current)
                continue
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
            comparison_norm = _comparison_text(norm, doc_type)
            num = detect_number(comparison_norm)
            if num:
                prefix, number, level = num
                body = _split_body(comparison_norm, prefix)
                section = detect_section_key(body)
                current = new_clause(
                    number,
                    level,
                    body or comparison_norm,
                    body or comparison_norm,
                    item,
                    section[1] if section else "",
                )
            else:
                section = detect_section_key(comparison_norm)
                current = new_clause(
                    "", item.heading_level or 1, comparison_norm, comparison_norm, item,
                    section[1] if section else "",
                )
            clauses.append(current)
            continue

        # paragraph:按行拆分(单个 block 可能含多行/多编号/多字段)
        # 优先级:编号 > 字段名 > 续入当前条款
        comparison_norm = _comparison_text(norm, doc_type)
        line_items = _split_lines_with_bbox(item, comparison_norm)
        for line_item in line_items:
            line = line_item.text
            evidence_item = line_item
            if comparison_norm != norm and len(line_items) == 1:
                evidence_item = _raw_evidence_item(line_item, norm)
            num = detect_number(line)
            if num:
                prefix, number, level = num
                body = _split_body(line, prefix)
                section = detect_section_key(body)
                current = new_clause(
                    number, level, body, body, evidence_item,
                    section[1] if section else "",
                )
                clauses.append(current)
                continue
            # 无编号:尝试键值字段识别(甲方:/地址:/日期 等)
            field = detect_field_key(line)
            if field:
                prefix, field_key = field
                body = _split_body(line, prefix)
                # 字段块:正文为冒号后的值,标题用归一化 field_key 便于阅读
                current = new_clause(
                    "", 0, field_key, body or line, evidence_item, field_key
                )
                clauses.append(current)
                continue
            section = detect_section_key(line)
            if section:
                title, field_key = section
                current = new_clause("", 1, title, line, evidence_item, field_key)
                clauses.append(current)
                continue
            if current is None:
                current = new_clause("", 0, "", line, evidence_item)
                clauses.append(current)
            else:
                sep = "\n" if current.text else ""
                current.text += sep + line
                if evidence_item.bbox:
                    current.blocks.append(_raw_to_block(evidence_item))

    return clauses


def _field_cells_from_table(
    item: RawItem,
) -> list[tuple[RawItem, str, str]]:
    """识别签字区键值表格，并按单元格拆为字段条款。

    只有所有非空单元格都以已知字段名开头、且至少两个字段时才生效，避免把
    普通产品明细表错误拆散。
    """
    if item.table is None:
        return []
    rows = [item.table.headers, *item.table.rows]
    parsed: list[tuple[RawItem, str, str]] = []
    nonempty = 0
    for row in rows:
        for cell in row:
            text = normalize_text(cell)
            if not text:
                continue
            nonempty += 1
            detected = detect_field_key(text)
            if detected is None:
                return []
            prefix, field_key = detected
            parsed.append((
                RawItem(
                    text=text,
                    kind="paragraph",
                    page_index=item.page_index,
                    bbox=list(item.bbox),
                ),
                prefix,
                field_key,
            ))
    return parsed if nonempty >= 2 else []
