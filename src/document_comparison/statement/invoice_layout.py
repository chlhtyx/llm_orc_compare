"""金额统计 — 全电发票专用坐标版面解析器。

背景:全电发票(增值税电子普通/专用发票)的明细表会被 pymupdf ``find_tables`` 压扁
成纯文本单元格(详见 AGENTS.md「确定性优先」原则的反例)。但这类发票版式高度统一,
文本层 span 的 x/y 坐标稳定可靠,因此用**确定性坐标解析**重建明细表,完全不需要
LLM/OCR,可复现且免费。

本模块只做一件事:从 pymupdf page 的文本层 span 坐标,重建出发票明细区
``TableStructure``(项目名称/规格/单位/数量/单价/金额/税率/税额),并单独抽出
价税合计小写金额。产出的 TableStructure 喂给 ``amount_column.summarize_table``,
复用现有含税口径 + Decimal 求和链路。

判定与边界:
  - 仅对"全电发票"生效(含「电子发票/电⼦发票」+「价税合计」特征);非发票返回 None,
    由调用方回退到通用 pymupdf/OCR 路径。
  - 明细区 = 表头行下方 ~ 合计行上方;合计行(¥金额合计/¥税额合计)与价税合计
    (¥xxx.xx 小写)不进 TableStructure.rows(避免与数据行重复计入)。
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any

from ..models import TableStructure

logger = logging.getLogger(__name__)

# 视觉行归并:y0 相差小于此值视为同一行(发票行高约 13pt)
_Y_ROW_TOL = 5.0
# 表头候选:必须同时出现"金额"与"税"相关列(税额/税率),且同行
_HEADER_MONEY = "金额"
_HEADER_TAX = "税"


def looks_like_invoice(text: str) -> bool:
    """快速判定页面文本是否为全电发票(决定是否走本解析器)。

    同时含「电子发票/电⼦发票」与「价税合计」即判定为是。宽松特征匹配,避免误伤对帐单。
    """
    if not text:
        return False
    has_title = "电子发票" in text or "电⼦发票" in text or "增值税" in text
    has_total = "价税合计" in text
    return has_title and has_total


def parse_invoice_page(page: Any) -> tuple[TableStructure | None, float | None]:
    """解析单页全电发票,重建明细表 + 价税合计小写金额。

    Args:
        page: pymupdf page 对象(调用方保证有原生文本层)。

    Returns:
        (table, grand_total):table 为明细区 TableStructure(含表头 8 列 + 数据行,
        **不含**合计行/价税合计行);若页面不像发票或解析失败返回 (None, None)。
        grand_total 为价税合计小写金额(已剥离 ¥ 与空格),用于调用方核对。
    """
    spans = _collect_spans(page)
    if not spans:
        return None, None

    header_y = _find_header_row_y(spans)
    if header_y is None:
        logger.debug("invoice_layout: 表头行未识别,跳过")
        return None, None

    col_defs = _build_column_defs(spans, header_y)
    if not col_defs:
        return None, None

    # 明细区下界:首个含「¥」且 y > 表头的合计行(¥金额合计/¥税额合计所在行)
    detail_y_end = _find_total_row_y(spans, header_y)

    rows = _extract_detail_rows(spans, header_y, detail_y_end, col_defs)
    if not rows:
        logger.debug("invoice_layout: 明细区未抽到数据行,跳过")
        return None, None

    headers = [name for name, *_ in col_defs]
    table = TableStructure(headers=headers, rows=rows)
    grand_total = _extract_grand_total(spans, header_y)
    logger.info(
        "invoice_layout: 解析成功 列=%s 数据行=%d 价税合计=%s",
        headers, len(rows), grand_total,
    )
    return table, grand_total


# —— 内部工具 ——

def _collect_spans(page: Any) -> list[dict]:
    """从 page 提取所有非空 span,带归一化坐标。"""
    spans: list[dict] = []
    document = page.get_text("dict")
    for block in document.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "")
                if not text or not text.strip():
                    continue
                x0, y0, x1, y1 = span.get("bbox", [0, 0, 0, 0])
                spans.append({
                    "text": text,
                    "x0": float(x0), "y0": float(y0),
                    "x1": float(x1), "y1": float(y1),
                    "xc": (float(x0) + float(x1)) / 2,
                })
    return spans


def _norm(text: str) -> str:
    """表头/列名归一化:去全半角空格。"""
    return re.sub(r"[\s\u3000]+", "", text)


def _find_header_row_y(spans: list[dict]) -> float | None:
    """定位明细表头行 y0:同行同时含「金额」与「税」(税额/税率)。

    '金额' 与 '税额/税率' 必须在同一视觉行(y0 接近),排除页眉「国家税务总局」(只含税不含金额)。
    """
    money = [s for s in spans if _HEADER_MONEY in _norm(s["text"])]
    for ms in money:
        peers = [s for s in spans if abs(s["y0"] - ms["y0"]) < 4]
        if any(_HEADER_TAX in _norm(p["text"]) for p in peers):
            return ms["y0"]
    return None


def _build_column_defs(spans: list[dict], header_y: float) -> list[tuple[str, float, float, float]]:
    """表头行 span → 列定义 [(name, x0, x1, xc), ...] 按 x0 升序。"""
    header_row = [s for s in spans if abs(s["y0"] - header_y) < 4]
    defs = [(_norm(s["text"]), s["x0"], s["x1"], s["xc"]) for s in header_row]
    return sorted(defs, key=lambda c: c[1])


def _assign_col(col_defs, x0: float, x1: float) -> str:
    """span 的 [x0,x1] 归属到重叠最多的表头列;无重叠则取中心点最近列。"""
    best_name, best_overlap = None, -1.0
    for name, cx0, cx1, cxc in col_defs:
        overlap = max(0.0, min(x1, cx1) - max(x0, cx0))
        if overlap > best_overlap:
            best_overlap, best_name = overlap, name
    if best_overlap == 0.0:
        xc = (x0 + x1) / 2
        best_name = min(col_defs, key=lambda c: abs(xc - c[3]))[0]
    return best_name


def _find_total_row_y(spans: list[dict], header_y: float) -> float:
    """明细区下界:首个含「¥」的合计行(¥金额合计/¥税额合计)y0。

    这些 ¥ 金额带位于表头下方、价税合计上方,是明细区与合计区的分界。
    """
    candidates = [
        s["y0"] for s in spans
        if "¥" in s["text"] and s["y0"] > header_y + 20
    ]
    if not candidates:
        return float("inf")
    # ¥ 与数字可能由不同字体生成独立 span,数字 y0 会略高于 ¥。
    # 边界取整条视觉行的最上沿,否则数字会落入明细区而把合计重复累加。
    total_y = min(candidates)
    return min(s["y0"] for s in spans if abs(s["y0"] - total_y) < _Y_ROW_TOL)


def _extract_detail_rows(
    spans: list[dict],
    header_y: float,
    detail_y_end: float,
    col_defs,
) -> list[list[str]]:
    """提取明细区数据行:按 y 归并视觉行 → 按 x 归列 → 跨行名称续行合并 → 金额单元格补单位。"""
    headers_order = [name for name, *_ in col_defs]
    money_headers = {h for h in headers_order if _is_money_header(h)}

    detail = [s for s in spans if header_y + 8 < s["y0"] < detail_y_end]
    detail.sort(key=lambda s: (s["y0"], s["x0"]))

    # 1) 按视觉行分组
    visual_rows: list[dict] = []
    for s in detail:
        placed = False
        for vr in visual_rows:
            if abs(s["y0"] - vr["_y"]) < _Y_ROW_TOL:
                vr["spans"].append(s)
                placed = True
                break
        if not placed:
            visual_rows.append({"_y": s["y0"], "spans": [s]})

    # 2) 每行按列归并文本
    row_dicts: list[dict] = []
    for vr in visual_rows:
        by_col: dict[str, str] = {}
        for s in vr["spans"]:
            col = _assign_col(col_defs, s["x0"], s["x1"])
            by_col[col] = by_col.get(col, "") + s["text"]
        row_dicts.append({"_y": vr["_y"], "cols": by_col})

    # 3) 跨行名称续行合并:本行缺所有金额列(金额/税额/单价/数量都空) → 并入上一行
    #    (商品名跨行如"沃隆/每日纯坚果750g",续行只有项目名称/规格列有内容)
    merged: list[dict] = []
    for rd in row_dicts:
        cols = rd["cols"]
        has_money_col = any(bool(cols.get(h)) for h in money_headers)
        if merged and not has_money_col:
            for col, val in cols.items():
                prev = merged[-1]["cols"]
                prev[col] = prev.get(col, "") + val
        else:
            merged.append(rd)

    # 4) 只保留含金额数据的行;金额/税额列的纯数字补「元」单位以便确定性抽取
    rows: list[list[str]] = []
    for rd in merged:
        cols = rd["cols"]
        if not any(_is_money_cell(cols.get(h, "")) for h in headers_order):
            continue
        row: list[str] = []
        for h in headers_order:
            cell = cols.get(h, "")
            if h in money_headers:
                cell = _normalize_money_cell(cell)
            row.append(cell)
        rows.append(row)
    return rows


def _is_money_header(name: str) -> bool:
    """表头是否为金额类列(需补「元」单位的列)。

    发票明细的金额/税额/单价是纯数字无单位,坐标解析已确定它们是金额列,
    故补「元」使 elements._fact_spans 确定性抽取。数量/税率不是金额,不补。
    """
    return any(k in name for k in ("金额", "税额", "单价"))


def _is_money_cell(text: str) -> bool:
    """单元格是否为金额数字(纯数字/带¥/带千分位),排除百分比(税率)。"""
    if not text:
        return False
    if text.strip().endswith("%"):
        return False
    t = text.replace(",", "").replace("¥", "").replace(" ", "")
    return bool(re.search(r"\d", t))


def _normalize_money_cell(cell: str) -> str:
    """金额单元格规范化:纯数字(无单位/无¥)补「元」,使 elements._fact_spans 能确定性抽取。

    发票明细金额是纯数字无单位(如 '96.46'),_fact_spans 强制要求金额单位不命中。
    坐标解析已确定该列是金额列,故在此补单位,避免触发 LLM 兜底。带 ¥/元 的原样保留。
    """
    if not cell:
        return cell
    s = unicodedata.normalize("NFKC", cell).strip().replace("−", "-")
    if "¥" in s or "元" in s:
        return s
    # 含折扣/红字负数；符号和数字之间可能存在 PDF 排版空格。
    if re.fullmatch(r"[+-]?\s*[\d,]+(?:\.\d+)?", s):
        return re.sub(r"\s+", "", s) + "元"
    return s


def _extract_grand_total(spans: list[dict], header_y: float) -> float | None:
    """提取价税合计小写金额(¥xxx.xx,通常在「价税合计(大写)」行附近)。

    定位:含「¥」且 y 最大(发票最下方的价税合计小写),剥离 ¥/空格后解析为 float。
    """
    candidates = [s for s in spans if "¥" in s["text"] and s["y0"] > header_y + 20]
    if not candidates:
        return None
    # 价税合计小写通常是最靠下、且金额最大的 ¥ 项;取 y 最大的
    candidates.sort(key=lambda s: s["y0"])
    last = candidates[-1]
    cleaned = last["text"].replace("¥", "").replace(",", "").strip()
    if not cleaned:
        # 同一金额的货币符号和数字可能分片;仅拼接同行且紧邻右侧的数字,
        # 不从全页搜索金额,避免误取其它列或其它行的数值。
        adjacent = [
            s for s in spans
            if abs(s["y0"] - last["y0"]) < _Y_ROW_TOL
            and -0.5 <= s["x0"] - last["x1"] <= 5.0
            and re.fullmatch(r"[+-]?[\d,]+(?:\.\d+)?", s["text"].strip())
        ]
        if len(adjacent) != 1:
            return None
        cleaned = adjacent[0]["text"].replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None
