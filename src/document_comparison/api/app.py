"""FastAPI 应用:比对端点 + 对外集成(§7、§14)。

端点:
  POST /api/v1/compare              提交(支持 callback_url,可选)
  GET  /api/v1/compare/{task_id}    查询结果
  GET  /api/v1/compare/{task_id}/events   SSE 进度(§7.3)
  GET  /api/v1/compare/{task_id}/report   下载报告(json/pdf/html)
  GET  /api/v1/compare/{task_id}/source   获取源 PDF 预览
  GET  /health
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from datetime import date, datetime

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..config import (
    _maybe_import_legacy_llm_config_file,
    apply_llm_overrides,
    ensure_llm_config_fresh,
    load_llm_overrides,
    save_llm_overrides,
    settings,
)
from .. import db as db_pkg
from ..db import repository as db_repo
from ..logging_config import setup_logging
from ..observability import log_context
from ..http_security import (
    apply_security_headers,
    safe_static_file_path,
    scan_probe_reason,
)
from ..models import (
    CompareOptions,
    RawCompareOptions,
    StatementOptions,
    StatementSummaryReport,
    TamperReport,
    TextDiffReport,
)
from ..parsing.pdf import count_pages_from_bytes
from ..storage import (
    download_to_upload,
    effective_target_path,
    finalize_temp_file,
    rendered_source_pdf_path,
    save_upload,
    upload_path,
)
from ..report.builder import burn_pdf
from ..compare.llm_diff import DEFAULT_DIFF_SYSTEM_PROMPT
from ..llm_protocol import VALID_PROTOCOLS
from .. import webhook
from ..console_auth import (
    COOKIE_NAME,
    clear_login_failures,
    console_auth_enabled,
    console_auth_required,
    issue_session_token,
    login_rate_limited,
    record_login_failure,
    verify_console_password,
    verify_session_token,
)
from ..external_api import (
    build_external_result,
    build_external_statement_result,
    external_config_enabled,
    external_html_report_path,
    external_image_path,
    external_pdf_report_path,
    external_report_filename,
    render_external_highlight_images,
    require_external_api_key,
    validate_callback_url,
    validate_public_base_url,
    write_external_html_report,
    write_external_pdf_report,
)
from ..tasks import task_manager

# python-multipart 低层解析 API(与 starlette.formparsers 同款 import 兼容);
# 仅用于外部审计的表单字段预提取,缺库时该兜底自动失效。
try:
    import python_multipart as multipart
    from python_multipart.multipart import parse_options_header
except ImportError:  # pragma: no cover
    try:
        import multipart
        from multipart.multipart import parse_options_header
    except ImportError:
        multipart = None  # type: ignore[assignment]
        parse_options_header = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def _upload_size(upload: UploadFile) -> int:
    """读取底层临时文件大小并回拨，不把整个文件复制进内存。"""
    current = upload.file.tell()
    upload.file.seek(0, 2)
    size = upload.file.tell()
    upload.file.seek(current)
    return size


def _normalize_target_urls(values: list[str]) -> list[str]:
    """兼容 multipart 的重复 URL 字段与单个 JSON URL 数组字段。"""
    normalized: list[str] = []
    for raw_value in values:
        candidate = (raw_value or "").strip()
        if not candidate:
            continue
        if candidate.startswith("["):
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError as exc:
                raise ValueError("target_urls JSON 数组解析失败") from exc
            if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
                raise ValueError("target_urls 必须是字符串数组或重复 URL 字段")
            normalized.extend(item.strip() for item in parsed if item.strip())
        else:
            normalized.append(candidate)
    return normalized


# 单据类型归一化表:金额统计(amountStat)任务的发票/对帐单细分。
# 对外契约(字符串枚举):"1"=发票、"2"=对帐单;不传默认 "1"(发票)。
# 兼容中文别名,归一后统一存 "1"/"2"。
_STATEMENT_DOCUMENT_TYPE_ALIASES = {
    "1": "1",
    "发票": "1",
    "2": "2",
    "对帐单": "2",
    "对账单": "2",
}


def _normalize_document_type(value: str | None) -> str:
    """归一化单据类型为 "1"(发票)/ "2"(对帐单)。

    空值默认 "1"(发票);支持中文别名(发票/对帐单/对账单);
    无法识别 → ValueError(端点转 400)。
    """
    candidate = (value or "").strip()
    if not candidate:
        return "1"
    mapped = _STATEMENT_DOCUMENT_TYPE_ALIASES.get(candidate)
    if mapped is None:
        raise ValueError("document_type 必须为 1(发票)或 2(对帐单)")
    return mapped


def _persist_role(
    task_id: str,
    role: str,
    upload: UploadFile | None,
    temp_path: Path | None,
    filename: str,
) -> Path:
    """落定单个角色的最终文件:UploadFile → save_upload;URL 临时文件 → finalize_temp_file。

    外部接口 source/target 允许"文件"或"URL"二选一,二者分派由 `upload` 是否为 None 决定。
    """
    if upload is not None and upload.filename:
        return save_upload(upload, task_id, role)
    assert temp_path is not None, "URL 模式下 temp_path 必须已下载"
    return finalize_temp_file(temp_path, task_id, role, filename)



def _mask_key(key: str) -> str:
    """API Key 脱敏返回:只露末 4 位,供前端判断是否已设置。"""
    if not key:
        return ""
    if len(key) <= 4:
        return "****"
    return "*" * (len(key) - 4) + key[-4:]


def _safe_persisted_config() -> dict:
    """返回可供管理端展示的配置快照，不泄露任何 API Key 明文。"""
    persisted = load_llm_overrides()
    for key in tuple(persisted):
        if key.endswith(("_api_key", "_access_token")):
            persisted[key] = _mask_key(str(persisted[key]))
    return persisted


def _extract_client_ip(request) -> str | None:
    """解析调用方真实 IP:优先 X-Forwarded-For[0],次 X-Real-IP,末用 client.host。"""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # 取链路最左(最原始调用方);剥空白
        first = forwarded.split(",", 1)[0].strip()
        if first:
            return first[:64]
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()[:64]
    client = getattr(request, "client", None)
    if client and client.host:
        return client.host[:64]
    return None


def _api_key_fingerprint(raw_key: str | None) -> str | None:
    """X-API-Key 的 sha256 十六进制指纹(64 字符);空 key 返回 None。不存明文。"""
    if not raw_key:
        return None
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


# 审计参数快照中单个字符串值的上限;超长 URL / 文件名截断,防止撑爆 JSONB
_AUDIT_PARAM_MAX_STR = 512


def _sanitize_audit_params(params: Any) -> dict[str, Any] | None:
    """清洗端点写入的 request.state.audit_params:敏感 key 脱敏 + 长字符串截断。

    仅接受 dict(其它类型记 None)。端点只记文件名不记文件内容,此处截断是
    防御性兜底,避免异常超长的 URL / 文件名进入审计记录。
    """
    if not isinstance(params, dict):
        return None

    def _short(value: Any) -> Any:
        if isinstance(value, str) and len(value) > _AUDIT_PARAM_MAX_STR:
            return value[:_AUDIT_PARAM_MAX_STR] + "…(truncated)"
        if isinstance(value, list):
            return [_short(item) for item in value]
        if isinstance(value, dict):
            return {str(k): _short(v) for k, v in value.items()}
        return value

    return _short(_mask_sensitive(params))


# 预解析阶段单个非文件字段的字节上限(防恶意超长字段;落库前还有 512 字符截断)
_PREFILL_FIELD_MAX_BYTES = 64 * 1024


class _MultipartFieldCollector:
    """python-multipart 回调收集器:只保留非文件字段值与文件字段名,丢弃文件内容。

    与端点的结构化快照口径一致——文件只记文件名;同名重复字段(如 amountStat
    的多个 target_urls)聚合成 list。字段值超上限截断,文件数据不缓冲。
    """

    def __init__(self) -> None:
        self.fields: dict[str, Any] = {}
        self._field_name: str | None = None
        self._filename: str | None = None
        self._buf = bytearray()
        self._hdr_name = b""
        self._hdr_value = b""
        self._content_disposition = b""

    def on_part_begin(self) -> None:
        self._field_name = None
        self._filename = None
        self._buf = bytearray()

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._filename is not None or self._field_name is None:
            return  # 文件内容直接丢弃
        chunk = data[start:end]
        room = _PREFILL_FIELD_MAX_BYTES - len(self._buf)
        if room > 0:
            self._buf += chunk[:room]

    def on_part_end(self) -> None:
        if self._field_name is None:
            return
        value = (
            self._filename
            if self._filename is not None
            else self._buf.decode("utf-8", errors="replace")
        )
        existing = self.fields.get(self._field_name)
        if existing is None:
            self.fields[self._field_name] = value
        elif isinstance(existing, list):
            existing.append(value)
        else:
            self.fields[self._field_name] = [existing, value]

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._hdr_name += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._hdr_value += data[start:end]

    def on_header_end(self) -> None:
        if self._hdr_name.lower() == b"content-disposition":
            self._content_disposition = self._hdr_value
        self._hdr_name = b""
        self._hdr_value = b""

    def on_headers_finished(self) -> None:
        if parse_options_header is None or not self._content_disposition:
            return
        _, options = parse_options_header(self._content_disposition)
        name = options.get(b"name")
        if name is not None:
            self._field_name = name.decode("utf-8", errors="replace")
        filename = options.get(b"filename")
        if filename is not None:
            self._filename = filename.decode("utf-8", errors="replace")

    def on_end(self) -> None:
        pass


def _extract_multipart_fields(body: bytes, content_type: str) -> dict[str, Any] | None:
    """从完整 multipart body 提取非文件字段与文件名;解析失败返回 None 不抛。"""
    if multipart is None or parse_options_header is None:
        return None
    try:
        _, params = parse_options_header(content_type.encode("latin-1", errors="replace"))
        boundary = params[b"boundary"]
    except Exception:  # noqa: BLE001
        return None
    collector = _MultipartFieldCollector()
    callbacks = {
        "on_part_begin": collector.on_part_begin,
        "on_part_data": collector.on_part_data,
        "on_part_end": collector.on_part_end,
        "on_header_field": collector.on_header_field,
        "on_header_value": collector.on_header_value,
        "on_header_end": collector.on_header_end,
        "on_headers_finished": collector.on_headers_finished,
        "on_end": collector.on_end,
    }
    try:
        parser = multipart.MultipartParser(boundary, callbacks)
        parser.write(body)
        parser.finalize()
    except Exception:  # noqa: BLE001
        return None
    return collector.fields or None


async def _prefill_external_audit_params(request: Request) -> None:
    """外部端点执行前预解析表单字段,兜底 401/422 失败拦截的请求参数记录。

    401(X-API-Key 鉴权依赖)与 422(FastAPI 参数校验)发生在端点体之前,
    端点入口写的 audit_params 不会执行;这里在中间件层先把调用方原始提交的
    非文件字段 + 文件名写入 request.state.audit_params,端点体执行时再覆盖为
    结构化快照。因此成功/400/413/429 请求记录的仍是端点的结构化版本。

    必须整体 ``await request.body()``:Starlette BaseHTTPMiddleware 的
    _CachedRequest 只在 body() 时缓存供下游重放;stream() 会给下游发空 body,
    破坏 FastAPI 表单解析。防内存放大:Content-Length 缺失(chunked 流式)或
    超过外部上传上限的请求跳过预读(正常表单提交均携带 Content-Length)。
    """
    ct = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" not in ct:
        return
    if getattr(request.state, "audit_params", None):
        return
    raw_len = _parse_content_length(request.headers.get("content-length"))
    limit = max(settings.external_max_upload_mb, 1) * 1024 * 1024
    if raw_len is None or raw_len > limit:
        return
    try:
        fields = _extract_multipart_fields(await request.body(), ct)
        if fields:
            request.state.audit_params = fields
    except Exception:  # noqa: BLE001
        logger.debug("prefill external audit params failed", exc_info=True)


# 外部接口审计:按 HTTP 状态码映射固定的失败摘要,便于审计筛选。
_EXTERNAL_ERROR_BY_STATUS: dict[int, str] = {
    400: "bad request",
    401: "invalid external API key",
    403: "forbidden",
    404: "not found",
    413: "payload too large",
    422: "validation error",
    429: "too many requests",
    500: "internal error",
    503: "external api disabled",
}


async def _persist_external_call(*, request, status_code: int, elapsed_ms: int) -> None:
    """异步写一条外部接口审计记录,绝不阻塞响应。

    从 request.state 读取端点设置的 audit_endpoint / audit_task_id / audit_document_no
    (401/422 等端点未执行场景下为 None)与 audit_params(请求参数快照:端点入口
    写入结构化版本;401/422 端点体未执行时为中间件预读的原始表单字段,超大或
    chunked 请求可能为 None)。写库失败只记日志(遵循 task_records 双写规则)。
    """
    endpoint = getattr(request.state, "audit_endpoint", None) or "unknown"
    task_id = getattr(request.state, "audit_task_id", None)
    document_no = getattr(request.state, "audit_document_no", None)
    request_id = getattr(request.state, "request_id", "") or uuid.uuid4().hex[:12]
    try:
        await asyncio.to_thread(
            db_repo.save_external_call,
            task_id=task_id,
            endpoint=endpoint,
            method=request.method,
            document_no=document_no,
            client_ip=_extract_client_ip(request),
            api_key_sha256=_api_key_fingerprint(request.headers.get("x-api-key")),
            status_code=status_code,
            elapsed_ms=elapsed_ms,
            error=getattr(request.state, "audit_error", None) or _EXTERNAL_ERROR_BY_STATUS.get(status_code),
            request_id=request_id,
            content_length=_parse_content_length(request.headers.get("content-length")),
            request_params=_sanitize_audit_params(
                getattr(request.state, "audit_params", None)
            ),
        )
    except Exception:  # noqa: BLE001
        from ..deployment import record_persistence_error
        record_persistence_error()
        logger.exception(
            "persist_external_call failed: endpoint=%s status=%s request_id=%s",
            endpoint, status_code, request_id,
        )


def _parse_content_length(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


# —— 入站 API access log ——
# 只对 /api/ 与 /health 前缀生效,记录 method/path/status/耗时,并按上限记录完整请求体
# 与响应体(合同原文、金额等业务内容会进入本地滚动日志 storage_dir/logs/app.log)。
# multipart 文件上传、SSE 流、文件下载按摘要跳过 body;JSON 内容对鉴权类字段脱敏。

# 命中即脱敏的 key(小写包含匹配),避免 API Key / 密码进入 access log。
_SENSITIVE_KEY_FRAGMENTS = ("password", "api_key", "apikey", "token", "secret", "authorization")


def _mask_sensitive(value: Any) -> Any:
    """递归把 dict 中敏感 key 的值替换为 ``***``(不处理合同正文/金额等业务字段)。

    list / dict 递归;其它类型原样返回。仅作用于 JSON 解析成功的内容。
    """
    if isinstance(value, dict):
        masked: dict[str, Any] = {}
        for key, val in value.items():
            key_lower = str(key).lower()
            if any(frag in key_lower for frag in _SENSITIVE_KEY_FRAGMENTS):
                masked[key] = "***"
            else:
                masked[key] = _mask_sensitive(val)
        return masked
    if isinstance(value, list):
        return [_mask_sensitive(item) for item in value]
    return value


def _coerce_log_body(raw: bytes | None, content_type: str | None, *, max_chars: int) -> str:
    """把请求/响应字节渲染成可在日志中展示的字符串。

    - application/json:解析后脱敏再序列化(失败回退为文本)。
    - 其它 text/* 或 JSON 解析失败:UTF-8 解码(errors=replace)。
    - 空体:``<empty>``。
    超过 ``max_chars`` 截断并加 ``…(truncated)``。
    """
    if not raw:
        return "<empty>"
    ct = (content_type or "").lower()
    if "json" in ct:
        try:
            parsed = json.loads(raw)
            text = json.dumps(_mask_sensitive(parsed), ensure_ascii=False)
        except (ValueError, TypeError):
            text = raw.decode("utf-8", errors="replace")
    else:
        text = raw.decode("utf-8", errors="replace")
    if len(text) > max_chars:
        return text[:max_chars] + "…(truncated)"
    return text


class ApiAccessLogMiddleware:
    """纯 ASGI 入站请求/响应日志中间件(access log)。

    为什么不用 ``@app.middleware("http")``(BaseHTTPMiddleware):Starlette 1.x 的
    BaseHTTPMiddleware 会把所有下游响应(包括普通 dict handler)重包成
    ``_StreamingResponse``,其 ``body`` 在中间件返回时不可读,无法捕获响应体。
    纯 ASGI 中间件直接拦截 ``receive``/``send`` 通道,能可靠抓取请求体与响应体块。

    仅对 ``/api/`` 与 ``/health`` 前缀生效;记录 method/path/query/status/耗时,并按
    ``DC_API_LOG_MAX_BODY`` 记录完整请求体与响应体(鉴权类敏感字段脱敏)。边界:
    multipart 文件上传只记摘要;SSE(text/event-stream)与文件下载不缓存响应体。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not settings.api_request_logging:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        # 只覆盖业务前缀;静态资源、SPA fallback、探测路径零开销透传。
        if not (path.startswith("/api/") or path == "/health"):
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        query = scope.get("query_string", b"").decode("latin-1", errors="replace")
        headers = _headers_to_dict(scope)
        ct = (headers.get("content-type") or "").lower()
        max_body = settings.api_log_max_body
        request_id = (headers.get("x-request-id") or "-")
        is_multipart = "multipart/form-data" in ct

        # —— 请求体:tee receive,记录后原样转发给下游 ——
        request_chunks: list[bytes] = []

        async def receive_tee():
            message = await receive()
            if message["type"] == "http.request" and not is_multipart:
                body = message.get("body", b"")
                if body:
                    request_chunks.append(body)
            return message

        start = time.perf_counter()
        status_code = 500
        resp_ct = ""
        resp_body_buf: list[bytes] = []
        skip_resp_body = False

        async def send_tee(message):
            nonlocal status_code, resp_ct, skip_resp_body
            if message["type"] == "http.response.start":
                status_code = message.get("status", 500)
                resp_headers = _headers_to_dict(message.get("headers", []))
                resp_ct = (resp_headers.get("content-type") or "").lower()
                # SSE 流与文件响应不缓存 body(前者会消费殆尽,后者可能很大)。
                if "text/event-stream" in resp_ct:
                    skip_resp_body = True
            elif message["type"] == "http.response.body":
                if not skip_resp_body:
                    chunk = message.get("body", b"")
                    if chunk:
                        resp_body_buf.append(chunk)
            await send(message)

        try:
            await self.app(scope, receive_tee, send_tee)
        finally:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            # —— 请求体日志 ——
            if is_multipart:
                request_body = (
                    f"<multipart, content_length={_parse_content_length(headers.get('content-length'))}>"
                )
            else:
                request_body = _coerce_log_body(b"".join(request_chunks), ct, max_chars=max_body)
            logger.info(
                "api request method=%s path=%s query=%s request_id=%s body=%s",
                method, path, query or "-", request_id, request_body,
            )
            # —— 响应体日志 ——
            if skip_resp_body:
                resp_body = "<sse stream, skipped>"
            elif not resp_ct or (
                "json" not in resp_ct and not resp_ct.startswith("text/")
            ):
                resp_body = f"<binary, content_type={resp_ct or 'unknown'}>"
            else:
                resp_body = _coerce_log_body(b"".join(resp_body_buf), resp_ct, max_chars=max_body)
            logger.info(
                "api response method=%s path=%s status=%s elapsed_ms=%s request_id=%s body=%s",
                method, path, status_code, elapsed_ms, request_id, resp_body,
            )


def _headers_to_dict(scope_or_raw) -> dict[str, str]:
    """从 ASGI scope/message 的 raw headers(小写键)构造普通 dict。

    入参可以是 scope/message dict(取其 ``headers``),也可以直接是 raw headers 列表
    (list[(bytes, bytes)]),两者都兼容。
    """
    if isinstance(scope_or_raw, dict):
        raw = scope_or_raw.get("headers")
    else:
        raw = scope_or_raw
    if not raw:
        return {}
    out: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        key = item[0].decode("latin-1", errors="replace").lower()
        val = item[1].decode("latin-1", errors="replace")
        if key:
            out[key] = val
    return out


def _task_record_to_task_info_dict(rec, *, kind: str) -> dict:
    """把 PG TaskRecord 还原成 TaskInfo.model_dump 形状,附加对应报告字段。

    用于进程重启后内存 Task 丢失时,查询端点从 PG 回兜的响应构造。
    字段与 TaskInfo 对齐,前端 store.applyInfo 无感消费。
    """
    base = {
        "task_id": rec.task_id,
        "status": rec.status,
        "stage": "",        # 历史 stage 不还原(避免误导前端进度条)
        "progress": 1.0 if rec.status == "done" else 0.0,
        "stage_timings": dict(rec.stage_timings or {}),
        "overall_risk": rec.overall_risk,
        "error": rec.error,
        "elapsed": rec.elapsed,
    }
    if kind == "compare" and rec.report_compare is not None:
        base["report"] = TamperReport.model_validate(rec.report_compare).model_dump()
    elif kind == "raw" and rec.report_raw is not None:
        base["raw_report"] = rec.report_raw
    elif kind == "statement" and rec.report_statement is not None:
        base["statement_report"] = rec.report_statement
    return base


def _attachment_file_response(path: Path, media_type: str, filename: str) -> FileResponse:
    """attachment 下载响应;RFC 5987:中文文件名用 filename*=UTF-8'' 编码。"""
    from urllib.parse import quote

    return FileResponse(
        path,
        media_type=media_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
        },
    )


async def _external_task_response(task_id: str) -> dict:
    """构造外部任务状态响应，供正式查询和管理端管线测试复用。"""
    task = task_manager.get(task_id)
    report = None
    if task is not None:
        if not task.external_request:
            raise HTTPException(404, "task not found")
        document_no = task.document_no or ""
        status = task.info.status
        response = {
            "task_id": task_id,
            "document_no": document_no,
            "status": status,
            "stage": task.info.stage,
            "progress": task.info.progress,
            "error": task.info.error,
        }
        report = task.report
    else:
        rec = await asyncio.to_thread(db_repo.get_task, task_id)
        if rec is None or not rec.external_request:
            raise HTTPException(404, "task not found")
        document_no = rec.document_no or ""
        status = rec.status
        response = {
            "task_id": task_id,
            "document_no": document_no,
            "status": status,
            "stage": "",
            "progress": 1.0 if status == "done" else 0.0,
            "error": rec.error,
        }
        if rec.report_compare is not None:
            report = TamperReport.model_validate(rec.report_compare)

    if status == "done" and report is not None:
        external = build_external_result(task_id, document_no, report)
        response.update(external)
    return response


async def _external_statement_response(task_id: str) -> dict:
    """构造金额统计外部任务状态响应,供外部查询端点复用。

    与 `_external_task_response` 思路一致:优先读内存 Task,缺失回兜 PG。
    只放行 `external_request=True` 的 statement 任务(内部 statement 任务 → 404,隔离)。
    done 时追加 `build_external_statement_result` 输出的扁平结果字段。
    """
    task = task_manager.get(task_id)
    report: StatementSummaryReport | None = None
    if task is not None:
        if not task.external_request or task.kind != "statement":
            raise HTTPException(404, "task not found")
        document_no = task.document_no or ""
        status = task.info.status
        response = {
            "task_id": task_id,
            "document_no": document_no,
            "document_type": task.document_type,
            "status": status,
            "stage": task.info.stage,
            "progress": task.info.progress,
            "error": task.info.error,
        }
        report = task.statement_report
    else:
        rec = await asyncio.to_thread(db_repo.get_task, task_id)
        if rec is None or not rec.external_request or rec.kind != "statement":
            raise HTTPException(404, "task not found")
        document_no = rec.document_no or ""
        status = rec.status
        response = {
            "task_id": task_id,
            "document_no": document_no,
            "document_type": rec.document_type,
            "status": status,
            "stage": "",
            "progress": 1.0 if status == "done" else 0.0,
            "error": rec.error,
        }
        if rec.report_statement is not None:
            report = StatementSummaryReport.model_validate(rec.report_statement)

    if status == "done" and report is not None:
        external = build_external_statement_result(
            task_id, document_no, report, document_type=response["document_type"]
        )
        response.update(external)
    return response


def create_app() -> FastAPI:
    setup_logging()  # 控制台 + 滚动文件,幂等
    # PG 硬依赖:初始化引擎 + 健康检查。失败即抛错,让 uvicorn 拒绝启动。
    db_pkg.init_engine()
    db_pkg.check_connection()
    # schema 就位:测试用 db_auto_create 直接 create_all;默认走 alembic upgrade head。
    # 两者都失败抛错,避免运行时查询时才发现表缺失(曾导致 500 internal error)。
    if settings.db_auto_create:
        db_pkg.create_all()
    elif settings.db_auto_migrate:
        db_pkg.run_migrations()
    from ..db import releases
    from ..deployment import DeploymentAdmissionMiddleware, check_readiness
    releases.register(releases.DEPLOYMENT_ID, settings.version, "SERVING")
    # LLM 配置持久化迁移:首次启动若 PG 无记录且本地遗留 llm_config.json 存在,
    # 把文件导入 PG(文件保留作备份),再把 PG 配置应用到运行时 settings 单例。
    if releases.status(releases.DEPLOYMENT_ID)["mode"] == "SERVING":
        _maybe_import_legacy_llm_config_file()
    apply_llm_overrides()
    audit_futures: set[asyncio.Task] = set()
    @asynccontextmanager
    async def _queue_lifespan(_app: FastAPI):
        await task_manager.start_queue_dispatcher()
        try:
            yield
        finally:
            await task_manager.stop_queue_dispatcher()
            if audit_futures:
                await asyncio.gather(*list(audit_futures), return_exceptions=True)

    app = FastAPI(
       title="文档比对 API",
        description="基于多模态 LLM API 的合同条款篡改检测(§7、§14)",
       version=settings.version,
       lifespan=_queue_lifespan,
    )

    @app.middleware("http")
    async def _external_audit_middleware(request, call_next):
        """外部接口入站请求审计中间件。

        仅对 `/api/v1/external/` 前缀生效:统一生成 request_id(写入 request.state
        与响应头 X-Request-Id),测量耗时,在请求结束时异步落库一条审计记录。
        覆盖成功(202/200)与失败(401 鉴权 / 422 校验 / 413 / 429 等)全链路。
        非外部前缀请求零开销透传。
        """
        path = request.url.path
        if not path.startswith("/api/v1/external/"):
            return await call_next(request)

        request.state.request_id = uuid.uuid4().hex[:12]
        with log_context(request_id=request.state.request_id):
            # 失败拦截兜底:预解析表单字段——401/422 发生在端点体之前,
            # 预读的原始字段让这些请求的审计也能携带请求参数。
            if request.method == "POST":
                await _prefill_external_audit_params(request)
            start = time.perf_counter()
            status_code = 500
            try:
                response = await call_next(request)
                status_code = response.status_code
                response.headers["X-Request-Id"] = request.state.request_id
                return response
            except Exception:
                # Starlette 兜底会转成 500;这里记审计后重新抛出交全局 handler
                status_code = 500
                raise
            finally:
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                logger.info(
                    "external request completed endpoint=%s method=%s status=%s elapsed_ms=%s",
                    getattr(request.state, "audit_endpoint", None) or "unknown",
                    request.method, status_code, elapsed_ms,
                )
                # 异步落库,不阻塞响应返回;异常已在 _persist_external_call 内吞并
                future = asyncio.create_task(_persist_external_call(
                    request=request,
                    status_code=status_code,
                    elapsed_ms=elapsed_ms,
                ))
                audit_futures.add(future)
                future.add_done_callback(audit_futures.discard)
                if "release_background" in request.scope:
                    request.scope["release_background"].append(future)

    async def _audit_maintenance_rejection(scope):
        # 发布屏障在 multipart 预读之前；拒绝请求不读取上传内容，但仍写外部审计。
        request = Request(scope)
        request.state.audit_error = "deployment maintenance or control unavailable"
        request.state.audit_endpoint = {
            "/api/v1/external/contractCompare": "contractCompare.submit",
            "/api/v1/external/amountStat": "amountStat.submit",
        }.get(scope["path"], "unknown")
        await _persist_external_call(request=request, status_code=503, elapsed_ms=0)

    app.add_middleware(
        DeploymentAdmissionMiddleware, owner=task_manager._queue_worker_id,
        on_rejected=_audit_maintenance_rejection,
    )
    # CORS 在发布屏障外层，跨域调用方也能读取维护响应及 Retry-After。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Retry-After", "X-Request-Id"],
    )

    @app.middleware("http")
    async def _console_auth_middleware(request, call_next):
        """控制台口令鉴权中间件(DC_CONSOLE_PASSWORD 非空时生效)。

        注册在 scan_protection 之内、access log 之外:被拒的 401 会进 access log,
        也能带上 scan_protection 补充的安全响应头。豁免名单见
        console_auth.console_auth_required;会话校验为无状态 HMAC,多 worker 免共享。
        """
        if not console_auth_enabled() or not console_auth_required(request.url.path):
            return await call_next(request)
        if not verify_session_token(request.cookies.get(COOKIE_NAME, "")):
            return JSONResponse(
                status_code=401,
                content={"code": 401, "message": "console authentication required"},
            )
        return await call_next(request)

    @app.middleware("http")
    async def _scan_protection_middleware(request, call_next):
        """在路由前拒绝常见漏洞扫描请求，并为所有响应补充安全头。

        不记录原始探测路径或来源地址，避免攻击载荷和不必要的个人数据进入日志。
        中间件置于外部接口审计层之外，因此被拦截的请求也不会被当作业务调用落库。
        """
        raw_path = request.scope.get("raw_path", b"")
        if isinstance(raw_path, bytes):
            raw_path_text = raw_path.decode("latin-1", errors="replace")
        else:
            raw_path_text = str(raw_path)
        reason = (
            scan_probe_reason(method=request.method, raw_path=raw_path_text)
            if settings.scan_protection_enabled
            else None
        )
        if reason:
            logger.warning(
                "blocked suspicious request method=%s category=%s", request.method, reason
            )
            response = JSONResponse(
                status_code=404, content={"code": 404, "message": "not found"}
            )
        else:
            response = await call_next(request)

        if settings.security_headers_enabled:
            apply_security_headers(response.headers)
        return response

    # access log 必须作为最外层之一:在 scan_protection/外部审计之前看到原始请求体,
    # 并能捕获异常处理器产出的响应。add_middleware 后注册=最外层。
    app.add_middleware(ApiAccessLogMiddleware)

    @app.exception_handler(HTTPException)
    async def _http_exc_handler(request, exc: HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": exc.status_code,
                "message": exc.detail,
                "request_id": getattr(request.state, "request_id", None) or uuid.uuid4().hex[:12],
            },
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exc_handler(request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "code": 422,
                "message": str(exc),
                "request_id": getattr(request.state, "request_id", None) or uuid.uuid4().hex[:12],
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled_exc_handler(request, exc: Exception):  # noqa: BLE001
        return JSONResponse(
            status_code=500,
            content={
                "code": 500,
                "message": f"internal error: {exc}",
                "request_id": getattr(request.state, "request_id", None) or uuid.uuid4().hex[:12],
            },
        )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/ready")
    async def ready():
        result = await asyncio.to_thread(check_readiness)
        dispatcher = task_manager._queue_dispatcher
        result["checks"]["dispatcher"] = dispatcher is not None and not dispatcher.done()
        result["prepared"] = result["prepared"] and result["checks"]["dispatcher"]
        result["ready"] = result["prepared"] and result["mode"] == "SERVING"
        return JSONResponse(result, status_code=200 if result["ready"] else 503)

    @app.get("/api/v1/deployment")
    async def deployment_status():
        return await asyncio.to_thread(releases.status, releases.DEPLOYMENT_ID)

    @app.get("/api/v1/version")
    async def version():
        """返回应用版本号(单一真相源:.env 的 DC_VERSION;回退到 package __version__)。"""
        return {"version": settings.version}

    # —— 控制台口令鉴权(DC_CONSOLE_PASSWORD 非空时启用,端点自身豁免于鉴权中间件)——
    @app.get("/api/v1/auth/status")
    async def auth_status(request: Request):
        """登录状态(免鉴权):前端据此决定是否先跳登录页。"""
        auth_required = console_auth_enabled()
        authenticated = (
            verify_session_token(request.cookies.get(COOKIE_NAME, ""))
            if auth_required
            else True
        )
        return {"auth_required": auth_required, "authenticated": authenticated}

    @app.post("/api/v1/auth/login")
    async def auth_login(body: dict, request: Request):
        """控制台登录:校验口令,成功后下发会话 Cookie(HttpOnly + SameSite=Lax)。

        登录失败按来源 IP 限速(进程内滑动窗口,达到阈值后 429)。access log 的
        脱敏规则已覆盖 "password" key,口令明文不会进入本地日志。
        """
        if not console_auth_enabled():
            raise HTTPException(400, "控制台口令未配置,无需登录")
        client_ip = _extract_client_ip(request)
        if login_rate_limited(client_ip):
            raise HTTPException(429, "登录失败次数过多,请稍后再试")
        if not verify_console_password(str(body.get("password", ""))):
            record_login_failure(client_ip)
            raise HTTPException(401, "口令错误")
        clear_login_failures(client_ip)
        response = JSONResponse(
            content={"status": "ok", "auth_required": True, "authenticated": True}
        )
        response.set_cookie(
            COOKIE_NAME,
            issue_session_token(),
            max_age=max(1, int(settings.console_session_ttl_hours * 3600)),
            httponly=True,
            samesite="lax",
            path="/",
        )
        return response

    @app.post("/api/v1/auth/logout")
    async def auth_logout():
        """退出登录:清除会话 Cookie(服务端无状态,令牌随之失效)。"""
        response = JSONResponse(content={"status": "ok"})
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @app.get("/api/v1/config/llm")
    async def get_llm_config():
        """读取当前生效的 LLM 配置(环境变量 + 持久化覆盖后的合并值)。"""
        # 多 worker 同步:本 worker 可能未收到 PUT,内存 settings 落后于 PG。
        ensure_llm_config_fresh()
        return {
            "llm_api_protocol": settings.llm_api_protocol,
            "llm_api_base": settings.llm_api_base,
            "llm_api_key": _mask_key(settings.llm_api_key),
            "llm_api_key_set": bool(settings.llm_api_key),
            "llm_model": settings.llm_model,
            "llm_timeout": settings.llm_timeout,
            "llm_max_concurrency": settings.llm_max_concurrency,
            "llm_max_retries": settings.llm_max_retries,
            "paddleocr_api_mode": settings.paddleocr_api_mode,
            "paddleocr_api_base": settings.paddleocr_api_base,
            "paddleocr_api_key": _mask_key(settings.paddleocr_api_key),
            "paddleocr_api_key_set": bool(settings.paddleocr_api_key),
            "paddleocr_model": settings.paddleocr_model,
            "paddleocr_official_api_base": settings.paddleocr_official_api_base,
            "paddleocr_official_access_token": _mask_key(
                settings.paddleocr_official_access_token
            ),
            "paddleocr_official_access_token_set": bool(
                settings.paddleocr_official_access_token
            ),
            "paddleocr_official_model": settings.paddleocr_official_model,
            "paddleocr_paddlex_api_base": settings.paddleocr_paddlex_api_base,
            "paddleocr_paddlex_endpoint": settings.paddleocr_paddlex_endpoint,
            "paddleocr_paddlex_api_key": _mask_key(settings.paddleocr_paddlex_api_key),
            "paddleocr_paddlex_api_key_set": bool(settings.paddleocr_paddlex_api_key),
            "paddleocr_timeout": settings.paddleocr_timeout,
            "paddleocr_max_concurrency": settings.paddleocr_max_concurrency,
            "paddleocr_max_retries": settings.paddleocr_max_retries,
            "judge_api_protocol": settings.judge_api_protocol,
            "judge_api_base": settings.judge_api_base,
            "judge_api_key": _mask_key(settings.judge_api_key),
            "judge_api_key_set": bool(settings.judge_api_key),
            "judge_model": settings.judge_model,
            "judge_timeout": settings.judge_timeout,
            "llm_direct_diff_prompt": settings.llm_direct_diff_prompt,
            "llm_diff_no_think_enabled": settings.llm_diff_no_think_enabled,
            "embed_backend": settings.embed_backend,
            "embed_api_base": settings.embed_api_base,
            "embed_api_key": _mask_key(settings.embed_api_key),
            "embed_api_key_set": bool(settings.embed_api_key),
            "embed_model": settings.embed_model,
            "embed_timeout": settings.embed_timeout,
            "pdf_render_dpi": settings.pdf_render_dpi,
            "seal_recovery_enabled": settings.seal_recovery_enabled,
            "seal_recovery_dpi": settings.seal_recovery_dpi,
            "max_pdf_pages": settings.max_pdf_pages,
            "external_api_key": _mask_key(settings.external_api_key),
            "external_api_key_set": bool(settings.external_api_key),
            "external_public_base_url": settings.external_public_base_url,
            "external_max_upload_mb": settings.external_max_upload_mb,
            "external_image_dpi": settings.external_image_dpi,
            "external_ocr_backend": settings.external_ocr_backend,
            "external_enable_llm_judge": settings.external_enable_llm_judge,
            "external_enable_llm_alignment": settings.external_enable_llm_alignment,
            "external_enable_risk_assessment": settings.external_enable_risk_assessment,
            "external_enable_llm_direct_diff": settings.external_enable_llm_direct_diff,
            "external_truncate_to_original_pages": settings.external_truncate_to_original_pages,
            "external_auto_discard_trailing_drawings": settings.external_auto_discard_trailing_drawings,
            "external_enabled": external_config_enabled(),
            "persisted": _safe_persisted_config(),
            # 只读:内置默认提示词(settings.llm_direct_diff_prompt 为空时生效的前半段),
            # 供 UI「查看内置默认规则」展示;不在 PUT 白名单内,前端回传会被挡。
            "llm_direct_diff_default_prompt": DEFAULT_DIFF_SYSTEM_PROMPT,
        }

    @app.put("/api/v1/config/llm")
    async def put_llm_config(body: dict):
        """更新 LLM 配置并持久化。

        可选字段:llm_api_base, llm_api_key, llm_model, llm_timeout,
        llm_max_concurrency, llm_max_retries,
        paddleocr_api_mode, paddleocr_api_base, paddleocr_api_key, paddleocr_model,
        paddleocr_official_api_base, paddleocr_official_access_token,
        paddleocr_official_model,
        paddleocr_timeout, paddleocr_max_concurrency, paddleocr_max_retries,
        judge_api_base, judge_api_key, judge_model, judge_timeout,
        embed_backend, embed_api_base, embed_api_key, embed_model, embed_timeout,
        pdf_render_dpi, seal_recovery_enabled, seal_recovery_dpi,
        max_pdf_pages, external_api_key,
        external_public_base_url, external_max_upload_mb, external_image_dpi,
        external_ocr_backend, external_enable_llm_judge, external_enable_llm_alignment,
        external_enable_risk_assessment, external_truncate_to_original_pages,
        external_auto_discard_trailing_drawings,
        llm_direct_diff_prompt(合同 LLM 直接比对系统提示词,留空=内置默认)。
        空值/省略表示不修改(api_key 传
        空串则清除已保存的 key)。
        """
        allowed = {
            "llm_api_protocol", "llm_api_base", "llm_api_key", "llm_model",
            "llm_timeout", "llm_max_concurrency", "llm_max_retries",
            "paddleocr_api_mode", "paddleocr_api_base", "paddleocr_api_key", "paddleocr_model",
            "paddleocr_official_api_base", "paddleocr_official_access_token",
            "paddleocr_official_model",
            "paddleocr_paddlex_api_base", "paddleocr_paddlex_endpoint",
            "paddleocr_paddlex_api_key",
            "paddleocr_timeout", "paddleocr_max_concurrency", "paddleocr_max_retries",
            "judge_api_protocol", "judge_api_base", "judge_api_key", "judge_model", "judge_timeout",
            "llm_direct_diff_prompt",
            "llm_diff_no_think_enabled",
            "embed_backend", "embed_api_base", "embed_api_key",
            "embed_model", "embed_timeout", "pdf_render_dpi",
            "seal_recovery_enabled", "seal_recovery_dpi", "max_pdf_pages",
            "external_api_key", "external_public_base_url",
            "external_max_upload_mb", "external_image_dpi",
            "external_ocr_backend", "external_enable_llm_judge",
            "external_enable_llm_alignment",
            "external_enable_risk_assessment",
            "external_enable_llm_direct_diff",
            "external_truncate_to_original_pages",
            "external_auto_discard_trailing_drawings",
        }
        unknown = set(body.keys()) - allowed
        if unknown:
            raise HTTPException(400, f"未知字段: {sorted(unknown)}")

        # 类型校验
        if "llm_api_protocol" in body and body["llm_api_protocol"] not in VALID_PROTOCOLS:
            raise HTTPException(
                400,
                "llm_api_protocol 必须为 openai / openai_responses / anthropic",
            )
        if "judge_api_protocol" in body and body["judge_api_protocol"] not in VALID_PROTOCOLS:
            raise HTTPException(
                400,
                "judge_api_protocol 必须为 openai / openai_responses / anthropic",
            )
        if "llm_timeout" in body and body["llm_timeout"] is not None:
            try:
                float(body["llm_timeout"])
            except (TypeError, ValueError):
                raise HTTPException(400, "llm_timeout 必须为数字")
        if "llm_max_concurrency" in body and body["llm_max_concurrency"] is not None:
            try:
                int(body["llm_max_concurrency"])
            except (TypeError, ValueError):
                raise HTTPException(400, "llm_max_concurrency 必须为整数")
        if "llm_max_retries" in body and body["llm_max_retries"] is not None:
            try:
                int(body["llm_max_retries"])
            except (TypeError, ValueError):
                raise HTTPException(400, "llm_max_retries 必须为整数")
        if "paddleocr_api_mode" in body:
            if body["paddleocr_api_mode"] not in {
                "vllm", "official_sdk", "paddlex_serving",
            }:
                raise HTTPException(
                    400,
                    "paddleocr_api_mode 必须为 vllm / official_sdk / paddlex_serving",
                )
        if body.get("paddleocr_official_model") not in {
            None,
            "",
            "PaddleOCR-VL",
            "PaddleOCR-VL-1.5",
            "PaddleOCR-VL-1.6",
        }:
            raise HTTPException(
                400,
                "paddleocr_official_model 仅支持 "
                "PaddleOCR-VL | PaddleOCR-VL-1.5 | PaddleOCR-VL-1.6",
            )
        if "paddleocr_timeout" in body and body["paddleocr_timeout"] is not None:
            try:
                float(body["paddleocr_timeout"])
            except (TypeError, ValueError):
                raise HTTPException(400, "paddleocr_timeout 必须为数字")
        if "paddleocr_max_concurrency" in body and body["paddleocr_max_concurrency"] is not None:
            try:
                int(body["paddleocr_max_concurrency"])
            except (TypeError, ValueError):
                raise HTTPException(400, "paddleocr_max_concurrency 必须为整数")
        if "paddleocr_max_retries" in body and body["paddleocr_max_retries"] is not None:
            try:
                int(body["paddleocr_max_retries"])
            except (TypeError, ValueError):
                raise HTTPException(400, "paddleocr_max_retries 必须为整数")
        if "judge_timeout" in body and body["judge_timeout"] is not None:
            try:
                float(body["judge_timeout"])
            except (TypeError, ValueError):
                raise HTTPException(400, "judge_timeout 必须为数字")
        if (
            "llm_direct_diff_prompt" in body
            and body["llm_direct_diff_prompt"] is not None
            and not isinstance(body["llm_direct_diff_prompt"], str)
        ):
            raise HTTPException(400, "llm_direct_diff_prompt 必须为字符串")
        if "llm_diff_no_think_enabled" in body:
            if not isinstance(body["llm_diff_no_think_enabled"], bool):
                raise HTTPException(400, "llm_diff_no_think_enabled 必须为布尔值")
        if "embed_backend" in body and body["embed_backend"] not in ("mock", "bge", "qwen"):
            raise HTTPException(400, "embed_backend 仅支持 mock | bge | qwen")
        if "embed_timeout" in body and body["embed_timeout"] is not None:
            try:
                float(body["embed_timeout"])
            except (TypeError, ValueError):
                raise HTTPException(400, "embed_timeout 必须为数字")
        if "pdf_render_dpi" in body and body["pdf_render_dpi"] is not None:
            try:
                dpi = int(body["pdf_render_dpi"])
            except (TypeError, ValueError):
                raise HTTPException(400, "pdf_render_dpi 必须为整数")
            if not 72 <= dpi <= 600:
                raise HTTPException(400, "pdf_render_dpi 取值范围 72-600")
        if "seal_recovery_enabled" in body:
            if not isinstance(body["seal_recovery_enabled"], bool):
                raise HTTPException(400, "seal_recovery_enabled 必须为布尔值")
        if "seal_recovery_dpi" in body and body["seal_recovery_dpi"] is not None:
            try:
                seal_dpi = int(body["seal_recovery_dpi"])
            except (TypeError, ValueError):
                raise HTTPException(400, "seal_recovery_dpi 必须为整数")
            if not 200 <= seal_dpi <= 600:
                raise HTTPException(400, "seal_recovery_dpi 取值范围 200-600")
        if "max_pdf_pages" in body and body["max_pdf_pages"] is not None:
            try:
                pages = int(body["max_pdf_pages"])
            except (TypeError, ValueError):
                raise HTTPException(400, "max_pdf_pages 必须为整数")
            if pages < 0:
                raise HTTPException(400, "max_pdf_pages 必须 >= 0(0 表示不限制)")
        if body.get("external_public_base_url"):
            try:
                body = dict(body)
                body["external_public_base_url"] = validate_public_base_url(
                    str(body["external_public_base_url"])
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        if "external_max_upload_mb" in body and body["external_max_upload_mb"] is not None:
            try:
                upload_mb = int(body["external_max_upload_mb"])
            except (TypeError, ValueError):
                raise HTTPException(400, "external_max_upload_mb 必须为整数")
            if not 1 <= upload_mb <= 1024:
                raise HTTPException(400, "external_max_upload_mb 取值范围 1-1024")
        if "external_image_dpi" in body and body["external_image_dpi"] is not None:
            try:
                image_dpi = int(body["external_image_dpi"])
            except (TypeError, ValueError):
                raise HTTPException(400, "external_image_dpi 必须为整数")
            if not 72 <= image_dpi <= 600:
                raise HTTPException(400, "external_image_dpi 取值范围 72-600")
        if "external_ocr_backend" in body:
            if body["external_ocr_backend"] not in {"llm", "paddleocr"}:
                raise HTTPException(400, "external_ocr_backend 必须为 llm 或 paddleocr")
        if "external_enable_llm_judge" in body:
            if not isinstance(body["external_enable_llm_judge"], bool):
                raise HTTPException(400, "external_enable_llm_judge 必须为布尔值")
        if "external_enable_llm_alignment" in body:
            if not isinstance(body["external_enable_llm_alignment"], bool):
                raise HTTPException(400, "external_enable_llm_alignment 必须为布尔值")
        if "external_enable_risk_assessment" in body:
            if not isinstance(body["external_enable_risk_assessment"], bool):
                raise HTTPException(400, "external_enable_risk_assessment 必须为布尔值")
        if "external_truncate_to_original_pages" in body:
            if not isinstance(body["external_truncate_to_original_pages"], bool):
                raise HTTPException(400, "external_truncate_to_original_pages 必须为布尔值")
        if "external_auto_discard_trailing_drawings" in body:
            if not isinstance(body["external_auto_discard_trailing_drawings"], bool):
                raise HTTPException(
                    400, "external_auto_discard_trailing_drawings 必须为布尔值"
                )

        # api_key 特殊处理:明文哨兵 "********" 表示"不修改"
        overrides = dict(body)
        if overrides.get("llm_api_key") == "********":
            overrides.pop("llm_api_key")
        if overrides.get("paddleocr_api_key") == "********":
            overrides.pop("paddleocr_api_key")
        if overrides.get("paddleocr_official_access_token") == "********":
            overrides.pop("paddleocr_official_access_token")
        if overrides.get("paddleocr_paddlex_api_key") == "********":
            overrides.pop("paddleocr_paddlex_api_key")
        if overrides.get("embed_api_key") == "********":
            overrides.pop("embed_api_key")
        if overrides.get("judge_api_key") == "********":
            overrides.pop("judge_api_key")
        if overrides.get("external_api_key") == "********":
            overrides.pop("external_api_key")

        save_llm_overrides(overrides)
        return {
            "status": "ok",
            "config": {
                "llm_api_protocol": settings.llm_api_protocol,
                "llm_api_base": settings.llm_api_base,
                "llm_api_key": _mask_key(settings.llm_api_key),
                "llm_api_key_set": bool(settings.llm_api_key),
                "llm_model": settings.llm_model,
                "llm_timeout": settings.llm_timeout,
                "llm_max_concurrency": settings.llm_max_concurrency,
                "llm_max_retries": settings.llm_max_retries,
                "paddleocr_api_mode": settings.paddleocr_api_mode,
                "paddleocr_api_base": settings.paddleocr_api_base,
                "paddleocr_api_key": _mask_key(settings.paddleocr_api_key),
                "paddleocr_api_key_set": bool(settings.paddleocr_api_key),
                "paddleocr_model": settings.paddleocr_model,
                "paddleocr_official_api_base": settings.paddleocr_official_api_base,
                "paddleocr_official_access_token": _mask_key(
                    settings.paddleocr_official_access_token
                ),
                "paddleocr_official_access_token_set": bool(
                    settings.paddleocr_official_access_token
                ),
                "paddleocr_official_model": settings.paddleocr_official_model,
                "paddleocr_paddlex_api_base": settings.paddleocr_paddlex_api_base,
                "paddleocr_paddlex_endpoint": settings.paddleocr_paddlex_endpoint,
                "paddleocr_paddlex_api_key": _mask_key(settings.paddleocr_paddlex_api_key),
                "paddleocr_paddlex_api_key_set": bool(settings.paddleocr_paddlex_api_key),
                "paddleocr_timeout": settings.paddleocr_timeout,
                "paddleocr_max_concurrency": settings.paddleocr_max_concurrency,
                "paddleocr_max_retries": settings.paddleocr_max_retries,
                "judge_api_protocol": settings.judge_api_protocol,
                "judge_api_base": settings.judge_api_base,
                "judge_api_key": _mask_key(settings.judge_api_key),
                "judge_api_key_set": bool(settings.judge_api_key),
                "judge_model": settings.judge_model,
                "judge_timeout": settings.judge_timeout,
                "llm_direct_diff_prompt": settings.llm_direct_diff_prompt,
                "embed_backend": settings.embed_backend,
                "embed_api_base": settings.embed_api_base,
                "embed_api_key": _mask_key(settings.embed_api_key),
                "embed_api_key_set": bool(settings.embed_api_key),
                "embed_model": settings.embed_model,
                "embed_timeout": settings.embed_timeout,
                "pdf_render_dpi": settings.pdf_render_dpi,
                "seal_recovery_enabled": settings.seal_recovery_enabled,
                "seal_recovery_dpi": settings.seal_recovery_dpi,
                "max_pdf_pages": settings.max_pdf_pages,
                "external_api_key": _mask_key(settings.external_api_key),
                "external_api_key_set": bool(settings.external_api_key),
                "external_public_base_url": settings.external_public_base_url,
                "external_max_upload_mb": settings.external_max_upload_mb,
                "external_image_dpi": settings.external_image_dpi,
                "external_ocr_backend": settings.external_ocr_backend,
                "external_enable_llm_judge": settings.external_enable_llm_judge,
                "external_enable_llm_alignment": settings.external_enable_llm_alignment,
                "external_enable_risk_assessment": settings.external_enable_risk_assessment,
                "external_enable_llm_direct_diff": settings.external_enable_llm_direct_diff,
                "external_truncate_to_original_pages": settings.external_truncate_to_original_pages,
                "external_auto_discard_trailing_drawings": settings.external_auto_discard_trailing_drawings,
                "external_enabled": external_config_enabled(),
                "llm_direct_diff_default_prompt": DEFAULT_DIFF_SYSTEM_PROMPT,
                "llm_diff_no_think_enabled": settings.llm_diff_no_think_enabled,
            },
        }

    @app.post("/api/v1/compare/api-test", status_code=202)
    async def submit_external_pipeline_test(
        source: UploadFile = File(..., description="测试用原始合同 Word(.docx)或 PDF(.pdf)"),
        target: UploadFile = File(..., description="测试用回收件 PDF(.pdf)"),
    ):
        """合同比对页 API 管线测试：复用外部任务产物流程，但不发送真实回调。"""
        if not external_config_enabled():
            raise HTTPException(400, "请先保存完整的外部系统 API 配置")
        if Path(source.filename or "").suffix.lower() not in {".docx", ".pdf"}:
            raise HTTPException(400, "source 必须为 .docx 或 .pdf")
        if not (target.filename or "").lower().endswith(".pdf"):
            raise HTTPException(400, "target 必须为 .pdf")

        max_bytes = settings.external_max_upload_mb * 1024 * 1024
        for field_name, upload in (("source", source), ("target", target)):
            if _upload_size(upload) > max_bytes:
                raise HTTPException(
                    413,
                    f"{field_name} 超过 {settings.external_max_upload_mb} MiB 上限",
                )

        if settings.max_pdf_pages > 0:
            try:
                pdf_bytes = target.file.read()
            finally:
                target.file.seek(0)
            try:
                page_count = count_pages_from_bytes(pdf_bytes)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(400, f"无法解析 PDF: {exc}")
            if page_count > settings.max_pdf_pages:
                raise HTTPException(
                    400,
                    f"暂不支持:PDF 共 {page_count} 页,超过上限 {settings.max_pdf_pages} 页",
                )

        if await task_manager.queue_is_full():
            raise HTTPException(429, "等待队列已满,请稍后重试")

        document_no = f"API-TEST-{uuid.uuid4().hex[:12].upper()}"
        task_id = task_manager.create(
            "compare",
            source_name=source.filename or "",
            target_names=[target.filename or ""],
            ocr_backend=settings.external_ocr_backend,
            document_no=document_no,
            external_request=True,
        )
        word_path = save_upload(source, task_id, "source")
        pdf_path = save_upload(target, task_id, "target")
        await task_manager.enqueue(task_id, {
            "runner": "compare", "word_path": str(word_path), "pdf_path": str(pdf_path),
            "enable_llm_judge": settings.external_enable_llm_judge,
            "enable_llm_alignment": settings.external_enable_llm_alignment,
            "ocr_backend": settings.external_ocr_backend,
            "enable_risk_assessment": settings.external_enable_risk_assessment,
            "enable_llm_direct_diff": settings.external_enable_llm_direct_diff,
            "truncate_to_original_pages": settings.external_truncate_to_original_pages,
            "auto_discard_trailing_drawings": settings.external_auto_discard_trailing_drawings,
        })
        return {"task_id": task_id, "document_no": document_no, "status": "pending"}

    @app.get("/api/v1/compare/api-test/{task_id}")
    async def get_external_pipeline_test(task_id: str):
        response = await _external_task_response(task_id)
        if not str(response.get("document_no", "")).startswith("API-TEST-"):
            raise HTTPException(404, "test task not found")
        return response

    @app.get("/api/v1/compare/api-test/{task_id}/images/{page_number}")
    async def get_external_pipeline_test_image(task_id: str, page_number: int):
        if page_number < 1:
            raise HTTPException(404, "image not found")
        task = task_manager.get(task_id)
        if task is not None:
            document_no = task.document_no or ""
        else:
            rec = await asyncio.to_thread(db_repo.get_task, task_id)
            document_no = rec.document_no if rec else ""
        if not str(document_no).startswith("API-TEST-"):
            raise HTTPException(404, "test task not found")
        path = external_image_path(task_id, page_number)
        if not path.is_file():
            raise HTTPException(404, "image not found")
        return FileResponse(path, media_type="image/png")

    @app.get("/api/v1/compare/api-test/{task_id}/source-images/{page_number}")
    async def get_external_pipeline_test_source_image(task_id: str, page_number: int):
        if page_number < 1:
            raise HTTPException(404, "image not found")
        task = task_manager.get(task_id)
        if task is not None:
            document_no = task.document_no or ""
        else:
            rec = await asyncio.to_thread(db_repo.get_task, task_id)
            document_no = rec.document_no if rec else ""
        if not str(document_no).startswith("API-TEST-"):
            raise HTTPException(404, "test task not found")
        path = external_image_path(task_id, page_number, side="source")
        if not path.is_file():
            raise HTTPException(404, "image not found")
        return FileResponse(path, media_type="image/png")

    @app.post("/api/v1/external/contractCompare", status_code=202)
    async def external_compare(
        request: Request,
        source: UploadFile | None = File(
            default=None, description="原始合同 Word(.docx)或 PDF(.pdf)"
        ),
        target: UploadFile | None = File(default=None, description="回收件 PDF(.pdf)"),
        source_url: str | None = Form(
            default=None,
            description="原始合同 URL(http/https .docx/.pdf);与 source 二选一",
        ),
        target_url: str | None = Form(
            default=None, description="回收件 URL(http/https .pdf);与 target 二选一"
        ),
        document_no: str = Form(...),
        original_page_count: int | None = Form(
            default=None,
            description="原始合同真实页数；仅用于显式截取回收 PDF",
        ),
        target_body_end_page: int | None = Form(
            default=None,
            description="供应商 PDF 正文截止页；优先于自动图纸识别和原件页数截取",
        ),
        callback_url: str | None = Form(default=None),
        sync: bool = Form(default=False),
        _auth: None = Depends(require_external_api_key),
    ):
        """供外部系统调用的标准合同比对入口。

        默认异步(`sync=false`):返回 task_id,结果经回调或查询端点获取。
        `sync=true`:优先同步等待完成；持续排队超过配置预算时返回 202 和 task_id。

        source/source_url 二选一、target/target_url 二选一;URL 模式下后端下载落盘后复用同一 pipeline。
        """
        # 审计参数快照:记录调用方提交的原始表单参数(文件只记文件名)。
        # 放在所有校验之前,400/413/429 等失败的审计记录同样携带参数。
        request.state.audit_params = {
            "source": (source.filename or None) if source is not None else None,
            "source_url": source_url,
            "target": (target.filename or None) if target is not None else None,
            "target_url": target_url,
            "document_no": document_no,
            "original_page_count": original_page_count,
            "target_body_end_page": target_body_end_page,
            "callback_url": callback_url,
            "sync": sync,
        }
        # source:文件与 URL 二选一(都传/都不传 → 400)
        source_url = (source_url or "").strip() or None
        has_source_file = source is not None and bool(source.filename)
        if has_source_file and source_url:
            raise HTTPException(400, "source 与 source_url 只能二选一")
        if not has_source_file and not source_url:
            raise HTTPException(400, "必须提供 source 文件或 source_url")
        # target:同上
        target_url = (target_url or "").strip() or None
        has_target_file = target is not None and bool(target.filename)
        if has_target_file and target_url:
            raise HTTPException(400, "target 与 target_url 只能二选一")
        if not has_target_file and not target_url:
            raise HTTPException(400, "必须提供 target 文件或 target_url")

        document_no = document_no.strip()
        if not document_no:
            raise HTTPException(400, "document_no 不能为空")
        if len(document_no) > 255:
            raise HTTPException(400, "document_no 不能超过 255 个字符")
        if original_page_count is not None and original_page_count < 1:
            raise HTTPException(400, "original_page_count 必须 >= 1")
        if target_body_end_page is not None and target_body_end_page < 1:
            raise HTTPException(400, "target_body_end_page 必须 >= 1")
        # callback_url:异步模式必填,同步模式可选(结果随响应返回)。
        callback_url = (callback_url or "").strip() or None
        if callback_url is None and not sync:
            raise HTTPException(400, "异步模式必须提供 callback_url")
        if callback_url is not None:
            try:
                callback_url = validate_callback_url(callback_url)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

        max_bytes = settings.external_max_upload_mb * 1024 * 1024
        if max_bytes <= 0:
            raise HTTPException(503, "外部 API 上传大小配置无效")
        # URL 预校验(scheme/userinfo),提前给出明确错误,避免发起无效连接。
        for role, url in (("source", source_url), ("target", target_url)):
            if url is None:
                continue
            try:
                validate_callback_url(url)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

        # URL 模式:下载到临时文件;失败 → 400,临时文件已在内部清理。
        # 下载与后续校验共用一个 try/finally,失败时 finally 清理未消费的临时文件。
        temp_source: Path | None = None
        source_filename = source.filename or "" if has_source_file else ""
        temp_target: Path | None = None
        target_filename = target.filename or "" if has_target_file else ""
        try:
            try:
                if source_url is not None:
                    temp_source, source_filename = await download_to_upload(
                        source_url, max_bytes=max_bytes, role="source"
                    )
                if target_url is not None:
                    temp_target, target_filename = await download_to_upload(
                        target_url, max_bytes=max_bytes, role="target"
                    )
            except ValueError as exc:
                # 下载失败(scheme/连接/状态码/超限/超时):转 400,错误信息已不含完整 URL。
                raise HTTPException(400, str(exc)) from exc

            # 文件名后缀校验(文件名来源:UploadFile 或下载推断)
            # source 允许 .docx/.pdf(原始合同可为 Word 或 PDF);target 固定 .pdf(回收件)。
            if Path(source_filename).suffix.lower() not in {".docx", ".pdf"}:
                raise HTTPException(400, "source 必须为 .docx 或 .pdf")
            if not target_filename.lower().endswith(".pdf"):
                raise HTTPException(400, "target 必须为 .pdf")

            # 大小校验:UploadFile 用 _upload_size(临时文件已在下载时流式限制,此处不再查)
            for field_name, upload in (("source", source), ("target", target)):
                if upload is None or not upload.filename:
                    continue
                if _upload_size(upload) > max_bytes:
                    raise HTTPException(
                        413,
                        f"{field_name} 超过 {settings.external_max_upload_mb} MiB 上限",
                    )

            # 与内部接口保持一致：页数上限和显式正文截止页在入队前校验。
            if settings.max_pdf_pages > 0 or target_body_end_page is not None:
                if has_target_file:
                    try:
                        pdf_bytes = target.file.read()
                    finally:
                        target.file.seek(0)
                else:
                    assert temp_target is not None
                    pdf_bytes = temp_target.read_bytes()
                try:
                    page_count = count_pages_from_bytes(pdf_bytes)
                except Exception as exc:  # noqa: BLE001
                    raise HTTPException(400, f"无法解析 PDF: {exc}")
                if settings.max_pdf_pages > 0 and page_count > settings.max_pdf_pages:
                    raise HTTPException(
                        400,
                        f"暂不支持:PDF 共 {page_count} 页,超过上限 {settings.max_pdf_pages} 页",
                    )
                if (
                    target_body_end_page is not None
                    and target_body_end_page > page_count
                ):
                    raise HTTPException(
                        400, "target_body_end_page 不能超过回收件实际页数"
                    )

            # 已满的执行槽位不再直接拒绝：只要持久化等待队列仍有容量就受理。
            # 预检发生在文件最终落盘前，避免队列满时留下无主上传文件。
            if await task_manager.queue_is_full():
                raise HTTPException(429, "等待队列已满,请稍后重试")

            task_id = task_manager.create(
                "compare",
                source_name=source_filename,
                target_names=[target_filename],
                ocr_backend=settings.external_ocr_backend,
               callback_url=callback_url,
               document_no=document_no,
               external_request=True,
               sync_mode=sync,
            )
            # 审计中间件读取:提交成功关联 task_id + document_no
            request.state.audit_endpoint = "contractCompare.submit"
            request.state.audit_task_id = task_id
            request.state.audit_document_no = document_no
            word_path = _persist_role(
                task_id, "source", source, temp_source, source_filename
            )
            pdf_path = _persist_role(
                task_id, "target", target, temp_target, target_filename
            )
            # 已落定,rename 后的最终路径接管;临时文件引用清空,finally 不再清理。
            temp_source = None
            temp_target = None
            queued = await task_manager.enqueue(task_id, {
                "runner": "compare",
                "word_path": str(word_path),
                "pdf_path": str(pdf_path),
                "enable_llm_judge": settings.external_enable_llm_judge,
                "enable_llm_alignment": settings.external_enable_llm_alignment,
                "ocr_backend": settings.external_ocr_backend,
                "enable_risk_assessment": settings.external_enable_risk_assessment,
                "enable_llm_direct_diff": settings.external_enable_llm_direct_diff,
                "truncate_to_original_pages": settings.external_truncate_to_original_pages,
                "auto_discard_trailing_drawings": settings.external_auto_discard_trailing_drawings,
                "target_body_end_page": target_body_end_page,
                "original_page_count": original_page_count,
                "sync_mode": sync,
            })
            if not queued:
                raise HTTPException(503, "任务入队失败,请稍后重试")
            if sync:
                # sync 优先保持旧契约；若持续 pending 超出队列预算，则交回 task_id
                # 让调用方查询/接收回调，避免大量长连接拖垮上游和本服务。
                claimed = await task_manager.wait_for_sync_queue(task_id)
                if not claimed:
                    return JSONResponse(
                        status_code=202,
                        content=await _external_task_response(task_id),
                    )
                await task_manager.wait_for_terminal(task_id)
                return JSONResponse(
                    status_code=200,
                    content=await _external_task_response(task_id),
                )
            return {"task_id": task_id, "document_no": document_no, "status": "pending"}
        finally:
            # 仅清理未被 finalize 接管的临时文件(下载后校验/并发失败时)。
            if temp_source is not None:
                temp_source.unlink(missing_ok=True)
            if temp_target is not None:
                temp_target.unlink(missing_ok=True)

    @app.get("/api/v1/external/contractCompare/{task_id}")
    async def get_external_result(
        request: Request,
        task_id: str,
        _auth: None = Depends(require_external_api_key),
    ):
        # 审计中间件读取:结果查询关联 task_id
        request.state.audit_endpoint = "contractCompare.result"
        request.state.audit_task_id = task_id
        request.state.audit_document_no = None
        return await _external_task_response(task_id)

    @app.get("/api/v1/external/contractCompare/{task_id}/images/{page_number}")
    async def get_external_highlight_image(
        request: Request,
        task_id: str,
        page_number: int,
        _auth: None = Depends(require_external_api_key),
    ):
        # 审计中间件读取:图片请求关联 task_id
        request.state.audit_endpoint = "contractCompare.image"
        request.state.audit_task_id = task_id
        request.state.audit_document_no = None
        request.state.audit_params = {"page_number": page_number}
        if page_number < 1:
            raise HTTPException(404, "image not found")
        task = task_manager.get(task_id)
        if task is not None:
            is_external = task.external_request
        else:
            rec = await asyncio.to_thread(db_repo.get_task, task_id)
            is_external = bool(rec and rec.external_request)
        if not is_external:
            raise HTTPException(404, "task not found")
        path = external_image_path(task_id, page_number)
        if not path.is_file():
            raise HTTPException(404, "image not found")
        return FileResponse(
            path,
            media_type="image/png",
            headers={
                "Content-Disposition": (
                    f'inline; filename="{task_id}-page-{page_number:04d}.png"'
                )
            },
        )

    @app.get("/api/v1/external/contractCompare/{task_id}/source-images/{page_number}")
    async def get_external_source_highlight_image(
        request: Request,
        task_id: str,
        page_number: int,
        _auth: None = Depends(require_external_api_key),
    ):
        request.state.audit_endpoint = "contractCompare.sourceImage"
        request.state.audit_task_id = task_id
        request.state.audit_document_no = None
        request.state.audit_params = {"page_number": page_number}
        if page_number < 1:
            raise HTTPException(404, "image not found")
        task = task_manager.get(task_id)
        if task is not None:
            is_external = task.external_request
        else:
            rec = await asyncio.to_thread(db_repo.get_task, task_id)
            is_external = bool(rec and rec.external_request)
        if not is_external:
            raise HTTPException(404, "task not found")
        path = external_image_path(task_id, page_number, side="source")
        if not path.is_file():
            raise HTTPException(404, "image not found")
        return FileResponse(
            path,
            media_type="image/png",
            headers={
                "Content-Disposition": (
                    f'inline; filename="{task_id}-source-page-{page_number:04d}.png"'
                )
            },
        )

    async def _external_report_file_context(
        request: Request, task_id: str, audit_endpoint: str
    ) -> tuple[str, datetime | None]:
        """外部报告下载端点共用:审计标签 + 任务归属校验,返回 (document_no, finished_at)。"""
        request.state.audit_endpoint = audit_endpoint
        request.state.audit_task_id = task_id
        request.state.audit_document_no = None
        document_no = ""
        finished_at = None
        task = task_manager.get(task_id)
        if task is not None:
            is_external = task.external_request
            document_no = task.document_no or ""
        else:
            rec = await asyncio.to_thread(db_repo.get_task, task_id)
            is_external = bool(rec and rec.external_request)
            if rec is not None:
                document_no = rec.document_no or ""
                finished_at = rec.finished_at or rec.created_at
        if not is_external:
            raise HTTPException(404, "task not found")
        return document_no, finished_at

    @app.get("/api/v1/external/contractCompare/{task_id}/report.html")
    async def download_external_html_report(
        request: Request,
        task_id: str,
        _auth: None = Depends(require_external_api_key),
    ):
        """下载自包含 HTML 比对报告(attachment;文件名=【单据号】对比+北京时间)。"""
        document_no, finished_at = await _external_report_file_context(
            request, task_id, "contractCompare.report"
        )
        path = external_html_report_path(task_id)
        if not path.is_file():
            raise HTTPException(404, "report not ready")
        filename = external_report_filename(document_no, finished_at)
        return _attachment_file_response(path, "text/html; charset=utf-8", filename)

    @app.get("/api/v1/external/contractCompare/{task_id}/report.pdf")
    async def download_external_pdf_report(
        request: Request,
        task_id: str,
        _auth: None = Depends(require_external_api_key),
    ):
        """下载自包含 PDF 比对报告(attachment;结果字段 ``result_url`` 指向此处)。"""
        document_no, finished_at = await _external_report_file_context(
            request, task_id, "contractCompare.report"
        )
        path = external_pdf_report_path(task_id)
        if not path.is_file():
            raise HTTPException(404, "report not ready")
        filename = external_report_filename(document_no, finished_at, suffix=".pdf")
        return _attachment_file_response(path, "application/pdf", filename)

    @app.post("/api/v1/external/amountStat", status_code=202)
    async def external_statement(
        request: Request,
        target: list[UploadFile] = File(default=[], description="对帐单 PDF(可多选,.pdf)"),
        target_urls: list[str] = Form(
            default=[], description="对帐单 URL(http/https .pdf,可重复多次);可与 target 混合"
        ),
        document_no: str = Form(...),
        document_type: str = Form(
            default="1",
            description="单据类型:1=发票(默认)/ 2=对帐单;支持中文别名",
        ),
        callback_url: str | None = Form(default=None),
        sync: bool = Form(default=False),
        _auth: None = Depends(require_external_api_key),
    ):
        """供外部系统调用的金额统计入口。

        默认异步(`sync=false`):返回 task_id,结果经回调或查询端点获取。
        `sync=true`:优先同步等待完成；持续排队超过配置预算时返回 202 和 task_id。

        支持多 PDF:target(文件)与 target_urls(URL)可混合提交,合计至少一个。
        document_type 标记统计对象是发票还是对帐单(1=发票、2=对帐单,缺省 1),
        入库并在对比记录中标识,供后续按单据类型查询。
        """
        # 审计参数快照:原始表单参数(文件只记文件名),先于校验写入。
        request.state.audit_params = {
            "target": [t.filename for t in target if t.filename],
            "target_urls": list(target_urls),
            "document_no": document_no,
            "document_type": document_type,
            "callback_url": callback_url,
            "sync": sync,
        }
        # 规范化 URL 列表(去空白、去空串)
        try:
            target_urls = _normalize_target_urls(target_urls)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        has_file = bool(target) and any(t.filename for t in target)
        if not has_file and not target_urls:
            raise HTTPException(400, "必须提供至少一个 target 文件或 target_url")

        document_no = document_no.strip()
        if not document_no:
            raise HTTPException(400, "document_no 不能为空")
        if len(document_no) > 255:
            raise HTTPException(400, "document_no 不能超过 255 个字符")
        try:
            document_type = _normalize_document_type(document_type)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        # callback_url:异步模式必填,同步模式可选(结果随响应返回)。
        callback_url = (callback_url or "").strip() or None
        if callback_url is None and not sync:
            raise HTTPException(400, "异步模式必须提供 callback_url")
        if callback_url is not None:
            try:
                callback_url = validate_callback_url(callback_url)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

        max_bytes = settings.external_max_upload_mb * 1024 * 1024
        if max_bytes <= 0:
            raise HTTPException(503, "外部 API 上传大小配置无效")
        # URL 预校验(scheme/userinfo),提前给出明确错误。
        for i, url in enumerate(target_urls):
            try:
                validate_callback_url(url)
            except ValueError as exc:
                raise HTTPException(400, f"第 {i + 1} 个 target_url: {exc}") from exc

        # 文件后缀校验(UploadFile)
        for i, t in enumerate(target):
            if t.filename and not t.filename.lower().endswith(".pdf"):
                raise HTTPException(400, f"第 {i + 1} 个文件必须是 .pdf")
        # 文件大小校验(URL 在下载时已流式限制)
        for i, t in enumerate(target):
            if t.filename and _upload_size(t) > max_bytes:
                raise HTTPException(
                    413,
                    f"第 {i + 1} 个文件超过 {settings.external_max_upload_mb} MiB 上限",
                )

        # URL 模式:逐个下载到临时文件;失败 → 400,临时文件在 finally 统一清理。
        temp_paths: list[Path] = []
        try:
            # 先下载 URL(与文件混合时,统一按"文件段 + URL 段"顺序落定,index 连续)
            url_filename_pairs: list[tuple[Path, str]] = []
            try:
                for i, url in enumerate(target_urls):
                    temp_path, filename = await download_to_upload(
                        url, max_bytes=max_bytes, role=f"target-url-{i}"
                    )
                    temp_paths.append(temp_path)
                    url_filename_pairs.append((temp_path, filename))
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

            # URL 下载产物后缀校验(下载推断的文件名)
            for i, (_tmp, filename) in enumerate(url_filename_pairs):
                if not filename.lower().endswith(".pdf"):
                    raise HTTPException(400, f"第 {i + 1} 个 target_url 必须为 .pdf")

            # PDF 页数上限预检(每个 PDF,文件 + URL)
            if settings.max_pdf_pages > 0:
                for i, t in enumerate(target):
                    if not t.filename:
                        continue
                    try:
                        pdf_bytes = t.file.read()
                    finally:
                        t.file.seek(0)
                    try:
                        page_count = count_pages_from_bytes(pdf_bytes)
                    except Exception as exc:  # noqa: BLE001
                        raise HTTPException(400, f"第 {i + 1} 个文件无法解析 PDF: {exc}")
                    if page_count > settings.max_pdf_pages:
                        raise HTTPException(
                            400,
                            f"暂不支持:第 {i + 1} 个文件共 {page_count} 页,"
                            f"超过上限 {settings.max_pdf_pages} 页",
                        )
                for i, (tmp, _filename) in enumerate(url_filename_pairs):
                    try:
                        page_count = count_pages_from_bytes(tmp.read_bytes())
                    except Exception as exc:  # noqa: BLE001
                        raise HTTPException(
                            400, f"第 {len(target) + i + 1} 个 target_url 无法解析 PDF: {exc}"
                        )
                    if page_count > settings.max_pdf_pages:
                        raise HTTPException(
                            400,
                            f"暂不支持:第 {len(target) + i + 1} 个 target_url 共 {page_count} 页,"
                            f"超过上限 {settings.max_pdf_pages} 页",
                        )

            if await task_manager.queue_is_full():
                raise HTTPException(429, "等待队列已满,请稍后重试")

            task_id = task_manager.create(
                "statement",
                source_name="",
                target_names=[
                    t.filename or f"target-{i}.pdf" for i, t in enumerate(target)
                ] + [fn for _tmp, fn in url_filename_pairs],
                ocr_backend=settings.external_ocr_backend,
                callback_url=callback_url,
                document_no=document_no,
                document_type=document_type,
                external_request=True,
                sync_mode=sync,
            )
            # 审计中间件读取:提交成功关联 task_id + document_no
            request.state.audit_endpoint = "amountStat.submit"
            request.state.audit_task_id = task_id
            request.state.audit_document_no = document_no

            # 多文件落定:index 连续(文件段 + URL 段),命名 {task_id}-target-{index}.pdf
            pdf_paths: list[str] = []
            file_names: list[str] = []
            index = 0
            for t in target:
                if not t.filename:
                    continue
                saved = save_upload(t, task_id, "target", index=index)
                pdf_paths.append(str(saved))
                file_names.append(t.filename)
                index += 1
            for tmp, filename in url_filename_pairs:
                saved = finalize_temp_file(tmp, task_id, "target", filename, index=index)
                temp_paths.remove(tmp)  # 已落定,finally 不再清理
                pdf_paths.append(str(saved))
                file_names.append(filename)
                index += 1

            queued = await task_manager.enqueue(task_id, {
                "runner": "statement",
                "pdf_paths": pdf_paths,
                "file_names": file_names,
                "ocr_backend": settings.external_ocr_backend,
                "sync_mode": sync,
            })
            if not queued:
                raise HTTPException(503, "任务入队失败,请稍后重试")
            if sync:
                claimed = await task_manager.wait_for_sync_queue(task_id)
                if not claimed:
                    return JSONResponse(
                        status_code=202,
                        content=await _external_statement_response(task_id),
                    )
                await task_manager.wait_for_terminal(task_id)
                return JSONResponse(
                    status_code=200,
                    content=await _external_statement_response(task_id),
                )
            return {
                "task_id": task_id,
                "document_no": document_no,
                "document_type": document_type,
                "status": "pending",
            }
        finally:
            # 仅清理未被 finalize 接管的临时文件(下载后校验/并发失败时)。
            for tmp in temp_paths:
                tmp.unlink(missing_ok=True)

    @app.get("/api/v1/external/amountStat/{task_id}")
    async def get_external_statement_result(
        request: Request,
        task_id: str,
        _auth: None = Depends(require_external_api_key),
    ):
        # 审计中间件读取:结果查询关联 task_id
        request.state.audit_endpoint = "amountStat.result"
        request.state.audit_task_id = task_id
        request.state.audit_document_no = None
        return await _external_statement_response(task_id)

    @app.post("/api/v1/compare")
    async def compare(
        source: UploadFile = File(..., description="原始 Word(.docx)或 PDF(.pdf)"),
        target: UploadFile = File(..., description="PDF 扫描件(.pdf)"),
        options: str | None = Form(default=None),
        callback_url: str | None = Form(default=None),
    ):
        # 基本类型校验
        if Path(source.filename or "").suffix.lower() not in {".docx", ".pdf"}:
            raise HTTPException(400, "source 必须为 .docx 或 .pdf")
        if not (target.filename or "").lower().endswith(".pdf"):
            raise HTTPException(400, "target 必须为 .pdf")

        # 解析 options(可选);提取 LLM 对齐/说明、OCR 与风险选项透传到 pipeline
        enable_llm_judge = False
        enable_llm_alignment = False
        ocr_backend: str | None = None
        enable_risk_assessment = False
        enable_llm_direct_diff = False
        truncate_to_original_pages = False
        auto_discard_trailing_drawings = False
        target_body_end_page: int | None = None
        original_page_count: int | None = None
        if options:
            try:
                opts = CompareOptions.model_validate_json(options)
                enable_llm_judge = opts.enable_llm_judge
                enable_llm_alignment = opts.enable_llm_alignment
                ocr_backend = opts.ocr_backend
                enable_risk_assessment = opts.enable_risk_assessment
                enable_llm_direct_diff = opts.enable_llm_direct_diff
                truncate_to_original_pages = opts.truncate_to_original_pages
                auto_discard_trailing_drawings = opts.auto_discard_trailing_drawings
                target_body_end_page = opts.target_body_end_page
                original_page_count = opts.original_page_count
                if (
                    opts.similarity_identical is not None
                    or opts.similarity_modified is not None
                ):
                    logger.warning(
                        "deprecated similarity decision thresholds ignored in zero-tolerance mode"
                    )
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(400, f"options 解析失败: {exc}")

        if await task_manager.queue_is_full():
            raise HTTPException(429, "等待队列已满,请稍后重试")

        # PDF 页数上限预检:超过配置上限直接拒绝,不进入流水线。
        # 0 表示不限制;读取 target 字节计数后回拨流,供 save_upload 再读一次。
        if settings.max_pdf_pages > 0 or target_body_end_page is not None:
            try:
                pdf_bytes = target.file.read()
            finally:
                target.file.seek(0)
            try:
                page_count = count_pages_from_bytes(pdf_bytes)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(400, f"无法解析 PDF: {exc}")
            if settings.max_pdf_pages > 0 and page_count > settings.max_pdf_pages:
                raise HTTPException(
                    400,
                    f"暂不支持:PDF 共 {page_count} 页,超过上限 {settings.max_pdf_pages} 页",
                )
            if target_body_end_page is not None and target_body_end_page > page_count:
                raise HTTPException(
                    400, "target_body_end_page 不能超过回收件实际页数"
                )

        task_id = task_manager.create(
            "compare",
            source_name=source.filename or "",
            target_names=[target.filename or ""],
            ocr_backend=ocr_backend,
            callback_url=callback_url,
        )
        word_path = save_upload(source, task_id, "source")
        pdf_path = save_upload(target, task_id, "target")
        await task_manager.enqueue(task_id, {
            "runner": "compare", "word_path": str(word_path), "pdf_path": str(pdf_path),
            "enable_llm_judge": enable_llm_judge,
            "enable_llm_alignment": enable_llm_alignment,
            "ocr_backend": ocr_backend,
            "enable_risk_assessment": enable_risk_assessment,
            "enable_llm_direct_diff": enable_llm_direct_diff,
            "truncate_to_original_pages": truncate_to_original_pages,
            "auto_discard_trailing_drawings": auto_discard_trailing_drawings,
            "target_body_end_page": target_body_end_page,
            "original_page_count": original_page_count,
        })
        return {"task_id": task_id, "status": "pending"}

    @app.get(
        "/api/v1/compare/{task_id}"
    )
    async def get_result(task_id: str):
        task = task_manager.get(task_id)
        if task is None:
            # 进程重启后内存丢失,从 PG 回捞(轻量元数据 + 报告 JSONB)。
            rec = await asyncio.to_thread(db_repo.get_task, task_id)
            if rec is None:
                raise HTTPException(404, "task not found")
            resp = _task_record_to_task_info_dict(rec, kind="compare")
            return resp
        resp = task.info.model_dump()
        if task.info.status == "done" and task.report is not None:
            resp["report"] = task.report.model_dump()
        elif task.info.status == "done":
            # 内存里 report 还没就绪时,从 PG JSONB 回捞。
            rec = await asyncio.to_thread(db_repo.get_task, task_id)
            if rec is not None and rec.report_compare is not None:
                resp["report"] = TamperReport.model_validate(
                    rec.report_compare
                ).model_dump()
        return resp

    @app.get(
        "/api/v1/compare/{task_id}/events",
    )
    async def events(task_id: str):
        task = task_manager.get(task_id)
        if task is None:
            # 内存 miss(任务在另一 worker 执行)时回查 PG,存在才继续。
            rec = await asyncio.to_thread(db_repo.get_task, task_id)
            if rec is None:
                raise HTTPException(404, "task not found")
            status = rec.status
        else:
            status = task.info.status

        async def gen():
            async for ev in task_manager.event_stream(task_id):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            yield f"event: done\ndata: {json.dumps({'status': status}, ensure_ascii=False)}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get(
        "/api/v1/compare/{task_id}/report",
    )
    async def download_report(
        task_id: str,
        format: Literal["json", "pdf", "html"] = "json",
        side: Literal["target", "source"] = "target",
    ):
        task = task_manager.get(task_id)
        report = task.report if task else None
        if report is None:
            # 从 PG JSONB 还原(进程重启或内存未就绪)
            rec = await asyncio.to_thread(db_repo.get_task, task_id)
            if rec is not None and rec.report_compare is not None:
                report = TamperReport.model_validate(rec.report_compare)
        if report is None:
            raise HTTPException(404, "report not ready")

        if format == "json":
            return JSONResponse(content=report.model_dump())
        if format == "pdf":
            if side == "source":
                source_path = upload_path(task_id, "source")
                if source_path is None:
                    raise HTTPException(404, "原件 PDF 文件已过期,无法生成标注报告")
                rendered_source = rendered_source_pdf_path(task_id)
                pdf_path = (
                    source_path if source_path.suffix.lower() == ".pdf" else rendered_source
                )
                if not pdf_path.is_file() or report.source_annotation_status == "unavailable":
                    raise HTTPException(
                        409,
                        report.source_annotation_reason or "原件侧版面标注不可用",
                    )
                out = settings.reports_dir / f"{task_id}_source_annotated.pdf"
            else:
                pdf_path = effective_target_path(task_id)
                if pdf_path is None:
                    raise HTTPException(404, "回收件 PDF 文件已过期,无法生成标注报告")
                out = settings.reports_dir / f"{task_id}_annotated.pdf"
            burn_pdf(pdf_path, report, out, side=side)
            return FileResponse(
                out,
                media_type="application/pdf",
                headers={
                    "Content-Disposition": f'attachment; filename="report-{side}-{task_id}.pdf"'
                },
            )
        # Web 端导出与外部 API 的 HTML 使用同一个渲染器：差异表行可跳到
        # 对应的高亮页面，且整页 PNG 已内嵌，下载后仍可离线查看。
        target_pdf = effective_target_path(task_id)
        if target_pdf is None:
            raise HTTPException(404, "回收件 PDF 文件已过期,无法生成 HTML 报告")
        source_path = upload_path(task_id, "source")
        source_pdf = (
            source_path
            if source_path is not None and source_path.suffix.lower() == ".pdf"
            else rendered_source_pdf_path(task_id)
        )
        render_args: tuple[Any, ...] = (task_id, target_pdf, report)
        if source_pdf.is_file():
            render_args = (*render_args, source_pdf)
        await asyncio.to_thread(render_external_highlight_images, *render_args)
        html_path = await asyncio.to_thread(
            write_external_html_report,
            task_id,
            (task.document_no if task else "") or task_id,
            report,
        )
        filename = external_report_filename(
            (task.document_no if task else "") or task_id,
            None,
        )
        from urllib.parse import quote

        return FileResponse(
            html_path,
            media_type="text/html; charset=utf-8",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
            },
        )

    @app.get("/api/v1/compare/{task_id}/report.pdf")
    async def download_pdf_report(task_id: str):
        """下载自包含 PDF 比对报告(概要+差异明细+逐页高亮图)。

        与外部 ``result_url`` 指向同一份产物。外部/api-test 任务完成时已落盘,
        直接返回;控制台自建任务首次下载时按需生成(先补齐每页高亮 PNG,
        与 HTML 导出同款,再渲染 PDF),生成后驻留盘上供后续下载复用。
        """
        task = task_manager.get(task_id)
        document_no = task.document_no if task else None
        report = task.report if task else None
        if report is None:
            # 从 PG JSONB 还原(进程重启或内存未就绪)
            rec = await asyncio.to_thread(db_repo.get_task, task_id)
            if rec is not None and rec.report_compare is not None:
                report = TamperReport.model_validate(rec.report_compare)
            if document_no is None and rec is not None:
                document_no = rec.document_no
        if report is None:
            raise HTTPException(404, "report not ready")
        document_no = document_no or task_id

        path = external_pdf_report_path(task_id)
        if not path.is_file():
            target_pdf = effective_target_path(task_id)
            if target_pdf is None:
                raise HTTPException(404, "回收件 PDF 文件已过期,无法生成 PDF 报告")
            source_path = upload_path(task_id, "source")
            source_pdf = (
                source_path
                if source_path is not None and source_path.suffix.lower() == ".pdf"
                else rendered_source_pdf_path(task_id)
            )
            render_args: tuple[Any, ...] = (task_id, target_pdf, report)
            if source_pdf.is_file():
                render_args = (*render_args, source_pdf)
            await asyncio.to_thread(render_external_highlight_images, *render_args)
            path = await asyncio.to_thread(
                write_external_pdf_report, task_id, document_no, report
            )
        return _attachment_file_response(
            path,
            "application/pdf",
            external_report_filename(document_no, None, suffix=".pdf"),
        )

    @app.get(
        "/api/v1/compare/{task_id}/source",
    )
    async def get_source_file(task_id: str):
        """返回实际参与比对的 PDF,供前端预览。

        页数截取发生时返回持久化的前 N 页文件；其他任务返回上传的原始 PDF。
        """
        path = effective_target_path(task_id)
        if path is None:
            raise HTTPException(404, "source file not found (may have been cleaned up)")
        return FileResponse(
            path,
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="{task_id}-target.pdf"'},
        )

    @app.get("/api/v1/compare/{task_id}/original-pdf")
    async def get_original_pdf(task_id: str):
        """返回原件 PDF 或 DOCX 派生的可视化 PDF 预览。"""
        source_path = upload_path(task_id, "source")
        if source_path is None:
            raise HTTPException(404, "原件文件已过期,无法预览")
        pdf_path = (
            source_path
            if source_path.suffix.lower() == ".pdf"
            else rendered_source_pdf_path(task_id)
        )
        if not pdf_path.is_file():
            raise HTTPException(409, "原件 DOCX 尚未生成可用的渲染 PDF")
        return FileResponse(
            pdf_path,
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="{task_id}-source.pdf"'},
        )

    # —— 无标注版(纯文本 difflib 比对)端点:与 /api/v1/compare 完全独立 ——
    @app.post("/api/v1/raw-compare")
    async def raw_compare(
        source: UploadFile = File(..., description="原始 Word(.docx)或 PDF(.pdf)"),
        target: UploadFile = File(..., description="PDF 扫描件(.pdf)"),
        options: str | None = Form(default=None),
        callback_url: str | None = Form(default=None),
    ):
        if Path(source.filename or "").suffix.lower() not in {".docx", ".pdf"}:
            raise HTTPException(400, "source 必须为 .docx 或 .pdf")
        if not (target.filename or "").lower().endswith(".pdf"):
            raise HTTPException(400, "target 必须为 .pdf")

        opts = RawCompareOptions()
        if options:
            try:
                opts = RawCompareOptions.model_validate_json(options)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(400, f"options 解析失败: {exc}")

        if await task_manager.queue_is_full():
            raise HTTPException(429, "等待队列已满,请稍后重试")

        # PDF 页数上限预检(与 /api/v1/compare 一致)。
        if settings.max_pdf_pages > 0:
            try:
                pdf_bytes = target.file.read()
            finally:
                target.file.seek(0)
            try:
                page_count = count_pages_from_bytes(pdf_bytes)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(400, f"无法解析 PDF: {exc}")
            if page_count > settings.max_pdf_pages:
                raise HTTPException(
                    400,
                    f"暂不支持:PDF 共 {page_count} 页,超过上限 {settings.max_pdf_pages} 页",
                )

        task_id = task_manager.create(
            "raw",
            source_name=source.filename or "",
            target_names=[target.filename or ""],
            ocr_backend=opts.ocr_backend,
            callback_url=callback_url,
        )
        word_path = save_upload(source, task_id, "source")
        pdf_path = save_upload(target, task_id, "target")
        await task_manager.enqueue(task_id, {
            "runner": "raw", "word_path": str(word_path), "pdf_path": str(pdf_path),
            "char_level": opts.char_level, "ocr_backend": opts.ocr_backend,
        })
        return {"task_id": task_id, "status": "pending"}

    @app.get("/api/v1/raw-compare/{task_id}")
    async def raw_get_result(task_id: str):
        task = task_manager.get(task_id)
        if task is None:
            rec = await asyncio.to_thread(db_repo.get_task, task_id)
            if rec is None:
                raise HTTPException(404, "task not found")
            resp = _task_record_to_task_info_dict(rec, kind="raw")
            return resp
        resp = task.info.model_dump()
        if task.info.status == "done" and task.raw_report is not None:
            resp["raw_report"] = task.raw_report.model_dump()
        elif task.info.status == "done":
            rec = await asyncio.to_thread(db_repo.get_task, task_id)
            if rec is not None and rec.report_raw is not None:
                resp["raw_report"] = rec.report_raw
        return resp

    @app.get("/api/v1/raw-compare/{task_id}/events")
    async def raw_events(task_id: str):
        task = task_manager.get(task_id)
        if task is None:
            rec = await asyncio.to_thread(db_repo.get_task, task_id)
            if rec is None:
                raise HTTPException(404, "task not found")
            status = rec.status
        else:
            status = task.info.status

        async def gen():
            async for ev in task_manager.event_stream(task_id):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            yield f"event: done\ndata: {json.dumps({'status': status}, ensure_ascii=False)}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/api/v1/raw-compare/{task_id}/report")
    async def raw_download_report(task_id: str):
        task = task_manager.get(task_id)
        report = task.raw_report if task else None
        if report is None:
            rec = await asyncio.to_thread(db_repo.get_task, task_id)
            if rec is not None and rec.report_raw is not None:
                report = TextDiffReport.model_validate(rec.report_raw)
        if report is None:
            raise HTTPException(404, "report not ready")
        return JSONResponse(content=report.model_dump())

    # —— 金额统计端点:与 /api/v1/raw-compare 完全独立 ——
    # 单端 PDF(对帐单扫描件),支持一次上传多个 PDF;后端串行 OCR + 表格抽取 + 代码求和,
    # 聚合输出所有文件的总金额。LLM 仅用于列定位兜底,绝不参与数值识别或求和。

    @app.post("/api/v1/statement/api-test", status_code=202)
    async def submit_statement_pipeline_test(
        target: list[UploadFile] = File(default=[], description="测试用对帐单 PDF(可多选,.pdf)"),
        target_urls: list[str] = Form(
            default=[], description="测试用对帐单 URL(http/https .pdf,可重复多次);可与 target 混合"
        ),
        document_type: str = Form(
            default="1",
            description="单据类型:1=发票(默认)/ 2=对帐单;支持中文别名",
        ),
    ):
        """金额统计页 API 管线测试：复用对外任务产物流程，但不发送真实回调。

        与 /api/v1/compare/api-test 对称：免鉴权、强制 document_no 以 `API-TEST-`
        开头(查询端点据此隔离)、不设 callback_url、OCR 走 external_ocr_backend。
        输入与正式 amountStat 一致：target 文件与 target_urls 可混合提交,
        document_type 单据类型(1=发票/2=对帐单)同样入库以便记录页联调验证。
        """
        if not external_config_enabled():
            raise HTTPException(400, "请先保存完整的外部系统 API 配置")
        try:
            target_urls = _normalize_target_urls(target_urls)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        try:
            document_type = _normalize_document_type(document_type)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        has_file = bool(target) and any(item.filename for item in target)
        if not has_file and not target_urls:
            raise HTTPException(400, "必须提供至少一个 target 文件或 target_url")
        for i, t in enumerate(target):
            if t.filename and not t.filename.lower().endswith(".pdf"):
                raise HTTPException(400, f"第 {i + 1} 个文件必须是 .pdf")

        max_bytes = settings.external_max_upload_mb * 1024 * 1024
        for i, t in enumerate(target):
            if t.filename and _upload_size(t) > max_bytes:
                raise HTTPException(
                    413,
                    f"第 {i + 1} 个文件超过 {settings.external_max_upload_mb} MiB 上限",
                )

        for i, url in enumerate(target_urls):
            try:
                validate_callback_url(url)
            except ValueError as exc:
                raise HTTPException(400, f"第 {i + 1} 个 target_url: {exc}") from exc

        temp_paths: list[Path] = []
        try:
            url_filename_pairs: list[tuple[Path, str]] = []
            try:
                for i, url in enumerate(target_urls):
                    temp_path, filename = await download_to_upload(
                        url, max_bytes=max_bytes, role=f"api-test-target-url-{i}"
                    )
                    temp_paths.append(temp_path)
                    url_filename_pairs.append((temp_path, filename))
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

            for i, (_temp_path, filename) in enumerate(url_filename_pairs):
                if not filename.lower().endswith(".pdf"):
                    raise HTTPException(400, f"第 {i + 1} 个 target_url 必须为 .pdf")

            # PDF 页数上限预检(对每个文件和 URL 下载产物校验)
            if settings.max_pdf_pages > 0:
                for i, t in enumerate(target):
                    if not t.filename:
                        continue
                    try:
                        pdf_bytes = t.file.read()
                    finally:
                        t.file.seek(0)
                    try:
                        page_count = count_pages_from_bytes(pdf_bytes)
                    except Exception as exc:  # noqa: BLE001
                        raise HTTPException(400, f"第 {i + 1} 个文件无法解析 PDF: {exc}")
                    if page_count > settings.max_pdf_pages:
                        raise HTTPException(
                            400,
                            f"暂不支持:第 {i + 1} 个文件 {t.filename} 共 {page_count} 页,"
                            f"超过上限 {settings.max_pdf_pages} 页",
                        )
                for i, (temp_path, _filename) in enumerate(url_filename_pairs):
                    try:
                        page_count = count_pages_from_bytes(temp_path.read_bytes())
                    except Exception as exc:  # noqa: BLE001
                        raise HTTPException(
                            400, f"第 {len(target) + i + 1} 个 target_url 无法解析 PDF: {exc}"
                        )
                    if page_count > settings.max_pdf_pages:
                        raise HTTPException(
                            400,
                            f"暂不支持:第 {len(target) + i + 1} 个 target_url 共 {page_count} 页,"
                            f"超过上限 {settings.max_pdf_pages} 页",
                        )

            if await task_manager.queue_is_full():
                raise HTTPException(429, "等待队列已满,请稍后重试")

            document_no = f"API-TEST-{uuid.uuid4().hex[:12].upper()}"
            task_id = task_manager.create(
                "statement",
                source_name="",
                target_names=[
                    t.filename or f"target-{i}.pdf" for i, t in enumerate(target) if t.filename
                ] + [filename for _temp_path, filename in url_filename_pairs],
                ocr_backend=settings.external_ocr_backend,
                document_no=document_no,
                document_type=document_type,
                external_request=True,
            )
            # 多文件保存:上传文件段 + URL 段均连续编号，避免覆盖。
            pdf_paths: list[str] = []
            file_names: list[str] = []
            index = 0
            for t in target:
                if not t.filename:
                    continue
                saved = save_upload(t, task_id, "target", index=index)
                pdf_paths.append(str(saved))
                file_names.append(t.filename)
                index += 1
            for temp_path, filename in url_filename_pairs:
                saved = finalize_temp_file(temp_path, task_id, "target", filename, index=index)
                temp_paths.remove(temp_path)
                pdf_paths.append(str(saved))
                file_names.append(filename)
                index += 1
            await task_manager.enqueue(task_id, {
                "runner": "statement", "pdf_paths": pdf_paths, "file_names": file_names,
                "ocr_backend": settings.external_ocr_backend,
            })
            return {
                "task_id": task_id,
                "document_no": document_no,
                "document_type": document_type,
                "status": "pending",
            }
        finally:
            for temp_path in temp_paths:
                temp_path.unlink(missing_ok=True)

    @app.get("/api/v1/statement/api-test/{task_id}")
    async def get_statement_pipeline_test(task_id: str):
        response = await _external_statement_response(task_id)
        if not str(response.get("document_no", "")).startswith("API-TEST-"):
            raise HTTPException(404, "test task not found")
        return response

    @app.post("/api/v1/statement")
    async def statement(
        target: list[UploadFile] = File(..., description="对帐单 PDF(可多选,.pdf)"),
        options: str | None = Form(default=None),
        callback_url: str | None = Form(default=None),
    ):
        if not target:
            raise HTTPException(400, "至少上传一个 PDF")
        for i, t in enumerate(target):
            if not (t.filename or "").lower().endswith(".pdf"):
                raise HTTPException(400, f"第 {i + 1} 个文件必须是 .pdf")

        opts = StatementOptions()
        if options:
            try:
                opts = StatementOptions.model_validate_json(options)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(400, f"options 解析失败: {exc}")

        if await task_manager.queue_is_full():
            raise HTTPException(429, "等待队列已满,请稍后重试")

        # PDF 页数上限预检(对每个 PDF 校验)
        if settings.max_pdf_pages > 0:
            for i, t in enumerate(target):
                try:
                    pdf_bytes = t.file.read()
                finally:
                    t.file.seek(0)
                try:
                    page_count = count_pages_from_bytes(pdf_bytes)
                except Exception as exc:  # noqa: BLE001
                    raise HTTPException(400, f"第 {i + 1} 个文件无法解析 PDF: {exc}")
                if page_count > settings.max_pdf_pages:
                    raise HTTPException(
                        400,
                        f"暂不支持:第 {i + 1} 个文件 {t.filename} 共 {page_count} 页,"
                        f"超过上限 {settings.max_pdf_pages} 页",
                    )

        task_id = task_manager.create(
            "statement",
            source_name="",
            target_names=[t.filename or f"target-{i}.pdf" for i, t in enumerate(target)],
            ocr_backend=opts.ocr_backend,
            callback_url=callback_url,
        )
        # 多文件保存:role="target" + index,避免覆盖
        pdf_paths: list[str] = []
        file_names: list[str] = []
        for i, t in enumerate(target):
            saved = save_upload(t, task_id, "target", index=i)
            pdf_paths.append(str(saved))
            file_names.append(t.filename or f"target-{i}.pdf")
        await task_manager.enqueue(task_id, {
            "runner": "statement", "pdf_paths": pdf_paths, "file_names": file_names,
            "ocr_backend": opts.ocr_backend,
            "amount_column_keywords": opts.amount_column_keywords,
            "enable_llm_column_detection": opts.enable_llm_column_detection,
        })
        return {"task_id": task_id, "status": "pending", "file_count": len(pdf_paths)}

    @app.get("/api/v1/statement/{task_id}")
    async def statement_get_result(task_id: str):
        task = task_manager.get(task_id)
        if task is None:
            rec = await asyncio.to_thread(db_repo.get_task, task_id)
            if rec is None:
                raise HTTPException(404, "task not found")
            resp = _task_record_to_task_info_dict(rec, kind="statement")
            return resp
        resp = task.info.model_dump()
        if task.info.status == "done" and task.statement_report is not None:
            resp["statement_report"] = task.statement_report.model_dump()
        elif task.info.status == "done":
            rec = await asyncio.to_thread(db_repo.get_task, task_id)
            if rec is not None and rec.report_statement is not None:
                resp["statement_report"] = rec.report_statement
        return resp

    @app.get("/api/v1/statement/{task_id}/events")
    async def statement_events(task_id: str):
        task = task_manager.get(task_id)
        if task is None:
            rec = await asyncio.to_thread(db_repo.get_task, task_id)
            if rec is None:
                raise HTTPException(404, "task not found")
            status = rec.status
        else:
            status = task.info.status

        async def gen():
            async for ev in task_manager.event_stream(task_id):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            yield f"event: done\ndata: {json.dumps({'status': status}, ensure_ascii=False)}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/api/v1/statement/{task_id}/report")
    async def statement_download_report(task_id: str):
        task = task_manager.get(task_id)
        report = task.statement_report if task else None
        if report is None:
            rec = await asyncio.to_thread(db_repo.get_task, task_id)
            if rec is not None and rec.report_statement is not None:
                report = StatementSummaryReport.model_validate(rec.report_statement)
        if report is None:
            raise HTTPException(404, "report not ready")
        return JSONResponse(content=report.model_dump())

    # —— 比对记录(任务历史列表 + 单任务里程碑时间线)——
    @app.get("/api/v1/tasks")
    async def list_tasks(
        kind: str | None = None,
        status: str | None = None,
        document_type: str | None = None,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        """分页查询任务历史列表(按 created_at 倒序)。

        查询参数:
          kind   - compare | raw | statement(可选筛选)
          status - pending | running | done | failed(可选筛选)
          document_type - 1(发票) | 2(对帐单)(可选筛选,金额统计任务的
                          单据类型细分,用于区分统计对象)
          q      - 模糊搜索关键字,匹配 task_id / document_no / source_name / target_names
          limit  - 默认 50,上限 200
          offset - 分页偏移
        返回 {items: [...轻量元数据], total: N}。不含报告 JSONB。
        """
        if limit < 1:
            limit = 50
        if limit > 200:
            limit = 200
        if offset < 0:
            offset = 0
        if kind is not None and kind not in ("compare", "raw", "statement"):
            raise HTTPException(400, "kind 必须为 compare | raw | statement")
        if status is not None and status not in ("pending", "running", "done", "failed"):
            raise HTTPException(400, "status 必须为 pending | running | done | failed")
        if document_type is not None and document_type not in ("1", "2"):
            raise HTTPException(400, "document_type 必须为 1(发票)或 2(对帐单)")
        # 空白 q 视为未搜索,避免空串退化为 %% 全表匹配
        q = q.strip() if q else None

        records, total = await asyncio.to_thread(
            db_repo.list_tasks,
            kind=kind, status=status, document_type=document_type,
            q=q, limit=limit, offset=offset,
        )
        items = [db_repo.to_dict(r, include_report=False) for r in records]
        return {"items": items, "total": total}

    @app.get("/api/v1/tasks/{task_id}/events")
    async def list_task_events(task_id: str):
        """返回指定任务的历史里程碑事件列表(按 id 升序)。

        与 SSE events 不同:本端点返回已经持久化的历史事件(只含里程碑),
        用于「比对记录 → 详情时间线」;SSE 仍是实时增量流。
        """
        rec = await asyncio.to_thread(db_repo.get_task, task_id)
        if rec is None:
            raise HTTPException(404, "task not found")
        events = await asyncio.to_thread(db_repo.get_task_events, task_id)
        return {
            "task_id": task_id,
            "items": [db_repo.event_to_dict(e) for e in events],
        }

    @app.post("/api/v1/tasks/{task_id}/recover")
    async def recover_task_from_history(task_id: str):
        """将保留输入文件和队列参数的失败任务重新入队。"""
        outcome = await task_manager.recover_from_history(task_id)
        if outcome == "not_found":
            raise HTTPException(404, "task not found")
        if outcome == "not_recoverable":
            raise HTTPException(409, "任务缺少可恢复的队列参数或上传文件")
        if outcome == "invalid_status":
            raise HTTPException(409, "仅失败任务可以恢复")
        return {"task_id": task_id, "status": "pending", "message": "任务已重新入队"}

    @app.post("/api/v1/tasks/{task_id}/stop")
    async def stop_task_from_history(task_id: str):
        """停止排队任务，或请求运行中任务在当前阶段后终止。"""
        outcome = await task_manager.stop_from_history(task_id)
        if outcome == "not_found":
            raise HTTPException(404, "task not found")
        if outcome == "invalid_status":
            raise HTTPException(409, "仅排队中或处理中的任务可以停止")
        if outcome == "stop_requested":
            return JSONResponse(
                status_code=202,
                content={
                    "task_id": task_id,
                    "status": "running",
                    "stop_requested": True,
                    "message": "已请求停止，当前处理阶段结束后终止",
                },
            )
        return {"task_id": task_id, "status": "failed", "message": "排队任务已停止"}

    async def _download_task_upload(task_id: str, role: Literal["source", "target"]):
        """下载任务上传的原始文件，不使用预览派生或截断产物。"""
        record = await asyncio.to_thread(db_repo.get_task, task_id)
        if record is None:
            raise HTTPException(404, "task not found")

        original_name = (
            record.source_name
            if role == "source"
            else (record.target_names or [""])[0]
        )
        if not original_name:
            raise HTTPException(404, "该任务没有可下载的原始文件")

        path = upload_path(task_id, role)
        if path is None or not path.is_file():
            raise HTTPException(404, "原始文件已过期,无法下载")

        # 仅使用持久化展示名的 basename，避免把调用方传入的路径片段带入下载响应头。
        filename = Path(original_name.replace("\\", "/")).name or f"{role}{path.suffix}"
        media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return _attachment_file_response(path, media_type, filename)

    @app.get("/api/v1/tasks/{task_id}/source/download")
    async def download_task_source_file(task_id: str):
        """下载采购部合同原始上传件，不使用 DOCX 派生的预览 PDF。"""
        return await _download_task_upload(task_id, "source")

    @app.get("/api/v1/tasks/{task_id}/target/download")
    async def download_task_target_file(task_id: str):
        """下载供应商合同原始上传件，不使用页数截断后的比对 PDF。"""
        return await _download_task_upload(task_id, "target")

    @app.post("/api/v1/tasks/{task_id}/redeliver-callback")
    async def redeliver_callback(task_id: str):
        """对已完成任务的回调地址重新推送一次(用于回调失败/漏投后补发)。

        从 task_records 读取 callback_url 与首次交付时持久化的业务 payload,
        用任务终态 status 作为 envelope,经 webhook.deliver 重投(指数退避重试),
        然后回写 callback 交付结果。功能上线前的历史任务若无 payload,退化为
        仅含 envelope 的最小事件。
        """
        rec = await asyncio.to_thread(db_repo.get_task, task_id)
        if rec is None:
            raise HTTPException(404, "task not found")
        if not rec.callback_url:
            raise HTTPException(400, "该任务未配置回调地址,无法重新推送")
        if rec.status not in ("done", "failed"):
            raise HTTPException(409, "任务尚未结束,暂不回调")
        payload = rec.callback_payload or {}
        event, raw = webhook.build_event(task_id, rec.status, payload)
        result = await webhook.deliver(
            rec.callback_url,
            raw,
            event["event_id"],
            max_retries=settings.webhook_max_retries,
            timeout=settings.webhook_timeout_seconds,
        )
        await asyncio.to_thread(
            db_repo.update_callback_result,
            task_id,
            success=result["success"],
            http_status=result["http_status"],
            error=result["error"],
        )
        return {
            "task_id": task_id,
            "callback_url": rec.callback_url,
            "callback_status": "success" if result["success"] else "failed",
            "callback_http_status": result["http_status"],
            "callback_error": result["error"],
        }

    @app.get("/api/v1/tasks/{task_id}/llm-calls")
    async def list_task_llm_calls(task_id: str):
        """返回指定任务所有模型调用与最终 OCR 解析结果(按 id 升序)。

        每条含 kind / attempt / status_code / elapsed_ms / payload(图片已脱敏)/
        response(截断 64KB)/ error。embedding 调用不入本表。ocr-result 保存最终采用
        的页/块/坐标和受限文本预览，用于「比对记录 → 模型 / OCR 记录」调试与审计。
        """
        rec = await asyncio.to_thread(db_repo.get_task, task_id)
        if rec is None:
            raise HTTPException(404, "task not found")
        calls = await asyncio.to_thread(db_repo.get_task_llm_calls, task_id)
        return {
            "task_id": task_id,
            "items": [db_repo.llm_call_to_dict(c) for c in calls],
        }

    @app.get("/api/v1/tasks/{task_id}/external-calls")
    async def list_task_external_calls(task_id: str):
        """返回指定任务所有外部接口调用记录(按 id 升序,即调用发生顺序)。

        每条含 endpoint / method / status_code / elapsed_ms / client_ip /
        api_key_sha256(指纹,非明文)/ error / request_id / content_length。
        用于「比对记录 → 外部调用」审计明细。覆盖 401 鉴权失败等无 task_id 场景时,
        通过全局 `/api/v1/external-calls` 查询。
        """
        rec = await asyncio.to_thread(db_repo.get_task, task_id)
        if rec is None:
            raise HTTPException(404, "task not found")
        calls = await asyncio.to_thread(db_repo.get_task_external_calls, task_id)
        return {
            "task_id": task_id,
            "items": [db_repo.external_call_to_dict(c) for c in calls],
        }

    @app.get("/api/v1/external-calls")
    async def list_external_calls(
        endpoint: str | None = Query(default=None),
        status_code: int | None = Query(default=None),
        document_no: str | None = Query(default=None),
        q: str | None = Query(default=None, description="task_id/document_no/client_ip/request_id 模糊匹配"),
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ):
        """全局外部接口调用审计列表(按 created_at 倒序,分页)。

        支持按 endpoint / status_code / document_no 精确过滤,以及 q 在
        task_id/document_no/client_ip/request_id 上的模糊匹配。覆盖无 task_id 的
        401 鉴权失败、422 校验失败等调用记录(这些调用没有关联 task_records)。
        """
        records, total = await asyncio.to_thread(
            db_repo.list_external_calls,
            endpoint=endpoint,
            status_code=status_code,
            document_no=document_no,
            q=q.strip() if q else None,
            limit=limit,
            offset=offset,
        )
        return {
            "items": [db_repo.external_call_to_dict(r) for r in records],
            "total": total,
        }

    @app.get("/api/v1/stats/daily")
    async def get_daily_stats(
        days: int = Query(default=14, ge=1, le=90, description="统计天数(仅未提供 start/end 时生效,含今天)"),
        start: str | None = Query(default=None, description="起始日期 YYYY-MM-DD(北京时间);提供后进入自由区间模式"),
        end: str | None = Query(default=None, description="结束日期 YYYY-MM-DD(北京时间);缺省为今天,晚于今天截断到今天"),
    ):
        """每日调用统计(控制台看板)。

        按天(北京时间)聚合三类计数:任务数(按状态/类型细分)、模型调用数
        (成功/失败,按 kind 细分)、外部接口调用数(成功/失败,按端点细分)。
        区间:start/end 任一提供即用显式区间(缺 end 补今天、缺 start 由 days
        回推),均缺省时为最近 days 天。start 晚于 end、跨度超 366 天、日期
        格式非法返回 400。缺失日期补零,series 按日期升序。控制台口令鉴权
        自动覆盖本端点。
        """
        try:
            start_d = date.fromisoformat(start) if start else None
            end_d = date.fromisoformat(end) if end else None
        except ValueError:
            raise HTTPException(400, "start/end 必须为 YYYY-MM-DD 格式日期")
        try:
            return await asyncio.to_thread(
                db_repo.daily_stats, days=days, start=start_d, end=end_d
            )
        except ValueError as e:
            raise HTTPException(400, str(e))

    # —— 前端静态文件(DC_STATIC_DIR 设置时启用,单容器部署用)——
    # 所有 /api、/health 路由已注册完毕,catch-all 放最后不会拦截 API。
    if settings.static_dir and settings.static_dir.is_dir():
        static_root = settings.static_dir.resolve()
        index_html = static_root / "index.html"
        assets_dir = static_root / "assets"

        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/{full_path:path}")
        async def spa_fallback(full_path: str):
            """非 API / 非静态资源的请求回退到 index.html,供 Vue Router history 模式。"""
            candidate = safe_static_file_path(static_root, full_path)
            if full_path and candidate is not None and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(index_html)

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    settings.ensure_dirs()
    uvicorn.run(
        "document_comparison.api.app:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )
