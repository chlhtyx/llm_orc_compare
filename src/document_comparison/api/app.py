"""FastAPI 应用:比对端点 + 对外集成(§7、§14)。

端点:
  POST /api/v1/compare              提交(支持 callback_url,可选)
  GET  /api/v1/compare/{task_id}    查询结果
  GET  /api/v1/compare/{task_id}/events   SSE 进度(§7.3)
  GET  /api/v1/compare/{task_id}/report   下载报告(json/pdf)
  GET  /api/v1/compare/{task_id}/source   获取源 PDF 预览
  GET  /health
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..config import (
    _maybe_import_legacy_llm_config_file,
    apply_llm_overrides,
    load_llm_overrides,
    save_llm_overrides,
    settings,
)
from .. import db as db_pkg
from ..db import repository as db_repo
from ..logging_config import setup_logging
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
    effective_target_path,
    save_upload,
)
from ..report.builder import burn_pdf
from ..external_api import (
    build_external_result,
    external_config_enabled,
    external_image_path,
    require_external_api_key,
    validate_callback_url,
    validate_public_base_url,
)
from ..tasks import task_manager

logger = logging.getLogger(__name__)


def _upload_size(upload: UploadFile) -> int:
    """读取底层临时文件大小并回拨，不把整个文件复制进内存。"""
    current = upload.file.tell()
    upload.file.seek(0, 2)
    size = upload.file.tell()
    upload.file.seek(current)
    return size



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
    (401/422 等端点未执行场景下为 None)。写库失败只记日志(遵循 task_records 双写规则)。
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
            error=_EXTERNAL_ERROR_BY_STATUS.get(status_code),
            request_id=request_id,
            content_length=_parse_content_length(request.headers.get("content-length")),
        )
    except Exception:  # noqa: BLE001
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
    # LLM 配置持久化迁移:首次启动若 PG 无记录且本地遗留 llm_config.json 存在,
    # 把文件导入 PG(文件保留作备份),再把 PG 配置应用到运行时 settings 单例。
    _maybe_import_legacy_llm_config_file()
    apply_llm_overrides()
    app = FastAPI(
       title="文档比对 API",
        description="基于多模态 LLM API 的合同条款篡改检测(§7、§14)",
       version="0.1.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
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
            # 异步落库,不阻塞响应返回;异常已在 _persist_external_call 内吞并
            asyncio.create_task(_persist_external_call(
                request=request,
                status_code=status_code,
                elapsed_ms=elapsed_ms,
            ))

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

    @app.get("/api/v1/config/llm")
    async def get_llm_config():
        """读取当前生效的 LLM 配置(环境变量 + 持久化覆盖后的合并值)。"""
        return {
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
            "paddleocr_timeout": settings.paddleocr_timeout,
            "paddleocr_max_concurrency": settings.paddleocr_max_concurrency,
            "paddleocr_max_retries": settings.paddleocr_max_retries,
            "judge_api_base": settings.judge_api_base,
            "judge_api_key": _mask_key(settings.judge_api_key),
            "judge_api_key_set": bool(settings.judge_api_key),
            "judge_model": settings.judge_model,
            "judge_timeout": settings.judge_timeout,
            "embed_backend": settings.embed_backend,
            "embed_api_base": settings.embed_api_base,
            "embed_api_key": _mask_key(settings.embed_api_key),
            "embed_api_key_set": bool(settings.embed_api_key),
            "embed_model": settings.embed_model,
            "embed_timeout": settings.embed_timeout,
            "pdf_render_dpi": settings.pdf_render_dpi,
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
            "external_enabled": external_config_enabled(),
            "persisted": _safe_persisted_config(),
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
        pdf_render_dpi, max_pdf_pages, external_api_key,
        external_public_base_url, external_max_upload_mb, external_image_dpi,
        external_ocr_backend, external_enable_llm_judge, external_enable_llm_alignment,
        external_enable_risk_assessment, external_truncate_to_original_pages。
        空值/省略表示不修改(api_key 传
        空串则清除已保存的 key)。
        """
        allowed = {
            "llm_api_base", "llm_api_key", "llm_model",
            "llm_timeout", "llm_max_concurrency", "llm_max_retries",
            "paddleocr_api_mode", "paddleocr_api_base", "paddleocr_api_key", "paddleocr_model",
            "paddleocr_official_api_base", "paddleocr_official_access_token",
            "paddleocr_official_model",
            "paddleocr_timeout", "paddleocr_max_concurrency", "paddleocr_max_retries",
            "judge_api_base", "judge_api_key", "judge_model", "judge_timeout",
            "embed_backend", "embed_api_base", "embed_api_key",
            "embed_model", "embed_timeout", "pdf_render_dpi", "max_pdf_pages",
            "external_api_key", "external_public_base_url",
            "external_max_upload_mb", "external_image_dpi",
            "external_ocr_backend", "external_enable_llm_judge",
            "external_enable_llm_alignment",
            "external_enable_risk_assessment",
            "external_enable_llm_direct_diff",
            "external_truncate_to_original_pages",
        }
        unknown = set(body.keys()) - allowed
        if unknown:
            raise HTTPException(400, f"未知字段: {sorted(unknown)}")

        # 类型校验
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
            if body["paddleocr_api_mode"] not in {"vllm", "official_sdk"}:
                raise HTTPException(
                    400, "paddleocr_api_mode 必须为 vllm 或 official_sdk"
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

        # api_key 特殊处理:明文哨兵 "********" 表示"不修改"
        overrides = dict(body)
        if overrides.get("llm_api_key") == "********":
            overrides.pop("llm_api_key")
        if overrides.get("paddleocr_api_key") == "********":
            overrides.pop("paddleocr_api_key")
        if overrides.get("paddleocr_official_access_token") == "********":
            overrides.pop("paddleocr_official_access_token")
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
                "paddleocr_timeout": settings.paddleocr_timeout,
                "paddleocr_max_concurrency": settings.paddleocr_max_concurrency,
                "paddleocr_max_retries": settings.paddleocr_max_retries,
                "judge_api_base": settings.judge_api_base,
                "judge_api_key": _mask_key(settings.judge_api_key),
                "judge_api_key_set": bool(settings.judge_api_key),
                "judge_model": settings.judge_model,
                "judge_timeout": settings.judge_timeout,
                "embed_backend": settings.embed_backend,
                "embed_api_base": settings.embed_api_base,
                "embed_api_key": _mask_key(settings.embed_api_key),
                "embed_api_key_set": bool(settings.embed_api_key),
                "embed_model": settings.embed_model,
                "embed_timeout": settings.embed_timeout,
                "pdf_render_dpi": settings.pdf_render_dpi,
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
                "external_enabled": external_config_enabled(),
            },
        }

    @app.post("/api/v1/compare/api-test", status_code=202)
    async def submit_external_pipeline_test(
        source: UploadFile = File(..., description="测试用原始合同 Word(.docx)"),
        target: UploadFile = File(..., description="测试用回收件 PDF(.pdf)"),
    ):
        """合同比对页 API 管线测试：复用外部任务产物流程，但不发送真实回调。"""
        if not external_config_enabled():
            raise HTTPException(400, "请先保存完整的外部系统 API 配置")
        if not (source.filename or "").lower().endswith(".docx"):
            raise HTTPException(400, "source 必须为 .docx")
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

        # 全局并发上限走 PG 计数(跨 worker 一致);事务级检查,瞬时略超由执行端
        # 进程内信号量 + 任务排队消化。
        running = await asyncio.to_thread(db_repo.count_active_tasks)
        if running >= settings.max_concurrent_tasks:
            raise HTTPException(429, "并发任务已达上限,请稍后重试")

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
        asyncio.create_task(
            task_manager.run(
                task_id,
                str(word_path),
                str(pdf_path),
                enable_llm_judge=settings.external_enable_llm_judge,
                enable_llm_alignment=settings.external_enable_llm_alignment,
                ocr_backend=settings.external_ocr_backend,
                enable_risk_assessment=settings.external_enable_risk_assessment,
                enable_llm_direct_diff=settings.external_enable_llm_direct_diff,
                truncate_to_original_pages=settings.external_truncate_to_original_pages,
            )
        )
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

    @app.post("/api/v1/external/contractCompare", status_code=202)
    async def external_compare(
        request: Request,
        source: UploadFile = File(..., description="原始合同 Word(.docx)"),
        target: UploadFile = File(..., description="回收件 PDF(.pdf)"),
        document_no: str = Form(...),
        original_page_count: int | None = Form(
            default=None,
            description="原始合同真实页数；仅用于显式截取回收 PDF",
        ),
        callback_url: str | None = Form(default=None),
        sync: bool = Form(default=False),
        _auth: None = Depends(require_external_api_key),
    ):
        """供外部系统调用的标准合同比对入口。

        默认异步(`sync=false`):返回 task_id,结果经回调或查询端点获取。
        `sync=true`:同步阻塞至比对完成,响应体内直接返回完整结果。
        """
        if not (source.filename or "").lower().endswith(".docx"):
            raise HTTPException(400, "source 必须为 .docx")
        if not (target.filename or "").lower().endswith(".pdf"):
            raise HTTPException(400, "target 必须为 .pdf")

        document_no = document_no.strip()
        if not document_no:
            raise HTTPException(400, "document_no 不能为空")
        if len(document_no) > 255:
            raise HTTPException(400, "document_no 不能超过 255 个字符")
        if original_page_count is not None and original_page_count < 1:
            raise HTTPException(400, "original_page_count 必须 >= 1")
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
        for field_name, upload in (("source", source), ("target", target)):
            if _upload_size(upload) > max_bytes:
                raise HTTPException(
                    413,
                    f"{field_name} 超过 {settings.external_max_upload_mb} MiB 上限",
                )

        # 与内部接口保持一致：超页数任务在入队前拒绝。
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

        # 全局并发上限走 PG 计数(跨 worker 一致);事务级检查,瞬时略超由执行端
        # 进程内信号量 + 任务排队消化。
        running = await asyncio.to_thread(db_repo.count_active_tasks)
        if running >= settings.max_concurrent_tasks:
            raise HTTPException(429, "并发任务已达上限,请稍后重试")

        task_id = task_manager.create(
            "compare",
            source_name=source.filename or "",
            target_names=[target.filename or ""],
            ocr_backend=settings.external_ocr_backend,
            callback_url=callback_url,
            document_no=document_no,
            external_request=True,
        )
        # 审计中间件读取:提交成功关联 task_id + document_no
        request.state.audit_endpoint = "contractCompare.submit"
        request.state.audit_task_id = task_id
        request.state.audit_document_no = document_no
        word_path = save_upload(source, task_id, "source")
        pdf_path = save_upload(target, task_id, "target")
        run_coro = task_manager.run(
            task_id,
            str(word_path),
            str(pdf_path),
            enable_llm_judge=settings.external_enable_llm_judge,
            enable_llm_alignment=settings.external_enable_llm_alignment,
            ocr_backend=settings.external_ocr_backend,
            enable_risk_assessment=settings.external_enable_risk_assessment,
            enable_llm_direct_diff=settings.external_enable_llm_direct_diff,
            truncate_to_original_pages=settings.external_truncate_to_original_pages,
            original_page_count=original_page_count,
        )
        if sync:
            # 同步模式:阻塞至比对完成,直接在响应体内返回完整结果。
            # run 内部已兜底异常(失败只置 status=failed + error,不向调用方抛)。
            await run_coro
            return JSONResponse(
                status_code=200,
                content=await _external_task_response(task_id),
            )
        asyncio.create_task(run_coro)
        return {"task_id": task_id, "document_no": document_no, "status": "pending"}

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

    @app.post("/api/v1/compare")
    async def compare(
        source: UploadFile = File(..., description="原始 Word(.docx)"),
        target: UploadFile = File(..., description="PDF 扫描件(.pdf)"),
        options: str | None = Form(default=None),
        callback_url: str | None = Form(default=None),
    ):
        # 基本类型校验
        if not (source.filename or "").lower().endswith(".docx"):
            raise HTTPException(400, "source 必须为 .docx")
        if not (target.filename or "").lower().endswith(".pdf"):
            raise HTTPException(400, "target 必须为 .pdf")

        # 解析 options(可选);提取 LLM 对齐/说明、OCR 与风险选项透传到 pipeline
        enable_llm_judge = False
        enable_llm_alignment = False
        ocr_backend: str | None = None
        enable_risk_assessment = False
        enable_llm_direct_diff = False
        truncate_to_original_pages = False
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

        # 限流:运行中任务达上限则拒绝(§14.4)
        # 全局并发上限走 PG 计数(跨 worker 一致);事务级检查,瞬时略超由执行端
        # 进程内信号量 + 任务排队消化。
        running = await asyncio.to_thread(db_repo.count_active_tasks)
        if running >= settings.max_concurrent_tasks:
            raise HTTPException(429, "并发任务已达上限,请稍后重试")

        # PDF 页数上限预检:超过配置上限直接拒绝,不进入流水线。
        # 0 表示不限制;读取 target 字节计数后回拨流,供 save_upload 再读一次。
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
            "compare",
            source_name=source.filename or "",
            target_names=[target.filename or ""],
            ocr_backend=ocr_backend,
            callback_url=callback_url,
        )
        word_path = save_upload(source, task_id, "source")
        pdf_path = save_upload(target, task_id, "target")
        asyncio.create_task(
            task_manager.run(
                task_id, str(word_path), str(pdf_path),
                enable_llm_judge=enable_llm_judge,
                enable_llm_alignment=enable_llm_alignment,
                ocr_backend=ocr_backend,
                enable_risk_assessment=enable_risk_assessment,
                enable_llm_direct_diff=enable_llm_direct_diff,
                truncate_to_original_pages=truncate_to_original_pages,
                original_page_count=original_page_count,
            )
        )
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
    async def download_report(task_id: str, format: str = "json"):
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
            target_path = effective_target_path(task_id)
            if target_path is None:
                raise HTTPException(404, "源 PDF 文件已过期,无法生成标注报告")
            out = settings.reports_dir / f"{task_id}_annotated.pdf"
            burn_pdf(target_path, report, out)
            return FileResponse(
                out,
                media_type="application/pdf",
                headers={
                    "Content-Disposition": f'attachment; filename="report-{task_id}.pdf"'
                },
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

    # —— 无标注版(纯文本 difflib 比对)端点:与 /api/v1/compare 完全独立 ——
    @app.post("/api/v1/raw-compare")
    async def raw_compare(
        source: UploadFile = File(..., description="原始 Word(.docx)"),
        target: UploadFile = File(..., description="PDF 扫描件(.pdf)"),
        options: str | None = Form(default=None),
        callback_url: str | None = Form(default=None),
    ):
        if not (source.filename or "").lower().endswith(".docx"):
            raise HTTPException(400, "source 必须为 .docx")
        if not (target.filename or "").lower().endswith(".pdf"):
            raise HTTPException(400, "target 必须为 .pdf")

        opts = RawCompareOptions()
        if options:
            try:
                opts = RawCompareOptions.model_validate_json(options)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(400, f"options 解析失败: {exc}")

        # 全局并发上限走 PG 计数(跨 worker 一致);事务级检查,瞬时略超由执行端
        # 进程内信号量 + 任务排队消化。
        running = await asyncio.to_thread(db_repo.count_active_tasks)
        if running >= settings.max_concurrent_tasks:
            raise HTTPException(429, "并发任务已达上限,请稍后重试")

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
        asyncio.create_task(
            task_manager.run_raw(
                task_id, str(word_path), str(pdf_path),
                char_level=opts.char_level, ocr_backend=opts.ocr_backend,
            )
        )
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

    # —— 对帐单金额统计端点:与 /api/v1/raw-compare 完全独立 ——
    # 单端 PDF(对帐单扫描件),支持一次上传多个 PDF;后端串行 OCR + 表格抽取 + 代码求和,
    # 聚合输出所有文件的总金额。LLM 仅用于列定位兜底,绝不参与数值识别或求和。
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

        # 限流:与 compare/raw 共享并发上限
        # 全局并发上限走 PG 计数(跨 worker 一致);事务级检查,瞬时略超由执行端
        # 进程内信号量 + 任务排队消化。
        running = await asyncio.to_thread(db_repo.count_active_tasks)
        if running >= settings.max_concurrent_tasks:
            raise HTTPException(429, "并发任务已达上限,请稍后重试")

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
        asyncio.create_task(
            task_manager.run_statement(
                task_id, pdf_paths, file_names,
                ocr_backend=opts.ocr_backend,
                amount_column_keywords=opts.amount_column_keywords,
                enable_llm_column_detection=opts.enable_llm_column_detection,
            )
        )
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
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        """分页查询任务历史列表(按 created_at 倒序)。

        查询参数:
          kind   - compare | raw | statement(可选筛选)
          status - pending | running | done | failed(可选筛选)
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
        # 空白 q 视为未搜索,避免空串退化为 %% 全表匹配
        q = q.strip() if q else None

        records, total = await asyncio.to_thread(
            db_repo.list_tasks,
            kind=kind, status=status, q=q, limit=limit, offset=offset,
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

    @app.get("/api/v1/tasks/{task_id}/llm-calls")
    async def list_task_llm_calls(task_id: str):
        """返回指定任务所有对话型 LLM 调用记录(按 id 升序,即调用发生顺序)。

        每条含 kind / attempt / status_code / elapsed_ms / payload(图片已脱敏)/
        response(截断 64KB)/ error。embedding 调用不入本表。用于「比对记录 →
        模型调用明细」调试与审计。
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

    # —— 前端静态文件(DC_STATIC_DIR 设置时启用,单容器部署用)——
    # 所有 /api、/health 路由已注册完毕,catch-all 放最后不会拦截 API。
    if settings.static_dir and settings.static_dir.is_dir():
        index_html = settings.static_dir / "index.html"
        assets_dir = settings.static_dir / "assets"

        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/{full_path:path}")
        async def spa_fallback(full_path: str):
            """非 API / 非静态资源的请求回退到 index.html,供 Vue Router history 模式。"""
            candidate = settings.static_dir / full_path  # type: ignore[arg-type]
            if full_path and candidate.is_file():
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
