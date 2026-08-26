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
- 失败策略:**HTTP 调用失败/字段非法 → 直接 raise**(由 task 层标记 failed);
  **LLM 返回彻底无法解析为 JSON 对象时(纯叙述/乱码)→ 降级为空 hunks +
  recognition_status="needs_review"**,任务继续跑完,不中断(与 judge/ocr 等模块的
  优雅降级惯例一致)。调用失败仍 raise(配置/网络错误属不可降级)。
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any

import httpx

from .._llm_json import LLMJsonParseError, extract_llm_json_strict
from ..config import settings
from ..llm_protocol import build_chat_request, parse_chat_content
from ..models import (
    Diff,
    DiffSegment,
    PageMeta,
    PageRegion,
    TamperReport,
    TextDiffHunk,
    TextDiffReport,
    TruncationRecord,
)
from ..observability import (
    log_model_failure,
    log_model_request,
    log_model_response,
    log_value_summary,
)

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

# /no_think:GLM-4.5/4.6 关闭 <think> 思考链的指令(等价于 Qwen3 的 enable_thinking=False,
# 但 GLM 系列需写入 prompt 文本而非 payload 字段)。judge / llm-diff 两个比对调用统一
# 追加此后缀,便于集中维护;非 GLM 模型按普通文本忽略,不报错。
_NO_THINK_SUFFIX = "/no_think"

# 导出别名:供 API 层在「查看内置默认规则」UI 上只读展示(settings.llm_direct_diff_prompt
# 为空时实际生效的 system prompt 前半段;char_level 额外追加的部分与用户自定义无关)。
# 注意:此处仅导出 _DIFF_SYSTEM_PROMPT 原义,不含运行时追加的 /no_think 后缀——
# 该后缀是模型行为控制指令,不属于「规则」语义,不应在只读默认规则面板中展示。
DEFAULT_DIFF_SYSTEM_PROMPT = _DIFF_SYSTEM_PROMPT


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

    no_think = _NO_THINK_SUFFIX if settings.llm_diff_no_think_enabled else ""
    system_prompt = (
        settings.llm_direct_diff_prompt or _DIFF_SYSTEM_PROMPT
    ) + (_CHAR_DIFF_INSTRUCTION if char_level else "") + no_think
    user_content = f"【原文】\n{word_text}\n\n【待核件】\n{pdf_text}\n\n请比对并输出 JSON。"

    content = _post_judge_chat(system_prompt, user_content)
    try:
        parsed = extract_llm_json_strict(content)
    except LLMJsonParseError:
        # 解析彻底失败(纯叙述/乱码/裸数组):降级为待人工复核,不中断任务。
        # (extract_llm_json_strict 已记 warning;这里补一条业务级日志。)
        logger.warning(
            "llm diff 返回非 JSON 对象,降级 needs_review; summary=%s",
            log_value_summary(content),
        )
        return _needs_review_report(word_text, pdf_text, source=source, target=target)

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
    """发一次对话 LLM 请求,返回 assistant 文本。

    复用 judge_* 配置(协议由 judge_api_protocol 决定:
    openai / openai_responses / anthropic);重试语义与 judge.py、ocr/llm.py 一致:
    TransportError / 429 / 5xx 重试,4xx 立即抛出。
    """
    url, headers, payload = build_chat_request(
        api_base=settings.judge_api_base,
        api_key=settings.judge_api_key,
        payload={
            "model": settings.judge_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0,
            # 不带 response_format=json_object:部分推理服务(SiliconFlow 等)在
            # json_object 约束解码 + 长输入下会触发服务端 500/超时。改为纯 prompt
            # 约束 + 后端 _extract_json 容错(支持 markdown 围栏 / 夹杂文本)。
            # chat_template_kwargs.enable_thinking=False:Qwen3 系列默认输出 <think> 思考链,
            # 这些 token 不进结果但严重拖慢生成(逐行比对是确定性任务,无需思考)。必须嵌进
            # chat_template_kwargs 才会被 vLLM 应用到 chat template;顶层 enable_thinking
            # 字段在多数 vLLM 版本被忽略(见 vllm#35574)。非 Qwen3 模型按兼容约定忽略。
            "chat_template_kwargs": {"enable_thinking": False},
        },
        protocol=settings.judge_api_protocol,
    )
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
                    return parse_chat_content(data, protocol=settings.judge_api_protocol)
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


def _needs_review_report(
    word_text: str, pdf_text: str, *, source: str = "", target: str = ""
) -> TextDiffReport:
    """LLM 解析彻底失败时的降级产物:空 hunks + needs_review 标记。

    与 raw_pipeline.py 后续 ``if not diagnostic.reliable: recognition_status = needs_review``
    叠加逻辑兼容(这里已置 needs_review,raw_pipeline 再设同值无副作用)。
    不抛异常,任务继续跑完,用户在报告页看到「待人工复核」而非「任务失败」。
    """
    word_total = len([ln for ln in word_text.splitlines() if ln]) or 1
    pdf_total = len([ln for ln in pdf_text.splitlines() if ln]) or 1
    return TextDiffReport(
        source=source,
        target=target,
        word_text=word_text,
        pdf_text=pdf_text,
        hunks=[],
        stats={
            "similarity": 0.0,
            "equal_lines": 0,
            "replaced": 0,
            "deleted": 0,
            "inserted": 0,
            "word_total_lines": word_total,
            "pdf_total_lines": pdf_total,
            "engine": "llm",
            # 前端/审计标记:本次结果因 LLM 解析失败而降级,非真实「无差异」。
            "llm_parse_failed": True,
        },
        recognition_status="needs_review",
    )


def text_diff_to_tamper_report(
    raw: TextDiffReport,
    *,
    source: str = "",
    target: str = "",
    truncation: TruncationRecord | None = None,
    page_regions: list[list[PageRegion]] | None = None,
    page_metas: list[PageMeta] | None = None,
    source_page_regions: list[list[PageRegion]] | None = None,
    source_page_metas: list[PageMeta] | None = None,
) -> TamperReport:
    """把 LLM 直接比对产出的 TextDiffReport 适配为标准 TamperReport。

    供标准合同比对的 ``enable_llm_direct_diff`` 分支使用:LLM 已直接产出语义差异,
    这里只做结构映射,让任务/历史/外部 API/报告页/JSON-PDF-DOCX 全链路复用。
    映射口径:
    - 每个 hunk → 一个 Diff;replace→modified、delete→deleted、insert→added。
    - replace 优先用 LLM 的 char_segments(行内字符级),否则退化为整段 delete+insert。
    - per-diff risk_level 恒 none(LLM 直接比对不做风险分级);verdict 初值 changed,
      随后由 apply_recognition_gate 按识别质量统一校正为 needs_review。
    - 可选传入 page_regions(经 locate_hunk_regions 定位):按 hunk 顺序挂坐标,
    使报告页能渲染高亮框;未传入或某 hunk 无坐标时 page_regions 空(不渲染)。
    """
    diffs: list[Diff] = []
    for idx, hunk in enumerate(raw.hunks):
        if hunk.tag == "replace":
            status = "modified"
            if hunk.char_segments:
                segments = list(hunk.char_segments)
            else:
                segments = []
                word_text = "\n".join(hunk.word_lines)
                pdf_text = "\n".join(hunk.pdf_lines)
                if word_text:
                    segments.append(DiffSegment(op="delete", text=word_text))
                if pdf_text:
                    segments.append(DiffSegment(op="insert", text=pdf_text))
        elif hunk.tag == "delete":
            status = "deleted"
            segments = [DiffSegment(op="delete", text="\n".join(hunk.word_lines))]
        else:  # insert
            status = "added"
            segments = [DiffSegment(op="insert", text="\n".join(hunk.pdf_lines))]

        title = hunk.context_before[0].strip() if hunk.context_before else ""
        regions = list(page_regions[idx]) if page_regions and idx < len(page_regions) else []
        source_regions = (
            list(source_page_regions[idx])
            if source_page_regions and idx < len(source_page_regions)
            else []
        )
        diffs.append(
            Diff(
                alignment_id=f"llm-diff-{idx + 1}",
                status=status,  # type: ignore[arg-type]
                segments=segments,
                risk_level="none",
                verdict="changed",
                confidence="high",
                judged_by="llm",
                title=title,
                page_regions=regions,
                source_page_regions=source_regions,
            )
        )

    has_diffs = bool(diffs)
    return TamperReport(
        source=source or raw.source,
        target=target or raw.target,
        change_status="changed" if has_diffs else "clean",
        overall_risk="changed" if has_diffs else "clean",
        diffs=diffs,
        summary={
            "llm_direct_diff": True,
            "similarity": raw.stats.get("similarity"),
            "replaced": raw.stats.get("replaced", 0),
            "deleted": raw.stats.get("deleted", 0),
            "inserted": raw.stats.get("inserted", 0),
            "engine": raw.stats.get("engine", "llm"),
        },
        recognition_status=raw.recognition_status,
        recognition_diagnostics=list(raw.recognition_diagnostics),
        truncation=truncation,
        page_meta=list(page_metas) if page_metas else [],
        source_page_meta=list(source_page_metas) if source_page_metas else [],
        source_annotation_status=("available" if source_page_metas else "unavailable"),
        source_annotation_reason=(
            "" if source_page_metas else "原件侧没有可用页面坐标"
        ),
    )
