"""高风险要素抽取与比对(§8.1)。

amount/date/ratio 抽取具体值;breach/jurisdiction/effective 检测关键词命中。
任一要素值集合变化即判 changed。

结构化表格(§一期增强):当条款含 TableStructure 时,按单元格维度抽取要素,
定位到具体行号+列名,实现"第3行金额列从 500000→50000"的精确告警。
"""
from __future__ import annotations

import re
from collections import defaultdict

from ..models import KeyElement, KeyElementKind, TableStructure
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


# —— 结构化表格要素(一期增强)——


def _extract_cell_values(cell: str) -> dict[str, list[str]]:
    """从单个单元格文本抽取要素值,返回 {kind: [values]}(复用正文正则)。"""
    res: dict[str, list[str]] = defaultdict(list)
    norm = normalize_text(cell)
    for pat in _AMOUNT:
        res["amount"].extend(_clean(m.group(0)) for m in pat.finditer(norm))
    for pat in _DATE:
        res["date"].extend(_clean(m.group(0)) for m in pat.finditer(norm))
    for pat in _RATIO:
        res["ratio"].extend(_clean(m.group(0)) for m in pat.finditer(norm))
    return {k: sorted(set(v)) for k, v in res.items() if v}


def extract_table_key_elements(
    tables: list[TableStructure],
) -> list[KeyElement]:
    """从结构化表格按单元格维度抽取高风险要素。

    每个命中要素的单元格生成一条 KeyElement,带 row_index + col_header 定位,
    便于报告精确指出"第X行Y列"的要素变化。同单元格内同 kind 多值合并为列表。
    """
    out: list[KeyElement] = []
    for table in tables:
        headers = table.headers
        for row_idx, row in enumerate(table.rows):
            for col_idx, cell in enumerate(row):
                col_header = headers[col_idx] if col_idx < len(headers) else ""
                vals = _extract_cell_values(cell)
                for kind, values in vals.items():
                    out.append(
                        KeyElement(
                            kind=kind,  # type: ignore[arg-type]
                            word_value=" | ".join(values),
                            pdf_value=" | ".join(values),
                            changed=False,
                            row_index=row_idx,
                            col_header=col_header,
                        )
                    )
    return out


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

