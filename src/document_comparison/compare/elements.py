"""高风险要素抽取与比对(§8.1)。

amount/date/ratio 抽取具体值;breach/jurisdiction/effective 检测关键词命中。
任一要素值集合变化即判 changed。

结构化表格(§一期增强):当条款含 TableStructure 时,按单元格维度抽取要素,
定位到具体行号+列名,实现"第3行金额列从 500000→50000"的精确告警。
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

from ..models import KeyElement, KeyElementKind, TableStructure
from ..structure.normalize import normalize_text

_ARABIC_AMOUNT_RE = re.compile(
    r"(?<![0-9A-Za-z.])"
    r"(?P<number>\d(?:[\d,， ]*\d)?(?: *\. *\d+)?) *"
    r"(?P<unit>万元?|元)"
)
_SYMBOL_AMOUNT_RE = re.compile(
    r"[¥￥] *(?P<number>\d(?:[\d,， ]*\d)?(?: *\. *\d+)?) *"
    r"(?P<unit>万元?|元)?"
)
_CN_AMOUNT_RE = re.compile(
    r"(?P<number>[零〇一壹二贰两三叁四肆五伍六陆七柒八捌九玖十拾百佰千仟万亿整]+)"
    r"(?P<unit>万元?|元|圆)"
)
_CN_DATE_RE = re.compile(
    r"(?P<year>\d{4})\s*年\s*(?P<month>\d{1,2})\s*月"
    r"(?:\s*(?P<day>\d{1,2})\s*日)?"
)
_ISO_DATE_RE = re.compile(
    r"(?<!\d)(?P<year>\d{4})\s*[-/]\s*(?P<month>\d{1,2})\s*[-/]\s*"
    r"(?P<day>\d{1,2})(?!\d)"
)
_DURATION_RE = re.compile(
    r"(?<!\d)(?P<number>\d+)\s*个?\s*(?P<unit>工作日|天|日|个月|月)(?!\d)"
)
_NUMERIC_RATIO_RE = re.compile(r"(?<!\d)(?P<number>\d+(?:\.\d+)?)\s*%")
_CN_RATIO_RE = re.compile(r"百分之(?P<number>[零〇一二两三四五六七八九十百\d]+)")

_ACCOUNT_RE = re.compile(
    r"(?:收款账号|付款账号|银行账号|银行账户|收款账户|付款账户|账号|帐号)"
    r"\s*[:：为]?\s*(?P<value>\d[\d\s-]{7,29})"
)
_IDENTIFIER_RE = re.compile(
    r"(?:统一社会信用代码|身份证号)\s*[:：为]?\s*"
    r"(?P<value>[0-9A-Za-z][0-9A-Za-z\s-]{7,24})"
)
_DOCUMENT_NUMBER_RE = re.compile(
    r"(?:合同编号|申购单编号|订单编号|项目编号)\s*[:：为]?\s*"
    r"(?P<value>[0-9A-Za-z][0-9A-Za-z_-]{3,39})"
)
_SPACED_DOCUMENT_NUMBER_RE = re.compile(
    r"(?:合同编号|申购单编号|订单编号|项目编号)\s*[:：为]?\s*"
    r"(?P<value>"
    r"(?=[0-9A-Za-z_ -]{4,40}(?:[^0-9A-Za-z_ -]|$))"
    r"(?=[0-9A-Za-z_ -]*\d)"
    r"[0-9A-Za-z][0-9A-Za-z_ -]{2,38}[0-9A-Za-z]"
    r")"
)
_KEYWORDS: dict[str, list[str]] = {
    "breach": ["违约", "赔偿", "滞纳金", "罚息"],
    "jurisdiction": ["诉讼", "仲裁", "管辖", "法院"],
    "effective": ["生效", "解除", "终止"],
}

_PARTIES = [
    "甲方", "乙方", "供方", "需方", "出租方", "承租方", "买方", "卖方",
    "发包方", "承包方", "许可方", "被许可方", "委托方", "受托方",
    "转让方", "受让方",
]
_NEGATIONS = ["不得", "无权", "无需", "免于", "禁止", "不承担", "不予", "除外"]

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "贰": 2, "两": 2,
    "三": 3, "叁": 3, "四": 4, "肆": 4, "五": 5, "伍": 5,
    "六": 6, "陆": 6, "七": 7, "柒": 7, "八": 8, "捌": 8,
    "九": 9, "玖": 9,
}
_CN_SMALL_UNITS = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
_CN_LARGE_UNITS = {"万": 10_000, "亿": 100_000_000}


# 只统一确定性的字形变体，不删除标点。句末标点缺失等差异会在裁决层进入待复核。
_SAFE_PUNCTUATION_TRANSLATION = str.maketrans({
    "“": '"',
    "”": '"',
    "„": '"',
    "‟": '"',
    "‘": "'",
    "’": "'",
    "‚": "'",
    "‛": "'",
    "∶": ":",
    "꞉": ":",
    "。": ".",
})

# OCR 容易丢失或混淆、但不能在零容忍模式下直接忽略的低证据标点。
# 负号、百分号、括号、斜杠不在此集合；集合内标点位于数字之间时也会保留。
_REVIEWABLE_PUNCTUATION = frozenset(".,!?、:;'\"")


@dataclass(frozen=True)
class _FactSpan:
    start: int
    end: int
    kind: str
    value: str
    priority: int


def _decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _parse_decimal(raw: str) -> Decimal | None:
    try:
        return Decimal(re.sub(r"[\s,，]+", "", raw))
    except InvalidOperation:
        return None


def _parse_cn_integer(raw: str) -> int | None:
    raw = raw.replace("整", "")
    if not raw:
        return None
    total = 0
    section = 0
    number = 0
    seen = False
    for char in raw:
        if char in _CN_DIGITS:
            number = _CN_DIGITS[char]
            seen = True
        elif char in _CN_SMALL_UNITS:
            section += (number or 1) * _CN_SMALL_UNITS[char]
            number = 0
            seen = True
        elif char in _CN_LARGE_UNITS:
            section += number
            total += (section or 1) * _CN_LARGE_UNITS[char]
            section = 0
            number = 0
            seen = True
        else:
            return None
    return total + section + number if seen else None


def _canonical_amount(number: Decimal, unit: str) -> str:
    if unit.startswith("万"):
        number *= Decimal(10_000)
    return f"CNY:{_decimal_text(number)}"


def _canonical_date(year: str, month: str, day: str | None) -> str | None:
    try:
        if day is None:
            month_value = int(month)
            if not 1 <= month_value <= 12:
                return None
            return f"{int(year):04d}-{month_value:02d}"
        value = date(int(year), int(month), int(day))
        return value.isoformat()
    except ValueError:
        return None


def _identifier_value(raw: str) -> str:
    return re.sub(r"[\s-]+", "", raw).upper()


def _candidate_fact_spans(text: str) -> list[_FactSpan]:
    spans: list[_FactSpan] = []

    for match in _CN_DATE_RE.finditer(text):
        value = _canonical_date(match["year"], match["month"], match["day"])
        if value:
            spans.append(_FactSpan(match.start(), match.end(), "date", value, 100))
    for match in _ISO_DATE_RE.finditer(text):
        value = _canonical_date(match["year"], match["month"], match["day"])
        if value:
            spans.append(_FactSpan(match.start(), match.end(), "date", value, 100))

    for regex, priority in ((_SYMBOL_AMOUNT_RE, 80), (_ARABIC_AMOUNT_RE, 70)):
        for match in regex.finditer(text):
            number = _parse_decimal(match["number"])
            if number is None:
                continue
            spans.append(_FactSpan(
                match.start(), match.end(), "amount",
                _canonical_amount(number, match["unit"] or "元"), priority,
            ))
    for match in _CN_AMOUNT_RE.finditer(text):
        raw_number = match["number"]
        # 避免把阿拉伯金额末尾的“万元”再次识别成中文数字“万”+“元”。
        if not any(char in _CN_DIGITS or char in _CN_SMALL_UNITS for char in raw_number):
            continue
        number = _parse_cn_integer(raw_number)
        if number is None:
            continue
        spans.append(_FactSpan(
            match.start(), match.end(), "amount",
            _canonical_amount(Decimal(number), match["unit"]), 70,
        ))

    for match in _DURATION_RE.finditer(text):
        spans.append(_FactSpan(
            match.start(), match.end(), "date",
            f"duration:{int(match['number'])}:{match['unit']}", 30,
        ))
    for match in _NUMERIC_RATIO_RE.finditer(text):
        number = _parse_decimal(match["number"])
        if number is not None:
            spans.append(_FactSpan(
                match.start(), match.end(), "ratio",
                f"{_decimal_text(number)}%", 50,
            ))
    for match in _CN_RATIO_RE.finditer(text):
        raw = match["number"]
        number = int(raw) if raw.isdigit() else _parse_cn_integer(raw)
        if number is not None:
            spans.append(_FactSpan(
                match.start(), match.end(), "ratio", f"{number}%", 50,
            ))

    for regex, kind in (
        (_ACCOUNT_RE, "account"),
        (_IDENTIFIER_RE, "identifier"),
        (_SPACED_DOCUMENT_NUMBER_RE, "identifier"),
        (_DOCUMENT_NUMBER_RE, "identifier"),
    ):
        for match in regex.finditer(text):
            spans.append(_FactSpan(
                match.start(), match.end(), kind,
                _identifier_value(match["value"]), 60,
            ))
    return spans


def _select_non_overlapping(spans: list[_FactSpan]) -> list[_FactSpan]:
    selected: list[_FactSpan] = []
    occupied: list[tuple[int, int]] = []
    for span in sorted(
        spans,
        key=lambda item: (-item.priority, -(item.end - item.start), item.start),
    ):
        if any(span.start < end and start < span.end for start, end in occupied):
            continue
        selected.append(span)
        occupied.append((span.start, span.end))
    return sorted(selected, key=lambda item: item.start)


def _fact_spans(text: str) -> list[_FactSpan]:
    return _select_non_overlapping(_candidate_fact_spans(text))


def _is_east_asian_word_char(char: str) -> bool:
    """中文/日文/韩文等宽字符间空格通常来自排版或 OCR 分字。"""
    return (
        bool(char)
        and char.isalnum()
        and unicodedata.east_asian_width(char) in {"W", "F"}
    )


def _normalize_layout_whitespace(text: str) -> str:
    """消除安全排版空格，同时保留英文单词及普通数字之间的分隔。"""
    if not text:
        return ""

    def replace(match: re.Match[str]) -> str:
        left = text[match.start() - 1] if match.start() else ""
        right = text[match.end()] if match.end() < len(text) else ""
        left_word = left.isalnum() or left == "_"
        right_word = right.isalnum() or right == "_"
        if (
            left_word
            and right_word
            and not _is_east_asian_word_char(left)
            and not _is_east_asian_word_char(right)
        ):
            return " "
        return ""

    return re.sub(r"\s+", replace, text)


def _canonical_base_text(text: str) -> str:
    # 先消除 CJK 字间空格(账 号→账号),否则多字关键词正则(账号/统一社会信用代码)
    # 会在正则扫描阶段失配,导致两端抽取结果不一致并误报"高风险要素变更"。
    return _normalize_layout_whitespace(normalize_text(text)).translate(
        _SAFE_PUNCTUATION_TRANSLATION
    )


def canonicalize_contract_text(text: str) -> str:
    """生成零容忍比对键，仅消除安全排版差异和确定性事实表示差异。"""
    norm = _canonical_base_text(text)
    spans = _fact_spans(norm)
    parts: list[str] = []
    cursor = 0
    for span in spans:
        parts.append(norm[cursor:span.start])
        parts.append(f"<{span.kind}:{span.value}>")
        cursor = span.end
    parts.append(norm[cursor:])
    return _normalize_layout_whitespace("".join(parts))


def _without_reviewable_punctuation(text: str) -> str:
    kept: list[str] = []
    for index, char in enumerate(text):
        if char not in _REVIEWABLE_PUNCTUATION:
            kept.append(char)
            continue
        previous = text[index - 1] if index else ""
        following = text[index + 1] if index + 1 < len(text) else ""
        if previous.isdigit() and following.isdigit():
            kept.append(char)
    return "".join(kept)


def reviewable_formatting_change(
    word_text: str,
    pdf_text: str,
) -> Literal["spacing", "punctuation"] | None:
    """识别只能降为人工复核、不能直接判 clean 的空格/标点差异。"""
    word = canonicalize_contract_text(word_text)
    pdf = canonicalize_contract_text(pdf_text)
    if word == pdf:
        return None

    compact_word = re.sub(r"\s+", "", word)
    compact_pdf = re.sub(r"\s+", "", pdf)
    if compact_word == compact_pdf:
        return "spacing"
    if _without_reviewable_punctuation(compact_word) == _without_reviewable_punctuation(
        compact_pdf
    ):
        return "punctuation"
    return None


def extract_key_elements(text: str) -> dict[str, list[str]]:
    """返回 canonical {kind: [values]}，避免表示格式差异触发误报。"""
    res: dict[str, list[str]] = defaultdict(list)
    # 同 _canonical_base_text:先消 CJK 字间空格,避免 OCR 分字导致关键词正则失配。
    norm = _normalize_layout_whitespace(normalize_text(text))
    for span in _fact_spans(norm):
        res[span.kind].append(span.value)
    for kind, kws in _KEYWORDS.items():
        res[kind].extend(kw for kw in kws if kw in norm)
    res["party"].extend(party for party in _PARTIES if party in norm)
    res["negation"].extend(token for token in _NEGATIONS if token in norm)
    return {k: sorted(set(v)) for k, v in res.items() if v}


def elements_changed(
    word_elems: dict[str, list[str]], pdf_elems: dict[str, list[str]]
) -> list[KeyElement]:
    """比对两端要素,产出 KeyElement 列表(含 changed 标记)。"""
    out: list[KeyElement] = []
    for kind in sorted(set(word_elems) | set(pdf_elems)):
        wv = sorted(set(word_elems.get(kind, [])))
        pv = sorted(set(pdf_elems.get(kind, [])))
        out.append(
            KeyElement(
                kind=kind,  # type: ignore[arg-type]
                word_value=" | ".join(wv),
                pdf_value=" | ".join(pv),
                changed=wv != pv,
            )
        )
    return out



# —— 结构化表格要素(一期增强)——


def _extract_cell_values(cell: str) -> dict[str, list[str]]:
    """从单个单元格文本抽取要素值,返回 {kind: [values]}(复用正文正则)。"""
    extracted = extract_key_elements(cell)
    fact_kinds = {"amount", "date", "ratio", "account", "identifier"}
    return {kind: values for kind, values in extracted.items() if kind in fact_kinds}



def table_elements_changed(
    word_tables: list[TableStructure],
    pdf_tables: list[TableStructure],
) -> list[KeyElement]:
    """比对两端结构化表格的单元格要素,标记 changed。

    配对策略:按表格序号 + 行号 + 列号对齐两端单元格,比较同位置的要素值。
    结构不对称时(行/列数不一致),仅比较能对齐的部分,其余跳过——
    结构性差异已由 table_diff 体现,此处聚焦值篡改。
    """
    out: list[KeyElement] = []
    for ti in range(min(len(word_tables), len(pdf_tables))):
        wt = word_tables[ti]
        pt = pdf_tables[ti]
        headers = wt.headers if len(wt.headers) >= len(pt.headers) else pt.headers
        for ri in range(min(len(wt.rows), len(pt.rows))):
            wrow = wt.rows[ri]
            prow = pt.rows[ri]
            for ci in range(min(len(wrow), len(prow))):
                col_header = headers[ci] if ci < len(headers) else ""
                wv = _extract_cell_values(wrow[ci])
                pv = _extract_cell_values(prow[ci])
                kinds = sorted(set(wv) | set(pv))
                for kind in kinds:
                    w_vals = sorted(set(wv.get(kind, [])))
                    p_vals = sorted(set(pv.get(kind, [])))
                    out.append(
                        KeyElement(
                            kind=kind,  # type: ignore[arg-type]
                            word_value=" | ".join(w_vals),
                            pdf_value=" | ".join(p_vals),
                            changed=w_vals != p_vals,
                            row_index=ri,
                            col_header=col_header,
                        )
                    )
    return out
