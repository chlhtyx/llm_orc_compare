"""流水线与模型调用的统一可观测性日志。

职责:
- ``timed_stage``:阶段耗时上下文管理器(所有 pipeline 使用)。
- ``log_model_request`` / ``log_model_response`` / ``log_model_failure``:对话型 LLM
  调用的请求/响应/失败日志,同时也是 LLM IO 记录的**单点拦截入口**。
- ``record_ocr_result`` / ``record_ocr_text_result``:记录最终被流水线采用的 OCR
  解析结果(页码、块、坐标、受限文本预览),用于排查模型原始响应解析后的错页/丢块问题。
- ``llm_call_collector``:contextvar 收集器上下文管理器;tasks.py 用它包住 pipeline
  执行,所有对话型 LLM 调用和 OCR 最终解析结果在任务结束时批量入库。

embedding 调用(kind="embedding")不收集(文本→向量,量太大且非对话型),
但仍像以前一样写日志。
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator


# —— 单条 LLM 调用记录(收集器 append 的对象;repository 转成 ORM 行入库)——


@dataclass
class LlmCallRecord:
    """一次模型调用或 OCR 最终解析快照的完整任务级记录。

    生命周期:log_model_request 创建(填 kind/attempt/payload/created_at,其余 None)
    → log_model_response 或 log_model_failure 补全 status_code/elapsed_ms/response/error。
    ``ocr-result`` 由 record_ocr_result / record_ocr_text_result 直接创建为完成态。
    """

    kind: str
    attempt: int
    payload: Any = None                # 已脱敏(图片 base64 → sha256 摘要)
    status_code: int | None = None
    elapsed_ms: int | None = None
    response: Any = field(default=None, repr=False)   # 已截断到 _MAX_RESPONSE_CHARS
    error: str | None = None
    created_at: str = field(           # ISO8601 UTC,JSONB 友好
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# —— 收集器:contextvar,跨 asyncio.to_thread 自动传播;threading.Thread 需 ctx.run ——


def _default_collector() -> None:
    """contextvar 默认值:None 表示「不收集」(如单测、独立脚本调用 LLM)。"""
    return None


current_llm_collector: contextvars.ContextVar[list[LlmCallRecord] | None] = (
    contextvars.ContextVar("current_llm_collector", default=_default_collector())
)


# 单次模型调用的业务上下文。它只会写入任务级模型调用明细，不会作为 HTTP
# payload 发送给模型服务，也不会把原文写入常规 app.log。
current_model_call_context: contextvars.ContextVar[dict[str, Any]] = (
    contextvars.ContextVar("current_model_call_context", default={})
)


# —— 日志关联上下文:由 HTTP 中间件和 TaskManager 注入,由 handler 统一渲染 ——

current_log_context: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "current_log_context", default={}
)


@contextmanager
def log_context(**fields: str | None) -> Iterator[None]:
    """在当前执行链附加安全的日志关联字段。

    ``ContextVar`` 会随 ``asyncio.create_task`` 和 ``asyncio.to_thread`` 传播；OCR
    的受控子线程已经通过 ``ctx.run`` 传播 context，因此同一任务的阶段、模型和
    回调日志都会带上相同的 task/request 标识。None 不覆盖父上下文。
    """
    current = current_log_context.get()
    merged = dict(current)
    merged.update({key: value for key, value in fields.items() if value is not None})
    token = current_log_context.set(merged)
    try:
        yield
    finally:
        current_log_context.reset(token)


def get_log_context() -> dict[str, str]:
    """供 logging handler 读取当前关联上下文，返回副本避免被意外修改。"""
    return dict(current_log_context.get())

# 任务级模型/OCR 审计 kind 白名单;embedding 不收集(文本→向量,量太大)。
_COLLECTED_KINDS = frozenset(
    {
        "ocr",
        "ocr-whole",
        "ocr-result",
        "paddleocr",
        "judge",
        "alignment",
        "llm-diff",
        "statement-column",
        "statement-amount",
    }
)

# response 截断上限(字符数)。PaddleOCR max_tokens=8000 响应可能接近上限,64KB 足够覆盖。
_MAX_RESPONSE_CHARS = 65536
# error 字段截断上限(对齐 ORM 列 String(512))。
_MAX_ERROR_CHARS = 512
# 最终 OCR 解析结果的双重体积限制。逐块预览用于定位断句/错页问题；完整内容可由
# content_sha256 关联原始模型响应，避免同一份大合同在 JSONB 中重复保存多次。
_MAX_OCR_RESULT_BLOCKS = 100
_MAX_OCR_BLOCK_PREVIEW_CHARS = 300
_MAX_OCR_TEXT_PREVIEW_CHARS = 32768


@contextmanager
def llm_call_collector() -> Iterator[list[LlmCallRecord]]:
    """设置一个任务级收集器,yield 出 list 供调用方在 pipeline 结束后批量入库。

    用法::

        with llm_call_collector() as records:
            report = run_pipeline(...)
        save_llm_calls_batch(task_id, records)

    通过 contextvars 自动传播到 asyncio.to_thread 工作线程;OCR 内部 threading.Thread
    需在 _BoundedConcurrency._run 用 ctx.run 显式传播(见各 ocr 引擎)。
    """
    collector: list[LlmCallRecord] = []
    token = current_llm_collector.set(collector)
    try:
        yield collector
    finally:
        current_llm_collector.reset(token)


@contextmanager
def model_call_context(**fields: Any) -> Iterator[None]:
    """为当前模型调用附加仅供审计明细使用的上下文。

    适用于文件/页面/表格索引等不属于 OpenAI 请求体、但排查调用结果时不可缺少的
    业务定位信息。调用方仍应避免放入证件号、金额明细等不必要的原文内容。
    """
    current = current_model_call_context.get()
    merged = dict(current)
    merged.update({key: value for key, value in fields.items() if value is not None})
    token = current_model_call_context.set(merged)
    try:
        yield
    finally:
        current_model_call_context.reset(token)


def _safe_model_value(value: Any) -> Any:
    """保留模型参数语义，但避免把体积巨大的图片 Base64 写入日志/入库。

    将 ``data:image/...;base64,<...>`` 替换为 ``{data_url, base64_chars, sha256}``,
    既保留可识别性(sha256 可用于跨记录比对同一张图),又避免几 MB base64 撑爆 JSONB。
    """
    copied = deepcopy(value)

    def scrub(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: scrub(val) for key, val in item.items()}
        if isinstance(item, list):
            return [scrub(val) for val in item]
        if isinstance(item, str) and item.startswith("data:image/"):
            header, _, encoded = item.partition(",")
            return {
                "data_url": header,
                "base64_chars": len(encoded),
                "sha256": hashlib.sha256(encoded.encode("ascii")).hexdigest(),
            }
        return item

    return scrub(copied)


def _truncate(value: Any, max_chars: int) -> Any:
    """对字符串/JSON 序列化结果截断到 max_chars,加 ``…(truncated)`` 尾标记。"""
    if value is None:
        return None
    if isinstance(value, str):
        if len(value) <= max_chars:
            return value
        return value[:max_chars] + "…(truncated)"
    # dict / list:先序列化测长度,超限则存截断后的 JSON 字符串(避免破坏结构)。
    serialized = json.dumps(value, ensure_ascii=False)
    if len(serialized) <= max_chars:
        return value
    return serialized[:max_chars] + "…(truncated)"


def _model_log_summary(value: Any) -> dict[str, Any]:
    """生成可排障、但不含合同原文的模型请求/响应日志摘要。"""
    serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    summary: dict[str, Any] = {
        "type": type(value).__name__,
        "chars": len(serialized),
        "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
    }
    if not isinstance(value, dict):
        return summary

    summary["keys"] = sorted(str(key) for key in value)[:12]
    # 这些字段均为模型调用控制面信息，不包含提示词、识别文本或合同内容。
    for key in ("model", "max_tokens", "temperature", "page", "page_index"):
        if key in value:
            summary[key] = value[key]
    finish_reason = value.get("finish_reason")
    choices = value.get("choices")
    if not finish_reason and isinstance(choices, list):
        finish_reason = [
            item.get("finish_reason")
            for item in choices
            if isinstance(item, dict) and item.get("finish_reason") is not None
        ]
    if finish_reason:
        summary["finish_reason"] = finish_reason
    return summary


def log_value_summary(value: Any) -> dict[str, Any]:
    """返回适合日志的内容摘要，供模型响应解析失败分支复用。"""
    return _model_log_summary(value)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _diagnostic_audit_value(diagnostic: Any) -> dict[str, Any]:
    """提取不含原文的逐页识别诊断字段。"""
    return {
        key: getattr(diagnostic, key)
        for key in (
            "page_index",
            "source",
            "reliable",
            "reasons",
            "char_count",
            "table_count",
            "location_status",
            "bbox_coverage",
        )
        if hasattr(diagnostic, key)
    }


def _table_shape(table: Any) -> dict[str, int] | None:
    """只记录表格维度，不在摘要中重复保存单元格原文。"""
    if table is None:
        return None
    headers = getattr(table, "headers", []) or []
    rows = getattr(table, "rows", []) or []
    return {
        "header_count": len(headers),
        "row_count": len(rows),
        "max_columns": max(
            [len(headers), *(len(row) for row in rows if isinstance(row, list))],
            default=0,
        ),
    }


def record_ocr_result(
    logger: logging.Logger,
    pages_blocks: list[list[Any]],
    *,
    diagnostics: list[Any] | None = None,
    stage: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """把流水线最终采用的结构化 OCR blocks 写入当前任务的受控审计明细。

    这是解析结果快照，不代表一次额外 HTTP 调用，因此直接创建已完成的
    ``ocr-result`` 记录。常规 app.log 只写计数/字符数，不写内容；任务明细中每块
    保存字符数、SHA-256 和最多 300 字预览，整条记录仍受 64KB 上限保护。
    """
    collector = current_llm_collector.get()
    page_summaries: list[dict[str, Any]] = []
    block_records: list[dict[str, Any]] = []
    total_blocks = sum(len(blocks) for blocks in pages_blocks)
    total_chars = 0
    located_blocks = 0
    if total_blocks <= _MAX_OCR_RESULT_BLOCKS:
        selected_block_offsets = set(range(total_blocks))
    elif _MAX_OCR_RESULT_BLOCKS > 1:
        # 大合同时在整份文档上等距取样，避免“只记录前 100 块”导致第 4 页及后续页
        # 恰好成为诊断盲区。逐页汇总仍覆盖所有页。
        selected_block_offsets = {
            round(index * (total_blocks - 1) / (_MAX_OCR_RESULT_BLOCKS - 1))
            for index in range(_MAX_OCR_RESULT_BLOCKS)
        }
    else:
        selected_block_offsets = {0}
    block_offset = 0

    for outer_page_index, blocks in enumerate(pages_blocks):
        label_counts: dict[str, int] = {}
        page_located = 0
        for block in blocks:
            content = str(getattr(block, "content", "") or "")
            total_chars += len(content)
            label = str(getattr(block, "label", "") or "")
            label_counts[label] = label_counts.get(label, 0) + 1
            bbox = list(getattr(block, "bbox", []) or [])
            if len(bbox) >= 4:
                located_blocks += 1
                page_located += 1
            if block_offset not in selected_block_offsets:
                block_offset += 1
                continue
            preview = content[:_MAX_OCR_BLOCK_PREVIEW_CHARS]
            block_records.append(
                {
                    "outer_page_index": outer_page_index,
                    "block_page_index": getattr(block, "page_index", None),
                    "block_id": getattr(block, "block_id", ""),
                    "label": label,
                    "bbox": bbox,
                    "content_chars": len(content),
                    "content_sha256": _sha256_text(content),
                    "content_preview": preview,
                    "content_truncated": len(preview) < len(content),
                    "table_shape": _table_shape(getattr(block, "table", None)),
                }
            )
            block_offset += 1
        page_summaries.append(
            {
                "page_index": outer_page_index,
                "block_count": len(blocks),
                "located_block_count": page_located,
                "label_counts": label_counts,
            }
        )

    omitted_blocks = max(0, total_blocks - len(block_records))
    logger.info(
        "ocr parsed result stage=%s pages=%s blocks=%s located=%s chars=%s omitted=%s",
        stage,
        len(pages_blocks),
        total_blocks,
        located_blocks,
        total_chars,
        omitted_blocks,
    )
    if collector is None:
        return

    payload = {
        "operation": "final_ocr_blocks",
        "stage": stage,
        "preview_policy": {
            "max_blocks": _MAX_OCR_RESULT_BLOCKS,
            "max_chars_per_block": _MAX_OCR_BLOCK_PREVIEW_CHARS,
            "max_record_chars": _MAX_RESPONSE_CHARS,
        },
    }
    if metadata:
        payload["metadata"] = _safe_model_value(metadata)
    response = {
        "summary": {
            "page_count": len(pages_blocks),
            "block_count": total_blocks,
            "located_block_count": located_blocks,
            "content_chars": total_chars,
            "recorded_block_count": len(block_records),
            "omitted_block_count": omitted_blocks,
        },
        "pages": page_summaries,
        "diagnostics": [
            _diagnostic_audit_value(item) for item in (diagnostics or [])
        ],
        "blocks": block_records,
    }
    collector.append(
        LlmCallRecord(
            kind="ocr-result",
            attempt=1,
            payload=payload,
            status_code=200,
            elapsed_ms=0,
            response=_truncate(response, _MAX_RESPONSE_CHARS),
        )
    )


def record_ocr_text_result(
    logger: logging.Logger,
    text: str,
    *,
    diagnostic: Any = None,
    stage: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """记录无结构化 blocks 的最终 PDF 读取文本(原生文本层或整篇 OCR)。"""
    content = text or ""
    logger.info(
        "ocr parsed text result stage=%s chars=%s sha256=%s",
        stage,
        len(content),
        _sha256_text(content),
    )
    collector = current_llm_collector.get()
    if collector is None:
        return
    payload = {
        "operation": "final_ocr_text",
        "stage": stage,
        "preview_policy": {
            "max_chars": _MAX_OCR_TEXT_PREVIEW_CHARS,
            "max_record_chars": _MAX_RESPONSE_CHARS,
        },
    }
    if metadata:
        payload["metadata"] = _safe_model_value(metadata)
    preview = content[:_MAX_OCR_TEXT_PREVIEW_CHARS]
    response = {
        "summary": {
            "content_chars": len(content),
            "content_sha256": _sha256_text(content),
            "content_truncated": len(preview) < len(content),
        },
        "diagnostic": (
            _diagnostic_audit_value(diagnostic) if diagnostic is not None else None
        ),
        "content_preview": preview,
    }
    collector.append(
        LlmCallRecord(
            kind="ocr-result",
            attempt=1,
            payload=payload,
            status_code=200,
            elapsed_ms=0,
            response=_truncate(response, _MAX_RESPONSE_CHARS),
        )
    )


def log_model_request(
    logger: logging.Logger, kind: str, url: str, payload: dict[str, Any], attempt: int
) -> float:
    """记录不含鉴权头的模型请求,返回计时起点(perf_counter)。

    若当前 context 设了收集器且 kind 属于对话型白名单,同时 append 一条
    LlmCallRecord(此时只填了 kind/attempt/payload/created_at,待 response/failure 补全)。
    """
    safe_payload = _safe_model_value(payload)
    logger.info(
        "model request kind=%s attempt=%s url=%s summary=%s",
        kind,
        attempt,
        url,
        json.dumps(_model_log_summary(safe_payload), ensure_ascii=False, separators=(",", ":")),
    )
    collector = current_llm_collector.get()
    if collector is not None and kind in _COLLECTED_KINDS:
        # URL 和业务定位信息不是发给模型的参数，但与脱敏请求体一起持久化，便于
        # 按一次具体调用复现问题。常规文件日志仍只写上面的安全摘要。
        record_payload = dict(safe_payload)
        record_payload["_request"] = {"url": url}
        trace_context = current_model_call_context.get()
        if trace_context:
            record_payload["_trace"] = _safe_model_value(trace_context)
        collector.append(
            LlmCallRecord(kind=kind, attempt=attempt, payload=record_payload)
        )
    return time.perf_counter()


def log_model_response(
    logger: logging.Logger,
    kind: str,
    status_code: int,
    value: Any,
    started_at: float,
) -> None:
    """记录模型原始返回值与单次 HTTP 调用耗时,并补全最近一条收集器记录。

    按 kind 匹配收集器中**最后一条同 kind 且未补全**(status_code is None)的记录。
    同 kind 在并发(逐页 OCR 多线程)下会有多条未补全记录,这里取最后一条匹配;
    由于 contextvar 在每个工作线程独立 copy,同一线程内同 kind 未补全记录唯一。
    """
    elapsed = time.perf_counter() - started_at
    safe_value = _safe_model_value(value)
    logger.info(
        "model response kind=%s status=%s elapsed=%.3fs summary=%s",
        kind,
        status_code,
        elapsed,
        json.dumps(_model_log_summary(safe_value), ensure_ascii=False, separators=(",", ":")),
    )
    _finalize_record(kind, status_code=status_code, elapsed_s=elapsed,
                     response=_truncate(safe_value, _MAX_RESPONSE_CHARS), error=None)


def log_model_failure(
    logger: logging.Logger,
    kind: str,
    started_at: float,
    error: str,
    *,
    status_code: int | None = None,
    response: Any = None,
) -> None:
    """记录模型调用失败(TransportError / 4xx / 重试耗尽),补全最近一条收集器记录。

    status_code 为 None 表示连接级失败(超时/网络);4xx/5xx 传实际状态码。
    error 字符串截断到 _MAX_ERROR_CHARS(对齐 ORM 列宽)。
    """
    elapsed = time.perf_counter() - started_at
    safe_response = _safe_model_value(response)
    logger.warning(
        "model failure kind=%s status=%s elapsed=%.3fs error=%s response_summary=%s",
        kind,
        status_code,
        elapsed,
        error[:_MAX_ERROR_CHARS],
        json.dumps(_model_log_summary(safe_response), ensure_ascii=False, separators=(",", ":")),
    )
    _finalize_record(kind, status_code=status_code, elapsed_s=elapsed,
                     response=_truncate(safe_response, _MAX_RESPONSE_CHARS),
                     error=error[:_MAX_ERROR_CHARS])


def _finalize_record(
    kind: str,
    *,
    status_code: int | None,
    elapsed_s: float,
    response: Any,
    error: str | None,
) -> None:
    """找到收集器中最后一条同 kind 且未补全的记录,填入结果字段。

    无收集器或找不到匹配记录时静默返回(如独立脚本、单测直接调 log_model_response)。
    """
    collector = current_llm_collector.get()
    if collector is None:
        return
    # 倒序找最后一条同 kind 且 status_code 未填的记录。
    for rec in reversed(collector):
        if rec.kind == kind and rec.status_code is None:
            rec.status_code = status_code
            rec.elapsed_ms = int(round(elapsed_s * 1000))
            rec.response = response
            rec.error = error
            return


@contextmanager
def timed_stage(logger: logging.Logger, stage: str, **fields: Any) -> Iterator[None]:
    """统一记录阶段开始、成功耗时与异常耗时。"""
    suffix = " ".join(f"{key}={value}" for key, value in fields.items())
    logger.info("stage start stage=%s%s", stage, f" {suffix}" if suffix else "")
    started_at = time.perf_counter()
    try:
        yield
    except Exception:
        logger.exception(
            "stage failed stage=%s elapsed=%.3fs%s",
            stage,
            time.perf_counter() - started_at,
            f" {suffix}" if suffix else "",
        )
        raise
    logger.info(
        "stage done stage=%s elapsed=%.3fs%s",
        stage,
        time.perf_counter() - started_at,
        f" {suffix}" if suffix else "",
    )
