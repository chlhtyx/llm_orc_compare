"""金额统计 — 列定位 + 金额抽取 + 确定性求和(主逻辑,零 LLM)。

核心理念(AGENTS.md「确定性优先,LLM 辅助」):
  - 金额抽取复用 compare.elements._fact_spans(已处理金额/日期/比例重叠优先级,
    比单条正则更鲁棒)。封装在 extract_amounts_from_cell 一处,降低耦合面。
  - 求和用 Decimal 精确累加,避免浮点误差。
  - 合计行通过 is_total_row 识别,记入 declared_totals 不参与 column_sums(避免重复计入)。
  - 列定位失败 → 标 needs_review,不强行下结论;LLM 兜底列指认由 pipeline 调用方处理。
"""
from __future__ import annotations

import logging
import re
import unicodedata
from decimal import Decimal
from typing import Literal

from ..compare import elements as _elements
from ..models import (
    StatementAmountItem,
    StatementTableSummary,
    TableStructure,
)

logger = logging.getLogger(__name__)


# —— 启发式金额列关键词(按优先级排序,列表靠前优先级高)——
# 每个 tuple: (关键词正则, 角色标签)。列名匹配时取命中的最高优先级角色。
# 用正则而非子串:允许 "未付金额" 同时命中 unpaid 与 amount,取靠前的 unpaid。
#
# 含税金额统计(本通道目标):role 现在是**功能性**的,驱动含税合计口径:
#   - tax_inclusive: 价税合计/含税 → 优先用此列,排除 amount/tax(防重复计入)
#   - tax:          纯税额 → 无含税列时与 amount 列相加
#   - amount:       不含税金额 → 无含税列时与 tax 列相加;无税列时单独作含税合计
#   - paid/unpaid:  已付/未付 → 不进入含税合计(仅保留语义)
#   - total:        总计/小计列 → 行级合计行的列形态,只入 declared_totals 不入 column_sums
AMOUNT_COLUMN_KEYWORDS: list[tuple[str, str]] = [
    # 价税合计/含税 优先级必须最高:否则会被 total(含「合计」)抢匹配为 total
    (r"价税合计|含税金额|含税总价|含税|税价合计|价税", "tax_inclusive"),
    (r"合计|小计|总计|总额|总金额|合计金额", "total"),
    (r"税额|税款|税金", "tax"),
    (r"未付|应付|未结|待结|欠款", "unpaid"),
    (r"已付|实付|已结|已收|实收", "paid"),
    (r"金额|款额|数额", "amount"),
]

# 预编译(模块级常量,避免每张表重复编译);亦供 llm_amount_extract 复用。
AMOUNT_COLUMN_COMPILED: list[tuple[re.Pattern, str]] = [
    (re.compile(pat), role) for pat, role in AMOUNT_COLUMN_KEYWORDS
]

# 合计行识别:前若干列单元格含这些关键词即认为是合计行。
TOTAL_ROW_KEYWORDS: tuple[str, ...] = ("合计", "小计", "总计", "总额", "价税合计")

# 列定位方式标签(对齐 StatementTableSummary.column_source)
ColumnSource = Literal["heuristic", "llm", "none"]


def detect_amount_columns(
    headers: list[str],
    user_keywords: list[str] | None = None,
) -> dict[int, str]:
    """启发式匹配列名 → 返回 {col_index: column_role}。

    匹配规则:列名(去空白后)包含任一关键词即命中,取最高优先级角色。
    user_keywords 非空时,所有列用 "amount" 角色匹配用户给的关键词(覆盖默认表)。
    """
    result: dict[int, str] = {}
    if not headers:
        return result

    if user_keywords:
        # 用户自定义关键词:统一标 "amount",不区分 paid/unpaid/total
        patterns = [re.compile(kw) for kw in user_keywords if kw and kw.strip()]
        for idx, raw in enumerate(headers):
            name = _normalize_header(raw)
            if not name:
                continue
            if any(p.search(name) for p in patterns):
                result[idx] = "amount"
        return result

    for idx, raw in enumerate(headers):
        name = _normalize_header(raw)
        if not name:
            continue
        # 按优先级遍历,命中即取
        for pattern, role in AMOUNT_COLUMN_COMPILED:
            if pattern.search(name):
                result[idx] = role
                break
    return result


def extract_amounts_from_cell(cell_text: str) -> list[tuple[str, float]]:
    """从单个单元格文本抽取金额值。

    复用 compare.elements._fact_spans 的扫描逻辑(已处理金额/日期/比例的重叠优先级
    与中文/阿拉伯/¥ 三类金额格式),返回 [(canonical, value), ...](单位元)。

    注:_fact_spans 为 elements 模块内部函数,此处封装为对外语义稳定的公开入口;
    若未来 elements 重构,只影响本函数这一处。
    """
    if not cell_text:
        return []
    text = unicodedata.normalize("NFKC", cell_text).replace("−", "-")
    # 通用事实扫描器只抽取无符号金额。金额统计在此保留紧邻金额的负号，
    # 并把 ¥-100 统一为 -¥100，使带货币符号、无「元」的负数也能被扫描。
    text = re.sub(r"([¥￥])\s*([+-])\s*(?=\d)", r"\2\1", text)
    spans = _elements._fact_spans(text)
    out: list[tuple[str, float]] = []
    for span in spans:
        if span.kind != "amount":
            continue
        # canonical 形如 "CNY:30000" 或 "CNY:30000.5"
        canonical = span.value
        if not canonical.startswith("CNY:"):
            continue
        try:
            amount = Decimal(canonical[len("CNY:"):])
            if re.search(r"(?<![0-9A-Za-z.])-\s*$", text[:span.start]):
                amount = -abs(amount)
                canonical = f"CNY:{_elements._decimal_text(amount)}"
            value = float(amount)
        except Exception:  # noqa: BLE001
            continue
        out.append((canonical, value))
    return out


def is_total_row(row: list[str], headers: list[str]) -> bool:
    """识别合计行:前若干列单元格含 TOTAL_ROW_KEYWORDS。

    合计行不参与 column_sums 求和(避免与数据行重复计入),
    其金额单元格作为 declared_totals 供核对。
    """
    # 检查前 3 列(对帐单合计行的标签通常在首列,但合并单元格可能在前几列)
    check_cells = [str(c or "").strip() for c in row[:3]] if row else []
    for cell in check_cells:
        if not cell:
            continue
        if any(kw in cell for kw in TOTAL_ROW_KEYWORDS):
            return True
    return False


def summarize_table(
    table: TableStructure,
    *,
    file_index: int,
    file_name: str,
    table_index: int,
    page_index: int,
    user_keywords: list[str] | None = None,
    column_source_override: dict[int, ColumnSource] | None = None,
    column_roles_override: dict[int, str] | None = None,
) -> StatementTableSummary:
    """对单张表执行金额列定位 + 求和 + 声明核对。

    column_roles_override:LLM 兜底成功时由 pipeline 传入 {col_index: role},
    直接覆盖启发式结果,强制按 LLM 给的列索引抽取金额。
    column_source_override:同上,标识这些列是 LLM 指认的(影响 column_source 标记)。
    未传任一 override 时,列定位走启发式 detect_amount_columns,column_source 全标 "heuristic"。
    """
    headers = list(table.headers)
    if column_roles_override is not None:
        column_roles = dict(column_roles_override)
    else:
        column_roles = detect_amount_columns(headers, user_keywords=user_keywords)

    column_source: dict[str, ColumnSource] = {}
    for col_idx in column_roles:
        col_name = _col_name(headers, col_idx)
        if column_source_override and col_idx in column_source_override:
            column_source[col_name] = column_source_override[col_idx]
        else:
            column_source[col_name] = "heuristic"

    column_sums: dict[str, Decimal] = {}
    declared_totals: dict[str, Decimal] = {}
    totals_match: dict[str, bool] = {}
    items: list[StatementAmountItem] = []
    skipped_rows: list[int] = []

    for row_idx, row in enumerate(table.rows):
        is_total = is_total_row(row, headers)
        if is_total:
            skipped_rows.append(row_idx)
        row_label = row[0] if row else ""

        for col_idx, role_col in column_roles.items():
            if col_idx >= len(row):
                continue
            cell = row[col_idx]
            col_name = _col_name(headers, col_idx)
            amounts = extract_amounts_from_cell(str(cell or ""))
            if not amounts:
                continue
            # 一个单元格可能有多个金额(如 "原币 100 / 本币 700");取第一个用于求和,
            # 多金额单元格的歧义由 needs_review 语义兜底(此处保持简单确定)。
            canonical, value = amounts[0]
            if is_total:
                # 合计行金额 → declared_totals(用最大值,避免多金额重复)
                amount = Decimal(str(value))
                prev = declared_totals.get(col_name)
                declared_totals[col_name] = amount if prev is None else max(prev, amount)
            else:
                key = col_name
                column_sums[key] = column_sums.get(key, Decimal("0")) + Decimal(str(value))
                items.append(
                    StatementAmountItem(
                        file_index=file_index,
                        file_name=file_name,
                        table_index=table_index,
                        page_index=page_index,
                        row_index=row_idx,
                        row_label=str(row_label or "")[:80],
                        column=col_name,
                        raw_cell=str(cell or "")[:200],
                        canonical=canonical,
                        value=value,
                    )
                )

    # 核对:同列存在 column_sum 与 declared_total 才比较
    for col_name in set(column_sums) & set(declared_totals):
        # 容忍 0.01 元误差(四舍五入差异)
        diff = abs(column_sums[col_name] - declared_totals[col_name])
        totals_match[col_name] = diff <= Decimal("0.01")

    # —— 含税金额合计(确定性,Decimal;LLM/代码均不做求和幻觉,此处是代码确定性算术)——
    # 按列角色分组:role → 命中列的列名集合
    cols_by_role: dict[str, list[str]] = {}
    for col_idx, role_col in column_roles.items():
        cols_by_role.setdefault(role_col, []).append(_col_name(headers, col_idx))

    def _sum_cols(names: list[str]) -> Decimal:
        total = Decimal("0")
        for n in names:
            total += column_sums.get(n, Decimal("0"))
        return total

    tax_inclusive_total = Decimal("0")
    tax_inclusive_method = ""
    tax_incl_cols = cols_by_role.get("tax_inclusive", [])
    amount_cols = cols_by_role.get("amount", [])
    tax_cols = cols_by_role.get("tax", [])

    if any(col in column_sums for col in tax_incl_cols):
        # Case 1:有含税/价税合计列 → 只用它,排除 amount/tax(防重复计入)
        tax_inclusive_total = _sum_cols(tax_incl_cols)
        tax_inclusive_method = "含税/价税合计列"
    elif any(col in column_sums for col in amount_cols) and any(
        col in column_sums for col in tax_cols
    ):
        # Case 2:金额(不含税)和税额都确实抽到 → 跨列相加
        tax_inclusive_total = _sum_cols(amount_cols) + _sum_cols(tax_cols)
        tax_inclusive_method = "金额(不含税)列 + 税额列"
    elif amount_cols and not tax_cols and any(col in column_sums for col in amount_cols):
        # Case 3:只有金额列,无税额列 → 照旧求和(语义上视作含税)
        tax_inclusive_total = _sum_cols(amount_cols)
        tax_inclusive_method = "金额列"
    # 否则(只有 paid/unpaid/total 列)→ 含税合计为 0,口径为空(调用方/前端据此标 needs_review 或略过)

    return StatementTableSummary(
        file_index=file_index,
        file_name=file_name,
        table_index=table_index,
        page_index=page_index,
        headers=headers,
        column_sums={k: float(v) for k, v in column_sums.items()},
        declared_totals={k: float(v) for k, v in declared_totals.items()},
        totals_match=totals_match,
        items=items,
        skipped_rows=skipped_rows,
        column_source=column_source,
        tax_inclusive_total=float(tax_inclusive_total),
        tax_inclusive_method=tax_inclusive_method,
    )


def is_cross_page_continuation(prev: TableStructure, curr: TableStructure) -> bool:
    """跨页续表判定:headers 集合相同(或子集)且前表无合计行。

    用于把跨页的同一张表合并成单逻辑表,避免重复计算合计行 / 重复列定位。
    """
    prev_headers = {_normalize_header(h) for h in prev.headers if _normalize_header(h)}
    curr_headers = {_normalize_header(h) for h in curr.headers if _normalize_header(h)}
    if not prev_headers or not curr_headers:
        return False
    # headers 集合相同或当前是前表的子集,且前表没有合计行(有合计行说明前表已结束)
    if curr_headers.issubset(prev_headers) or curr_headers == prev_headers:
        prev_has_total = any(is_total_row(row, prev.headers) for row in prev.rows)
        return not prev_has_total
    return False


def merge_cross_page_tables(
    tables_with_page: list[tuple[TableStructure, int]],
) -> list[tuple[TableStructure, int]]:
    """合并跨页续表为单逻辑表。

    输入:[(table, page_index), ...] 按文档顺序。
    输出:合并后的 [(table, start_page_index), ...]。
    合并策略:相邻且 is_cross_page_continuation 为真 → rows 累加,headers 取前者。
    """
    if not tables_with_page:
        return []
    merged: list[tuple[TableStructure, int]] = []
    current_table, current_page = tables_with_page[0]
    current_rows = list(current_table.rows)

    for table, page in tables_with_page[1:]:
        if is_cross_page_continuation(current_table, table):
            # 跨页续表:累加 rows(跳过续表的表头行,如果它重复了表头)
            new_rows = table.rows
            if new_rows and _rows_look_like_header_repeat(new_rows[0], current_table.headers):
                new_rows = new_rows[1:]
            current_rows.extend(new_rows)
        else:
            merged.append(
                (TableStructure(headers=current_table.headers, rows=current_rows), current_page)
            )
            current_table, current_page = table, page
            current_rows = list(table.rows)

    merged.append(
        (TableStructure(headers=current_table.headers, rows=current_rows), current_page)
    )
    return merged


# —— 内部工具 ——

def _col_name(headers: list[str], idx: int) -> str:
    """列名取值:越界时回退为 f"col{idx}",避免索引异常。"""
    return headers[idx] if idx < len(headers) else f"col{idx}"


def _normalize_header(raw: str) -> str:
    """列名归一化:去空白、统一全角/半角,便于关键词匹配。"""
    if not raw:
        return ""
    return re.sub(r"\s+", "", str(raw)).strip()


def _rows_look_like_header_repeat(row: list[str], headers: list[str]) -> bool:
    """判断某行是否是表头重复(跨页续表时常重复一次表头)。"""
    if not row or not headers:
        return False
    norm_row = [_normalize_header(c) for c in row]
    norm_headers = [_normalize_header(h) for h in headers]
    overlap = sum(1 for cell in norm_row if cell and cell in norm_headers)
    # 多数单元格与表头重合即认为是表头重复
    return overlap >= max(1, len(norm_headers) // 2)
