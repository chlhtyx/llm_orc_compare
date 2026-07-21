"""LLM 差异比对(无标注版管线核心)。

与 `compare/judge.py` 的区别:judge 只对**已算好的**字符 diff 做风险分级(返回
{risk_level, reason});本模块让 LLM **直接产出**整篇差异——接收 Word 与 PDF 两段
纯文本,单次 chat 调用返回结构化 hunks,不依赖任何确定性 diff 算法。

设计要点:
- 复用 judge_* 配置(judge_api_base / judge_api_key / judge_model / judge_timeout),
  与辅助说明复用同一纯文本 LLM 服务,零新增配置。
- HTTP 重试/超时/observability 骨架与 `judge.py`、`ocr/llm.py` 完全一致。
- 输出严格 JSON,映射到现有 `TextDiffHunk` / `DiffSegment` / `TextDiffReport`,
  前端与存储无感。
- 行级统计(equal/replaced/inserted/deleted)由后端从 hunks 聚合补全,不依赖模型
  数行数,避免计数不一致。
- 失败策略:解析失败/字段非法/调用失败 → 直接 raise(由 task 层标记 failed)。
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
from ..models import DiffSegment, TextDiffHunk, TextDiffReport
from ..observability import log_model_failure, log_model_request, log_model_response

logger = logging.getLogger(__name__)

_DIFF_SYSTEM_PROMPT = (
    "你是合同关键差异比对助手。给定两段文本:【原文】(Word 基准件)与【待核件】(PDF 扫描件 OCR 识别文本),"
    "快速找出**实质性的关键差异**,以 JSON 输出。\n"
    "严格只输出 JSON,不要任何解释、不要 markdown 代码块,输出的第一个字符必须是 '{'。\n"
    "\n"
    "核心原则:\n"
    "- 两端文本来自不同来源(Word 文档 vs 扫描件 OCR),换行点、空格、标点、全半角、"
    "段落边界经常不一致。这些都不是差异,必须忽略。\n"
    "- 只关注**语义内容**是否被实质性改动,不做机械的逐行/逐字对照。\n"
    "\n"
    "必须报出的关键差异(以下类别):\n"
    "- 金额、数量、比例、日期、期限、利率等数字类要素被修改;\n"
    "- 账号、开户行、统一社会信用代码、身份证号、主体名称等关键字段被修改;\n"
    "- 条款被整条新增、删除、或核心义务/责任/违约条款被实质性改写;\n"
    "- 关键措辞变化导致权利义务明显改变(如「有权」↔「无权」、「承担」↔「不承担」、否定词增删)。\n"
    "\n"
    "必须忽略的非差异(OCR 噪声与排版差异):\n"
    "- 空格增减、全角/半角、中英文标点差异(如 , 与 、 ; 与 ;);\n"
    "- 换行位置不同、段落被拆分或合并、行序在语义不变前提下的错位;\n"
    "- OCR 在字段值前重复字段名或公司名等冗余前缀(只要实际值未变);\n"
    "- 页眉页脚、页码、「(注:部分内容可能由 AI 生成)」等水印性文字;\n"
    "- 标点、空白、字间距等纯排版差异。\n"
    "\n"
    "对齐方式:\n"
    "- 按语义段落或条款(如「第X条」「X.X」编号块)对齐,而非按物理行对齐。"
    "两端同一条款即使换行不同,也要对照其完整内容判断是否实质修改。\n"
    "\n"
    "差异类型 tag:\n"
    "- replace:原文某段内容被实质改写(给出原文侧与待核件侧的对应文字);\n"
    "- delete:原文有、待核件缺失的条款/要素;\n"
    "- insert:待核件新增的条款/要素。\n"
    "\n"
    "每个差异片段用尽可能少的文字描述该处改动的原文侧与待核件侧内容"
    "(word_lines / pdf_lines 为对应的文字片段,不必整段复制,聚焦改动部分即可),"
    "可带一句 context_before 标注所在条款(无则空数组),不需要 context_after。\n"
    "若两端在关键内容上一致,hunks 为空数组。\n"
    "\n"
    "JSON 结构为:\n"
    "{\"hunks\": [\n"
    "  {\"tag\": \"replace|delete|insert\",\n"
    "   \"word_lines\": [\"原文对应片段\"],\n"
    "   \"pdf_lines\": [\"待核件对应片段\"],\n"
    "   \"context_before\": [\"所在条款/定位行\"],\n"
    "   \"context_after\": []}\n"
    "],\n"
    " \"similarity\": 0.0到1.0之间的整体相似度估计(仅计关键内容,忽略 OCR 噪声)}\n"
    "\n"
    "字段约定:\n"
    "- word_lines:replace/delete 时为原文侧片段;insert 时为空数组。\n"
    "- pdf_lines:replace/insert 时为待核件侧片段;delete 时为空数组。\n"
    "- similarity:1.0 表示关键内容一致,越低表示关键差异越大。\n"
    "- 不要输出 char_segments 字段。"
)

_CHAR_DIFF_INSTRUCTION = (
    "\n额外要求:对每个 tag=replace 且 word_lines 与 pdf_lines 各只有 1 行的片段,"
    "请在 char_segments 中给出该行的字符级差异(op: equal/delete/insert,text: 片段文本),"
    "其中 delete 取自 word 行,insert 取自 pdf 行,equal 为两行共有部分。"
    "其余片段(多行 replace / delete / insert)不要输出 char_segments。"
)


def llm_text_diff(
    word_text: str,
    pdf_text: str,
    *,
    char_level: bool = False,
    source: str = "",
    target: str = "",
) -> TextDiffReport:
    """对两段纯文本做 LLM 差异比对,产出 TextDiffReport。

    未配置 judge_* 时直接 raise(无标注版管线需要 LLM 才能工作,不回退)。
    解析失败或字段非法也直接 raise。
    """
    if not settings.judge_api_base or not settings.judge_model:
        raise RuntimeError(
            "无标注版 LLM 比对未配置:请在设置页填写 judge_api_base / judge_model"
        )

    system_prompt = _DIFF_SYSTEM_PROMPT + (_CHAR_DIFF_INSTRUCTION if char_level else "")
    user_content = f"【原文】\n{word_text}\n\n【待核件】\n{pdf_text}\n\n请比对并输出 JSON。"

    content = _post_judge_chat(system_prompt, user_content)
    parsed = _extract_json(content)
    if not isinstance(parsed, dict):
        raise ValueError(f"LLM 比对返回非 JSON 对象: {content[:200]}")

    hunks = _build_hunks(parsed.get("hunks"), char_level=char_level)
    similarity = _coerce_similarity(parsed.get("similarity"))

    stats = _aggregate_stats(word_text, pdf_text, hunks, similarity)
    logger.info(
        "llm diff done hunks=%s similarity=%.3f replaced=%s inserted=%s deleted=%s",
        len(hunks), similarity,
        stats["replaced"], stats["inserted"], stats["deleted"],
    )
    return TextDiffReport(
        source=source,
        target=target,
        word_text=word_text,
        pdf_text=pdf_text,
        hunks=hunks,
        stats=stats,
    )


def _post_judge_chat(system_prompt: str, user_content: str) -> str:
    """发一次 OpenAI 兼容 chat/completions 请求,返回 message content。

    复用 judge_* 配置;重试语义与 judge.py、ocr/llm.py 一致:
    TransportError / 429 / 5xx 重试,4xx 立即抛出。
    """
    url = settings.judge_api_base.rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": settings.judge_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0,
        # 不带 response_format=json_object:部分推理服务(SiliconFlow 等)在
        # json_object 约束解码 + 长输入下会触发服务端 500/超时。改为纯 prompt
        # 约束 + 后端 _extract_json 容错(支持 markdown 围栏 / 夹杂文本)。
        # enable_thinking=False:Qwen3 系列默认输出 <think> 思考链,这些 token
        # 不进结果但严重拖慢生成(逐行比对是确定性任务,无需思考)。Qwen3 原生支持
        # 该参数;非 Qwen3 模型按 OpenAI 兼容约定忽略未知参数,不报错。
        "enable_thinking": False,
    }
    headers = {"Authorization": f"Bearer {settings.judge_api_key}"}
    timeout = httpx.Timeout(settings.judge_timeout, connect=10.0)
    max_retries = settings.llm_max_retries

    last_exc: Exception | None = None
    with httpx.Client(timeout=timeout) as client:
        for attempt in range(max_retries + 1):
            request_started = log_model_request(logger, "llm-diff", url, payload, attempt + 1)
            try:
                resp = client.post(url, json=payload, headers=headers)
            except httpx.TransportError as exc:
                last_exc = exc
                log_model_failure(logger, "llm-diff", request_started, str(exc))
                logger.warning(
                    "llm-diff transport error attempt=%s/%s reason=%s",
                    attempt + 1, max_retries + 1, exc,
                )
            else:
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_exc = httpx.HTTPStatusError(
                        f"服务端瞬态错误:HTTP {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                    log_model_failure(
                        logger, "llm-diff", request_started,
                        f"transient HTTP {resp.status_code}",
                        status_code=resp.status_code,
                    )
                    logger.warning(
                        "llm-diff transient http status=%s attempt=%s/%s",
                        resp.status_code, attempt + 1, max_retries + 1,
                    )
                else:
                    try:
                        resp.raise_for_status()  # 4xx:不可重试,直接抛
                    except httpx.HTTPStatusError as exc:
                        log_model_failure(
                            logger, "llm-diff", request_started, str(exc),
                            status_code=resp.status_code,
                        )
                        raise
                    data = resp.json()
                    log_model_response(logger, "llm-diff", resp.status_code, data, request_started)
                    return data["choices"][0]["message"]["content"]
            if attempt < max_retries:
                backoff = min(2 ** attempt, 8) + random.random()
                logger.info("llm-diff retry after %.1fs", backoff)
                time.sleep(backoff)
    assert last_exc is not None
    logger.error("llm-diff give up after %s attempts: %s", max_retries + 1, last_exc)
    raise last_exc


def _build_hunks(raw_hunks: Any, *, char_level: bool) -> list[TextDiffHunk]:
    """把 LLM 返回的 hunks 数组映射为 TextDiffHunk 列表。

    字段非法的条目跳过并告警;tag 不合法直接 raise(按"直接报错"决策)。
    """
    if raw_hunks is None:
        return []
    if not isinstance(raw_hunks, list):
        raise ValueError(f"LLM 比对 hunks 非数组: {type(raw_hunks).__name__}")

    valid_tags = {"replace", "delete", "insert"}
    hunks: list[TextDiffHunk] = []
    for idx, item in enumerate(raw_hunks):
        if not isinstance(item, dict):
            logger.warning("llm diff hunk[%s] 非对象,跳过: %r", idx, item)
            continue
        tag = item.get("tag")
        if tag not in valid_tags:
            raise ValueError(f"LLM 比对 hunk[{idx}] tag 非法: {tag!r}")
        word_lines = _as_str_list(item.get("word_lines"), "word_lines", idx)
        pdf_lines = _as_str_list(item.get("pdf_lines"), "pdf_lines", idx)
        context_before = _as_str_list(item.get("context_before"), "context_before", idx)
        context_after = _as_str_list(item.get("context_after"), "context_after", idx)

        char_segments: list[DiffSegment] = []
        if char_level and tag == "replace" and len(word_lines) == 1 and len(pdf_lines) == 1:
            # char_segments 完全由 LLM 产出;模型不给就留空(前端回退到整行红删绿增)。
            char_segments = _parse_char_segments(item.get("char_segments"))

        hunks.append(
            TextDiffHunk(
                tag=tag,  # type: ignore[arg-type]
                word_lines=word_lines,
                pdf_lines=pdf_lines,
                char_segments=char_segments,
                context_before=context_before,
                context_after=context_after,
            )
        )
    return hunks


def _parse_char_segments(raw_segs: Any) -> list[DiffSegment]:
    """解析 LLM 给出的字符级 segments;非法条目跳过。模型不给则留空。"""
    if not isinstance(raw_segs, list):
        return []
    valid_ops = {"equal", "delete", "insert"}
    segs: list[DiffSegment] = []
    for s in raw_segs:
        if not isinstance(s, dict):
            continue
        op = s.get("op")
        text = s.get("text")
        if op in valid_ops and isinstance(text, str):
            segs.append(DiffSegment(op=op, text=text))  # type: ignore[arg-type]
    return segs


def _as_str_list(value: Any, field: str, hunk_idx: int) -> list[str]:
    """把字段值规整为字符串列表。None/缺失 → 空数组;非字符串元素转 str。"""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"LLM 比对 hunk[{hunk_idx}] {field} 非数组: {type(value).__name__}")
    return [str(v) if v is not None else "" for v in value]


def _coerce_similarity(value: Any) -> float:
    """把 similarity 字段收敛到 [0, 1]。非法时返回 0.0。"""
    try:
        sim = float(value)
    except (TypeError, ValueError):
        logger.warning("llm diff similarity 非法,置 0: %r", value)
        return 0.0
    if sim < 0:
        return 0.0
    if sim > 1:
        return 1.0
    return sim


def _aggregate_stats(
    word_text: str, pdf_text: str, hunks: list[TextDiffHunk], similarity: float
) -> dict:
    """从 hunks 聚合行级统计,补全前端 RawReportView 依赖的 stats 字段。

    统计口径(供前端 RawReportView 卡片显示):
    - replaced: 每个 replace hunk 计 max(word_lines, pdf_lines)
    - replaced: 每个 replace hunk 计 max(word_lines, pdf_lines)
    - deleted:  每个 delete hunk 计 len(word_lines);replace 不重复计
    - inserted: 每个 insert hunk 计 len(pdf_lines);replace 不重复计
    - equal_lines: 由总行数反推(与 similarity 口径对齐:2*eq/(W+P))
    """
    replaced = sum(max(len(h.word_lines), len(h.pdf_lines)) for h in hunks if h.tag == "replace")
    deleted = sum(len(h.word_lines) for h in hunks if h.tag == "delete")
    inserted = sum(len(h.pdf_lines) for h in hunks if h.tag == "insert")

    word_total = len([ln for ln in word_text.splitlines() if ln]) or 1
    pdf_total = len([ln for ln in pdf_text.splitlines() if ln]) or 1
    # 由 similarity 反推一致行数: similarity = 2*eq/(W+P)
    equal_lines = max(0, round(similarity * (word_total + pdf_total) / 2))

    return {
        "similarity": round(similarity, 4),
        "equal_lines": equal_lines,
        "replaced": replaced,
        "deleted": deleted,
        "inserted": inserted,
        "word_total_lines": word_total,
        "pdf_total_lines": pdf_total,
        "engine": "llm",
    }


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
    logger.warning("llm diff json parse failed; raw=%s", text[:200])
    return text
