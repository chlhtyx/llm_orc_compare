"""LLM 篡改复核判决(规则+LLM 结合)。

规则引擎(`classify_diff`)做初筛,对 `modified` 条款调 LLM 复核:
判断是"实质篡改"还是"OCR 噪声/同义改写",可升级或降级风险等级。

使用独立的 judge_* 配置(judge_api_base / judge_api_key / judge_model),
与 OCR 服务分开:OCR 需多模态 VL 模型(看图),复核需纯文本 LLM(判语义)。
走 OpenAI 兼容 Chat Completions 协议。temperature=0 + response_format=json_object
保证结构化输出。未配置 judge_* 时自动回退规则判定。
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from typing import Any

import httpx

from ..config import settings
from ..models import DiffSegment, RiskLevel

logger = logging.getLogger(__name__)

_JUDGE_SYSTEM_PROMPT = (
    "你是合同条款篡改检测的复核助手。下面给你一对条款:Word 原文与 PDF OCR 识别文本,"
    "以及字符级差异片段和规则引擎的初步风险判定。\n"
    "请判断这对条款的差异是「实质篡改」(故意修改关键内容)还是"
    "「OCR 噪声/同义改写」(识别误差或无害的表述调整)。\n"
    "严格只输出 JSON,不要解释、不要 markdown 代码块。\n"
    "JSON 结构为:{\"risk_level\": \"high|medium|low|none\", \"reason\": \"一句话理由\"}。\n"
    "判定指引:\n"
    "- high:金额、日期、违约责任、管辖法院等高风险要素被实质性修改。\n"
    "- medium:条款内容有实质性改动,但不涉及上述极高要素。\n"
    "- low:仅措辞/标点/排版差异,无实质影响(OCR 噪声归此类)。\n"
    "- none:无实质差异(OCR 噪声导致的误报)。\n"
    "注意:OCR 可能将数字误识(如 0→O、多字少字),若差异仅源于此应判 low/none。"
)

_VALID_RISK_LEVELS = {"high", "medium", "low", "none"}


def _format_segments(segments: list[DiffSegment]) -> str:
    """把字符级 diff 片段渲染为可读文本(标注删/增)。"""
    parts: list[str] = []
    for seg in segments:
        if seg.op == "equal":
            parts.append(seg.text)
        elif seg.op == "delete":
            parts.append(f"[-{seg.text}-]")
        elif seg.op == "insert":
            parts.append(f"[+{seg.text}+]")
    return "".join(parts)


def _extract_json(text: str) -> Any:
    """从可能混杂文本/代码块的回复中提取首个 JSON 对象。"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    logger.warning("llm judge json parse failed; raw=%s", text[:200])
    return {}


def llm_judge_diff(
    word_text: str,
    pdf_text: str,
    segments: list[DiffSegment],
    rule_risk: RiskLevel,
) -> tuple[RiskLevel, list[str]]:
    """对 modified 条款做 LLM 复核,返回(修正后风险级,理由列表)。

    使用独立的 judge_* 配置(judge_api_base / judge_api_key / judge_model),
    与 OCR(llm_*)分开——OCR 需多模态 VL 模型,复核需纯文本 LLM。
    未配置时回退规则判定,不阻断流程。
    """
    if not settings.judge_api_base or not settings.judge_model:
        logger.info("llm judge skipped: no judge config, fallback to rule")
        return rule_risk, ["LLM 复核未配置,沿用规则判定"]

    diff_text = _format_segments(segments)
    user_content = (
        f"【Word 原文】\n{word_text}\n\n"
        f"【PDF OCR 文本】\n{pdf_text}\n\n"
        f"【字符级差异】([-删除-] / [+新增+])\n{diff_text}\n\n"
        f"【规则初判风险级】{rule_risk}\n\n"
        f"请复核并输出 JSON。"
    )

    url = settings.judge_api_base.rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": settings.judge_model,
        "messages": [
            {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {settings.judge_api_key}"}
    timeout = httpx.Timeout(settings.judge_timeout, connect=10.0)

    last_exc: Exception | None = None
    max_retries = settings.llm_max_retries
    with httpx.Client(timeout=timeout) as client:
        for attempt in range(max_retries + 1):
            try:
                resp = client.post(url, json=payload, headers=headers)
            except httpx.TransportError as exc:
                last_exc = exc
                logger.warning("llm judge transport error attempt=%s/%s %s", attempt + 1, max_retries + 1, exc)
            else:
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_exc = httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
                    logger.warning("llm judge transient http=%s attempt=%s/%s", resp.status_code, attempt + 1, max_retries + 1)
                else:
                    resp.raise_for_status()
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    parsed = _extract_json(content)
                    risk = parsed.get("risk_level", "").strip().lower()
                    reason = (parsed.get("reason") or "").strip()
                    if risk not in _VALID_RISK_LEVELS:
                        logger.warning("llm judge invalid risk_level=%s, fallback to rule", risk)
                        return rule_risk, ["LLM 复核返回无效风险级,沿用规则判定"]
                    reasons = [f"LLM 复核:{reason}"] if reason else ["LLM 复核判定"]
                    return risk, reasons  # type: ignore[return-value]
            if attempt < max_retries:
                backoff = min(2 ** attempt, 8) + random.random()
                time.sleep(backoff)

    logger.warning("llm judge failed after retries: %s, fallback to rule", last_exc)
    return rule_risk, ["LLM 复核失败,沿用规则判定"]
