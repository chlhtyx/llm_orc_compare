"""FastAPI 应用:比对端点 + 对外集成(§7、§14)。

端点:
  POST /api/v1/compare              提交(支持 callback_url/callback_secret)
  GET  /api/v1/compare/{task_id}    查询结果
  GET  /api/v1/compare/{task_id}/events   SSE 进度(§7.3)
  GET  /api/v1/compare/{task_id}/report   下载报告(json/pdf)
  GET  /health
"""
from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..config import (
    llm_config_path,
    load_llm_overrides,
    save_llm_overrides,
    settings,
)
from ..models import CompareOptions
from ..storage import load_report, report_path, save_upload
from ..tasks import task_manager



def _mask_key(key: str) -> str:
    """API Key 脱敏返回:只露末 4 位,供前端判断是否已设置。"""
    if not key:
        return ""
    if len(key) <= 4:
        return "****"
    return "*" * (len(key) - 4) + key[-4:]


def create_app() -> FastAPI:
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

    @app.exception_handler(HTTPException)
    async def _http_exc_handler(_request, exc: HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": exc.status_code,
                "message": exc.detail,
                "request_id": uuid.uuid4().hex[:12],
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled_exc_handler(_request, exc: Exception):  # noqa: BLE001
        return JSONResponse(
            status_code=500,
            content={
                "code": 500,
                "message": f"internal error: {exc}",
                "request_id": uuid.uuid4().hex[:12],
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
            "embed_backend": settings.embed_backend,
            "persisted": load_llm_overrides(),
            "config_file": str(llm_config_path()),
        }

    @app.put("/api/v1/config/llm")
    async def put_llm_config(body: dict):
        """更新 LLM 配置并持久化。

        可选字段:llm_api_base, llm_api_key, llm_model,
        llm_timeout, llm_max_concurrency。空值/省略表示不修改(api_key 传
        空串则清除已保存的 key)。
        """
        allowed = {
            "llm_api_base", "llm_api_key", "llm_model",
            "llm_timeout", "llm_max_concurrency", "llm_max_retries",
            "embed_backend",
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
        if "embed_backend" in body and body["embed_backend"] not in ("mock", "bge"):
            raise HTTPException(400, "embed_backend 仅支持 mock | bge")

        # api_key 特殊处理:明文哨兵 "********" 表示"不修改"
        overrides = dict(body)
        if overrides.get("llm_api_key") == "********":
            overrides.pop("llm_api_key")

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
                "embed_backend": settings.embed_backend,
            },
        }

    @app.post("/api/v1/compare")
    async def compare(
        source: UploadFile = File(..., description="原始 Word(.docx)"),
        target: UploadFile = File(..., description="PDF 扫描件(.pdf)"),
        options: str | None = Form(default=None),
        callback_url: str | None = Form(default=None),
        callback_secret: str | None = Form(default=None),
    ):
        # 基本类型校验
        if not (source.filename or "").lower().endswith(".docx"):
            raise HTTPException(400, "source 必须为 .docx")
        if not (target.filename or "").lower().endswith(".pdf"):
            raise HTTPException(400, "target 必须为 .pdf")

        # 解析 options(可选)
        if options:
            try:
                CompareOptions.model_validate_json(options)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(400, f"options 解析失败: {exc}")

        # 限流:运行中任务达上限则拒绝(§14.4)
        running = sum(
            1
            for t in task_manager._tasks.values()
            if t.info.status in ("pending", "running")
        )
        if running >= settings.max_concurrent_tasks:
            raise HTTPException(429, "并发任务已达上限,请稍后重试")

        task_id = task_manager.create(callback_url, callback_secret)
        word_path = save_upload(source, task_id, "source")
        pdf_path = save_upload(target, task_id, "target")
        asyncio.create_task(
            task_manager.run(task_id, str(word_path), str(pdf_path))
        )
        return {"task_id": task_id, "status": "pending"}

    @app.get(
        "/api/v1/compare/{task_id}"
    )
    async def get_result(task_id: str):
        task = task_manager.get(task_id)
        if task is None:
            raise HTTPException(404, "task not found")
        resp = task.info.model_dump()
        if task.info.status == "done" and task.report is not None:
            resp["report"] = task.report.model_dump()
        elif task.info.status == "done":
            # 进程重启后内存丢失,从存储回捞
            report = load_report(task_id)
            if report is not None:
                resp["report"] = report.model_dump()
        return resp

    @app.get(
        "/api/v1/compare/{task_id}/events",
    )
    async def events(task_id: str):
        task = task_manager.get(task_id)
        if task is None:
            raise HTTPException(404, "task not found")

        async def gen():
            async for ev in task_manager.event_stream(task_id):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            yield f"event: done\ndata: {json.dumps({'status': task.info.status}, ensure_ascii=False)}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get(
        "/api/v1/compare/{task_id}/report",
    )
    async def download_report(task_id: str, format: str = "json"):
        task = task_manager.get(task_id)
        report = task.report if task else None
        if report is None:
            report = load_report(task_id)
        if report is None:
            raise HTTPException(404, "report not ready")

        if format == "json":
            return JSONResponse(content=report.model_dump())
        if format == "pdf":
            # §5.6 烧录高亮 PDF 暂未实现(待 reportlab/PyMuPDF 批注)
            raise HTTPException(501, "pdf 烧录报告尚未实现,请用 format=json")
        raise HTTPException(400, "format 仅支持 json|pdf")

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
