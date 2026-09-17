"""发布接单屏障与就绪检查；不包含分析业务逻辑。"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import logging
from pathlib import Path
import tempfile
import uuid

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from starlette.responses import JSONResponse

from .config import settings
from .db import engine, releases

logger = logging.getLogger(__name__)
# 跨 to_thread 传播同一列表：业务写库继续只记日志，发布排空却不能误报成功。
persistence_errors: ContextVar[list | None] = ContextVar("release_persistence_errors", default=None)


def record_persistence_error():
    errors = persistence_errors.get()
    if errors is not None:
        errors.append(True)


class DeploymentAdmissionMiddleware:
    """在读取上传内容之前登记操作；数据库异常拒绝新写请求。

    默认覆盖所有 API 写操作（包含恢复、停止、回调重推、配置修改）；
    登录/退出与 GET 查询仍可使用。操作跨响应和异步审计，取消时保留屏障。
    """

    def __init__(self, app, owner: str, on_rejected=None):
        self.app = app
        self.owner = owner
        self.on_rejected = on_rejected

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        guarded = (scope["type"] == "http" and path.startswith("/api/")
                   and scope["method"] not in {"GET", "HEAD", "OPTIONS"}
                   and not path.startswith("/api/v1/auth/"))
        if not guarded:
            return await self.app(scope, receive, send)
        activity_id = uuid.uuid4().hex
        try:
            accepted = await asyncio.to_thread(
                releases.begin_request, releases.DEPLOYMENT_ID, self.owner,
                f'{scope["method"]} {path}', activity_id,
            )
        except Exception:
            logger.error("release admission unavailable", exc_info=True)
            accepted = False
        if not accepted:
            state = scope.setdefault("state", {})
            request_id = state.setdefault("request_id", uuid.uuid4().hex[:12])
            response = JSONResponse(
                {"code": 503, "message": "服务发布维护中，暂不接受新任务或修改，请稍后重试",
                 "request_id": request_id}, status_code=503,
                headers={"Retry-After": "60",
                         "X-Request-Id": request_id},
            )
            await response(scope, receive, send)
            if self.on_rejected is not None and path.startswith("/api/v1/external/"):
                await self.on_rejected(scope)
            return
        errors: list = []
        context_token = persistence_errors.set(errors)
        scope["release_background"] = []
        try:
            await self.app(scope, receive, send)
            if scope["release_background"]:
                results = await asyncio.gather(*scope["release_background"], return_exceptions=True)
                errors.extend(r for r in results if isinstance(r, BaseException))
            if not errors:
                try:
                    await asyncio.to_thread(releases.finish_activity, activity_id)
                except Exception:
                    logger.error("release activity cleanup failed activity=%s", activity_id)
            else:
                logger.error("release activity retained after persistence failure activity=%s", activity_id)
        finally:
            # 取消/未处理异常不清除操作：后台线程可能仍在写文件或数据库。
            persistence_errors.reset(context_token)


def check_readiness():
    """检查本地依赖，不调用收费模型或暴露连接串。STOPPED 也可以 prepared。"""
    checks = {}
    mode = "UNKNOWN"
    try:
        with engine.get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
            current = set(MigrationContext.configure(conn).get_current_heads())
        checks["database"] = True
        cfg = Config()
        cfg.set_main_option("script_location", str(engine._find_alembic_dir()))
        expected = set(ScriptDirectory.from_config(cfg).get_heads())
        checks["schema"] = bool(expected) and current == expected
        state = releases.status(releases.DEPLOYMENT_ID)
        mode = state["mode"]
        checks["deployment"] = state["version"] == settings.version
    except Exception:
        checks["database_or_schema"] = False
    for label, directory in (("uploads", settings.uploads_dir), ("reports", settings.reports_dir)):
        try:
            Path(directory).mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=directory) as f:
                f.write(b"release-ready")
                f.flush()
                f.seek(0)
                assert f.read() == b"release-ready"
            checks[label] = True
        except Exception:
            checks[label] = False
    return {"deployment_id": releases.DEPLOYMENT_ID, "version": settings.version,
            "mode": mode, "checks": checks, "prepared": bool(checks) and all(checks.values())}
