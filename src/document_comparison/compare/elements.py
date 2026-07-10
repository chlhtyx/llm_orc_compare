"""高风险要素抽取与比对(§8.1)。

amount/date/ratio 抽取具体值;breach/jurisdiction/effective 检测关键词命中。
任一要素值集合变化即判 changed。
"""
from __future__ import annotations

import re
from collections import defaultdict

from ..models import KeyElement, KeyElementKind
from ..structure.normalize import normalize_text

_AMOUNT = [
    re.compile(r"[¥￥]\s*[\d,，.]+\s*(?:万元?|元)?"),
    re.compile(r"[\d,，.]+\s*(?:万元|元)"),
    re.compile(r"[壹贰叁肆伍陆柒捌玖拾佰仟万亿零整圆]{2,}元?"),
]
_DATE = [
    re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?"),
    re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}"),
    re.compile(r"\d+\s*个?\s*(?:工作日|天|日|个月|月)"),
]
_RATIO = [
    re.compile(r"\d+(?:\.\d+)?\s*%"),
    re.compile(r"百分之[一二三四五六七八九十百\d]+"),
]
_KEYWORDS: dict[str, list[str]] = {
    "breach": ["违约", "赔偿", "滞纳金", "罚息"],
    "jurisdiction": ["诉讼", "仲裁", "管辖", "法院"],
    "effective": ["生效", "解除", "终止"],
}

# 金额/日期为「极高」风险,其余高(§8.1)
_SEVERE_KINDS = {"amount", "date"}

# 去除要素值内所有空白,使 "2026 年 07 月 09 日" == "2026年07月09日"
_WS_RE = re.compile(r"\s+")


def _clean(val: str) -> str:
    return _WS_RE.sub("", val)


def extract_key_elements(text: str) -> dict[str, list[str]]:
    """返回 {kind: [values]}。amount/date/ratio 为具体值;其余为命中关键词。"""
    res: dict[str, list[str]] = defaultdict(list)
    norm = normalize_text(text)
    for pat in _AMOUNT:
        res["amount"].extend(_clean(m.group(0)) for m in pat.finditer(norm))
    for pat in _DATE:
        res["date"].extend(_clean(m.group(0)) for m in pat.finditer(norm))
    for pat in _RATIO:
        res["ratio"].extend(_clean(m.group(0)) for m in pat.finditer(norm))
    for kind, kws in _KEYWORDS.items():
        res[kind].extend(kw for kw in kws if kw in norm)
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


def severe_changes(elems: list[KeyElement]) -> list[KeyElementKind]:
    """返回发生变化的「极高」要素类别(金额/日期)。"""
    return [e.kind for e in elems if e.changed and e.kind in _SEVERE_KINDS]
