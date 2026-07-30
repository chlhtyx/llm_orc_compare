"""对帐单/发票金额统计 — LLM 兜底金额抽取。

仅在「启发式列定位 + LLM 列指认」都未能抽出金额时调用,典型场景是发票:
单元格里的金额是纯数字无单位(如 "1680.00"),compare.elements 的金额正则
(强制要求 元/万元/¥/中文大写)全部不命中,导致代码抽取为空。

严格护栏(贯彻 AGENTS.md「确定性优先,LLM 辅助;不可让 LLM 做算术」):
  - LLM **只抽数据行的金额数值**,不做求和、不做任何计算。
  - 抽出的每个金额必须通过 **grounding 校验**:在 OCR 识别出的页面文本里能逐字
    溯源(数字归一化后子串包含),否则丢弃。这把"模型幻觉"挡在确定性结论之外。
  - 求和仍由代码完成(调用方把本函数返回的 items 累加进 column_sums)。
  - 合计行的金额不在此抽取(合计行已在 amount_column.is_total_row 处理为 declared_totals)。
"""
from __future__ import annotations

import json
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from ..compare import elements as _elements
from ..models import StatementAmountItem
from ..observability import log_value_summary, model_call_context

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "你是发票/收据/对账单的金额抽取助手。本任务只统计【含税金额】。"
    "任务:从给定表格中抽取【含税金额】类列(价税合计/含税/含税金额)里"
    "每个数据行的金额数值。只返回 JSON,不做任何求和或计算。"
    "若表中同时存在「金额(不含税)」列与「税额」列,只抽含税金额列,"
    "不要抽这两列,避免与含税金额重复计入。"
    "不要抽数量、单价、税率、百分比、日期、行号、编号。"
)

_USER_PROMPT_TEMPLATE = (
    "表格表头如下:\n{headers_block}\n\n"
    "表格数据(每行用 | 分隔):\n{rows_block}\n\n"
    "下方是本页 OCR 识别出的原文,你返回的每个金额必须能在其中逐字找到:\n"
    "---BEGIN GROUNDING---\n{grounding}\n---END GROUNDING---\n\n"
    "请只返回 JSON,格式为 {{\"items\": [{{\"amount\": \"1680.00\"}}]}}。"
    "amount 为含税金额数字字符串(可含千分位/小数,不要带 ¥/元 等符号)。"
    "只抽含税/价税合计列的数据行金额,跳过合计/小计行;"
    "若同表有金额(不含税)列与税额列,不要抽它们(会与含税金额重复);"
    "若没有任何含税金额,返回 {{\"items\": []}}。"
)


def llm_extract_amounts(
    png_bytes: bytes,
    headers: list[str],
    rows: list[list[str]],
    grounding_text: str,
    *,
    file_index: int = 0,
    file_name: str = "",
    table_index: int = 0,
    page_index: int = 0,
    trace_context: dict[str, Any] | None = None,
) -> list[StatementAmountItem] | None:
    """列指认仍抽空时,让多模态 LLM 直接抽取数据行金额(发票纯数字场景)。

    Args:
        png_bytes: 表格所在页 PNG(视觉上下文,辅助列对齐)。
        headers: 表头列表。
        rows: 该表数据行(让模型聚焦目标表,而非整页)。
        grounding_text: 该页 OCR 文本(所有 block.content 拼接),用作逐字溯源。

    Returns:
        通过 grounding 校验的 ``StatementAmountItem`` 列表;调用/解析失败返回 None
        (调用方标 needs_review);空 list 表示无金额(调用方据此标 none)。
    """
    if not png_bytes:
        return None
    if not rows:
        return None

    # 延迟导入,避免 statement 包在 OCR 引擎未配置时仍可被单元测试 import
    from ..ocr.llm import LLMOCREngine, _to_data_url  # type: ignore

    engine = LLMOCREngine()
    data_url = _to_data_url(png_bytes)
    headers_block = _format_headers(headers)
    rows_block = _format_rows(headers, rows)

    payload: dict[str, Any] = {
        "model": engine.model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {
                        "type": "text",
                        "text": _USER_PROMPT_TEMPLATE.format(
                            headers_block=headers_block,
                            rows_block=rows_block,
                            grounding=grounding_text,
                        ),
                    },
                ],
            },
        ],
        "temperature": 0,
        # 关闭 Qwen3 思考链 token(确定性抽取,避免拖慢;非 Qwen3 按兼容约定忽略)
        "enable_thinking": False,
    }

    trace = {
        "operation": "statement_amount_extraction",
        "file_index": file_index,
        "table_index": table_index,
        "page_index": page_index,
        "header_count": len(headers),
        "row_count": len(rows),
        "image_bytes": len(png_bytes),
        "grounding_chars": len(grounding_text),
    }
    if trace_context:
        trace.update(trace_context)

    try:
        # trace 仅写入 task_llm_calls.payload._trace,不发送给模型服务
        with model_call_context(statement_amount=trace):
            content = engine._post_chat(payload, kind="statement-amount")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "llm amount extract failed file_index=%s table_index=%s page_index=%s: %s",
            file_index, table_index, page_index, exc,
        )
        return None

    # 记录模型返回报文摘要,便于分析"识别不到金额"的根因(不记原文,防敏感金额入日志)
    logger.info(
        "llm amount extract response file_index=%s table_index=%s page_index=%s response_summary=%s",
        file_index, table_index, page_index, log_value_summary(content),
    )

    items = _parse_amount_response(
        content,
        headers,
        grounding_text,
        file_index=file_index,
        file_name=file_name,
        table_index=table_index,
        page_index=page_index,
    )
    logger.info(
        "llm amount extract result file_index=%s table_index=%s page_index=%s items=%s",
        file_index, table_index, page_index, len(items) if items is not None else None,
    )
    return items


def _parse_amount_response(
    content: str,
    headers: list[str],
    grounding_text: str,
    *,
    file_index: int,
    file_name: str,
    table_index: int,
    page_index: int,
) -> list[StatementAmountItem] | None:
    """解析 LLM 返回的 JSON,产出通过 grounding 校验的 items。

    格式约束:{"items": [{"amount": "1680.00"}, ...]}。
    每条 amount 经 _parse_decimal 归一化后必须在 grounding_text(同样归一化)中
    能逐字溯源,否则丢弃该条;JSON 不合法返回 None。
    """
    if not content:
        return None
    try:
        # 允许模型在 JSON 外包一层 ```json ... ``` 或多余文本
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            return None
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        logger.warning(
            "llm amount extract response not json: response_summary=%s",
            log_value_summary(content),
        )
        return None

    raw_items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(raw_items, list):
        return None

    # grounding 文本做与数值同样的归一化(去千分位分隔符、去空白),用于子串包含
    norm_grounding = _normalize_for_grounding(grounding_text)
    # 语义列名:优先用表头里现成的金额列名;否则兜底
    col_name = _pick_amount_column_name(headers)

    items: list[StatementAmountItem] = []
    for idx, raw in enumerate(raw_items):
        amount = raw.get("amount") if isinstance(raw, dict) else None
        if not isinstance(amount, str):
            continue
        canonical_value = _canonicalize_amount(amount)
        if canonical_value is None:
            continue
        canonical, value = canonical_value
        # grounding 校验:归一化后的金额数字必须在 OCR 文本里逐字出现
        norm_amount = _normalize_for_grounding(amount)
        if norm_amount not in norm_grounding:
            logger.info(
                "llm amount extract grounding reject file_index=%s table_index=%s "
                "amount=%s (not found in page text)",
                file_index, table_index, log_value_summary(amount),
            )
            continue
        items.append(
            StatementAmountItem(
                file_index=file_index,
                file_name=file_name,
                table_index=table_index,
                page_index=page_index,
                row_index=idx,
                row_label="",
                column=col_name,
                raw_cell=amount[:200],
                canonical=canonical,
                value=value,
            )
        )
    return items


# —— 内部工具 ——

def _format_headers(headers: list[str]) -> str:
    return "\n".join(f"  [{i}] {h}" for i, h in enumerate(headers)) or "  (无表头)"


def _format_rows(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "  (无数据行)"
    lines = []
    for i, row in enumerate(rows):
        # 对齐到表头宽度,缺位补空
        cells = [(row[j] if j < len(row) else "") for j in range(len(headers))] if headers else row
        lines.append(f"  行{i}: " + " | ".join(str(c or "") for c in cells))
    return "\n".join(lines)


def _normalize_for_grounding(text: str) -> str:
    """grounding 比对用的归一化:去千分位分隔符(半/全角逗号)与所有空白。

    使 "1,680.00" 与 OCR 文本 "1,680.00" / "1680.00" 能互相匹配。
    """
    if not text:
        return ""
    return re.sub(r"[\s,，]+", "", str(text))


def _canonicalize_amount(raw: str) -> tuple[str, float] | None:
    """把 LLM 返回的金额字符串归一化为 (canonical, value)。

    复用 elements._parse_decimal(去千分位/空白)与 _canonical_amount(按"元")。
    负数/不可解析返回 None。
    """
    number = _elements._parse_decimal(raw)
    if number is None:
        return None
    if number < 0:
        return None
    canonical = _elements._canonical_amount(number, "元")
    try:
        value = float(Decimal(canonical[len("CNY:"):]))
    except (InvalidOperation, ValueError):
        return None
    return canonical, value


def _pick_amount_column_name(headers: list[str]) -> str:
    """选一个语义列名写入 StatementAmountItem.column(可审计)。

    优先取表头里最像金额的列名(金额/价税合计/税额/合计);都没有则用 "金额(llm)"。
    """
    from .amount_column import AMOUNT_COLUMN_KEYWORDS

    compiled = [re.compile(pat) for pat, _ in AMOUNT_COLUMN_KEYWORDS]
    for h in headers:
        name = re.sub(r"\s+", "", str(h)).strip()
        if name and any(p.search(name) for p in compiled):
            return h
    return "金额(llm)"
