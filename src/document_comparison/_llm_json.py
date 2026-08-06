"""从 LLM 回复中提取 JSON 的共享 helper。

历史上 ``compare/llm_diff.py``、``compare/judge.py``、``align/llm_resolver.py``、
``ocr/llm.py`` 各自维护一份近似的 ``_extract_json`` 实现,行为存在细微分叉:

- 仅 ``ocr/llm.py`` 兜底试 JSON 数组正则 ``[...]``,其余只试对象 ``{...}``;
- 仅 ``compare/llm_diff.py`` 彻底解析失败时返回**原始字符串**(其余返回 ``{}``);
- ``align/llm_resolver.py`` 会把解析成功但非 dict 的结果归一为 ``{}``,其余不做;
- ``align/llm_resolver.py`` 失败时**不记日志**,其余记 warning。

本模块合并为一份,取各家之长:markdown 围栏去除 + 对象/数组正则兜底 + 非 dict 归一 +
失败统一 warning。调用方按需选用:

- ``extract_llm_json`` — 彻底失败返回 ``{}``(向后兼容 judge/resolver/ocr 的优雅降级);
- ``extract_llm_json_strict`` — 彻底失败抛 ``LLMJsonParseError``,供需要区分「真失败」
  与「合法空结果」的调用方(如 llm_diff 降级为 needs_review)。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from .observability import log_value_summary

logger = logging.getLogger(__name__)

# 匹配首个 {...} 或 [...]。贪婪匹配到字符串中最后一个右括号;覆盖绝大多数 LLM
# 「JSON 前后带叙述」场景。嵌套结构同层会被一并吞入,极少触发误截断。
_OBJECT_RE = re.compile(r"\{[\s\S]*\}")
_ARRAY_RE = re.compile(r"\[[\s\S]*\]")
_FENCE_RE = re.compile(r"```(?:json)?\s?")


class LLMJsonParseError(ValueError):
    """LLM 回复彻底无法解析为 JSON 时抛出(供 strict 调用方捕获降级)。"""


def extract_llm_json(text: str, *, expect: type = dict) -> Any:
    """从可能混杂文本/markdown 代码块的 LLM 回复中提取首个 JSON 结构。

    彻底解析失败时返回 ``{}`` 并记 warning(向后兼容既有优雅降级调用方)。
    需要区分「真失败」的调用方改用 :func:`extract_llm_json_strict`。

    Args:
        text: LLM 返回的原始 message content。
        expect: 期望的顶层类型。``dict``(默认)时,解析成功但顶层非 dict 归一为 ``{}``;
            传入 ``list`` 或 ``(dict, list)`` 时保留数组(供 ``ocr/llm.py`` 这类调用方)。
    """
    try:
        return _extract(text, expect=expect)
    except LLMJsonParseError:
        return {}


def extract_llm_json_strict(text: str, *, expect: type = dict) -> Any:
    """与 :func:`extract_llm_json` 相同,但彻底解析失败时抛 :class:`LLMJsonParseError`。

    供需要区分「真失败」的调用方使用——例如 ``compare/llm_diff.py`` 需在失败时降级为
    ``needs_review``,而不是把失败静默当作「无差异」。
    """
    return _extract(text, expect=expect)


def _extract(text: str, *, expect: type) -> Any:
    if not isinstance(text, str):
        logger.warning("extract_llm_json non-str input: %s", type(text).__name__)
        raise LLMJsonParseError(f"non-str input: {type(text).__name__}")

    cleaned = _FENCE_RE.sub("", text).strip()

    # ① 整段直接解析(最快路径)
    try:
        return _coerce(json.loads(cleaned), expect)
    except json.JSONDecodeError:
        pass

    # ② 正则兜底:首个 {...} 或 [...]。expect 决定是否试数组正则。
    patterns = (_OBJECT_RE, _ARRAY_RE) if expect in (list, (dict, list)) else (_OBJECT_RE,)
    for pat in patterns:
        m = pat.search(cleaned)
        if not m:
            continue
        try:
            return _coerce(json.loads(m.group(0)), expect)
        except json.JSONDecodeError:
            continue

    logger.warning(
        "llm json parse failed; response_summary=%s",
        log_value_summary(text),
    )
    raise LLMJsonParseError("LLM 回复无法解析为 JSON")


def _coerce(parsed: Any, expect: type) -> Any:
    """按 ``expect`` 归一化解析结果。彻底非预期类型当作解析失败。"""
    if expect is dict:
        if isinstance(parsed, dict):
            return parsed
        raise LLMJsonParseError(f"expected JSON object, got {type(parsed).__name__}")
    if expect in (list, (dict, list)):
        if isinstance(parsed, (dict, list)):
            return parsed
        raise LLMJsonParseError(
            f"expected JSON object/array, got {type(parsed).__name__}"
        )
    return parsed
