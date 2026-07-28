"""对帐单金额统计 — LLM 兜底列指认。

仅在启发式 detect_amount_columns 返回空(无法定位金额列)时调用。
让多模态 LLM 只指认"哪一列是金额列",返回列索引 + 角色(amount/paid/unpaid/total)。

严格护栏(贯彻 AGENTS.md「LLM 不能撤销确定性结论」):
  - LLM 输出**仅是列索引+角色**,不做数值识别、不做求和。
  - 金额值仍由代码从 OCR 的 TableStructure.rows 用 elements._fact_spans 抽取。
  - 求和仍由代码完成。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..observability import log_value_summary

logger = logging.getLogger(__name__)

# LLM 返回的角色白名单(对齐 AMOUNT_COLUMN_KEYWORDS 的角色)
_VALID_ROLES = {"amount", "paid", "unpaid", "total"}

_SYSTEM_PROMPT = (
    "你是表格结构分析助手。任务:看图识别表格中哪些列是金额列。"
    "只返回 JSON,不做任何数值识别或计算。"
    "金额列角色:amount=一般金额, paid=已付/实付, unpaid=未付/应付, total=合计/小计。"
)

_USER_PROMPT_TEMPLATE = (
    "下表表头各列如下(0 基索引):\n{headers_block}\n\n"
    "请只返回 JSON,格式为 {{\"columns\": [{{\"index\": 0, \"role\": \"amount\"}}]}}。"
    "只指认金额相关的列;如果没有金额列,返回 {{\"columns\": []}}。"
    "不要识别具体数值,不要做任何加法或求和。"
)


def llm_detect_amount_columns(
    png_bytes: bytes,
    headers: list[str],
) -> dict[int, str] | None:
    """启发式列定位失败时,让多模态 LLM 指认金额列。

    Args:
        png_bytes: 表格所在页的 PNG 渲染字节(由 parsing.pdf.render_pages 产出)。
        headers: 表头列表(用于在 prompt 中列出各列名)。

    Returns:
        {col_index: column_role};LLM 调用失败或解析失败返回 None(调用方标 needs_review)。
        返回空 dict 表示 LLM 明确判定无金额列(调用方据此决定是否标 needs_review)。
    """
    if not headers:
        return None
    if not png_bytes:
        return None

    # 延迟导入,避免 statement 包在 OCR 引擎未配置时仍可被单元测试 import
    from ..ocr.llm import LLMOCREngine, _to_data_url  # type: ignore

    headers_block = "\n".join(f"  [{i}] {h}" for i, h in enumerate(headers))
    data_url = _to_data_url(png_bytes)

    payload: dict[str, Any] = {
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": _USER_PROMPT_TEMPLATE.format(headers_block=headers_block)},
                ],
            },
        ],
        "temperature": 0,
        # enable_thinking=False:关闭 Qwen3 系列默认输出的 <think> 思考链 token
        # (金额列定位是确定性判断,这些 token 不进结果但严重拖慢生成)。Qwen3 原生
        # 支持该参数;非 Qwen3 模型按 OpenAI 兼容约定忽略未知参数,不报错。
        "enable_thinking": False,
    }

    try:
        engine = LLMOCREngine()
        content = engine._post_chat(payload, kind="statement-column")
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm column detect failed: %s", exc)
        return None

    return _parse_column_response(content, headers)


def _parse_column_response(content: str, headers: list[str]) -> dict[int, str] | None:
    """解析 LLM 返回的 JSON,产出 {col_index: role}。

    格式约束:{"columns": [{"index": int, "role": str}, ...]}。
    严格校验:index 越界、role 不在白名单、JSON 不合法 → 返回 None。
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
            "llm column detect response not json: response_summary=%s",
            log_value_summary(content),
        )
        return None

    columns = data.get("columns") if isinstance(data, dict) else None
    if not isinstance(columns, list):
        return None

    result: dict[int, str] = {}
    for item in columns:
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        role = item.get("role")
        if not isinstance(idx, int) or idx < 0 or idx >= len(headers):
            continue
        if not isinstance(role, str) or role not in _VALID_ROLES:
            continue
        # 同列重复取先到的
        if idx not in result:
            result[idx] = role
    return result
