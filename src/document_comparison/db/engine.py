"""SQLAlchemy 引擎与会话管理。

设计要点:
- 进程级单例引擎,懒加载(`init_engine` 显式触发,通常在 FastAPI 启动时)。
- `session_scope` 上下文管理器统一提交/回滚,封装日志,避免上层漏 commit。
- `check_connection` 启动期 SELECT 1 健康检查;硬依赖 PG,失败即抛错。
- 自动建表仅用于开发/测试(`DC_DB_AUTO_CREATE=1`),生产用 Alembic。
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from ..config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """所有 ORM 模型的公共基类(SQLAlchemy 2.0 风格)。"""


_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None
_migrations_applied: bool = False  # 进程内幂等:run_migrations 只真正执行一次


def init_engine() -> Engine:
    """根据 settings.database_url 创建引擎并缓存为进程单例。

    未配置 database_url 时抛 RuntimeError(硬依赖)。
    重复调用幂等(返回已存在的引擎)。
    """
    global _engine, _SessionFactory
    if _engine is not None:
        return _engine

    url = settings.database_url.strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL 未配置;Postgres 为必选依赖,"
            "请在环境变量或 docker-compose 中提供。"
        )

    _engine = create_engine(
        url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_pre_ping=True,  # 长连接复用前先 ping,避免 stale 连接
        future=True,
    )
    _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    logger.info(
        "db engine initialized pool_size=%s max_overflow=%s",
        settings.db_pool_size, settings.db_max_overflow,
    )
    return _engine


def get_engine() -> Engine:
    """返回已初始化的引擎;未初始化时抛错(保护性)。"""
    if _engine is None:
        raise RuntimeError("db engine not initialized; call init_engine() first")
    return _engine


def dispose_engine() -> None:
    """释放引擎(测试切换 database_url 时使用)。"""
    global _engine, _SessionFactory, _migrations_applied
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None
    _migrations_applied = False  # 允许测试切换库后重新跑迁移


def check_connection() -> None:
    """启动期健康检查:SELECT 1。失败抛错让 uvicorn 拒绝启动。"""
    eng = get_engine()
    with eng.connect() as conn:
        conn.execute(text("SELECT 1"))
    logger.info("db connection ok")


def create_all() -> None:
    """开发/测试便捷:Base.metadata.create_all。生产用 Alembic。"""
    init_engine()
    # 延迟导入,避免循环(models.py 也导入 engine.Base)
    from . import models  # noqa: F401
    Base.metadata.create_all(get_engine())
    logger.info("db tables ensured (create_all)")


def _find_alembic_dir() -> Path:
    """定位 alembic/ 脚本目录(含 env.py 与 versions/)。

    顺序查找,首个命中即用:
      1. DC_ALEMBIC_INI 环境变量(显式指定 alembic.ini 路径)
      2. cwd/alembic.ini(本地开发从仓库根启动)
      3. /app/alembic.ini(docker 镜像 WORKDIR)
      4. __file__ 推导的仓库根(editable install,如 pip install -e .)

    找不到则抛 RuntimeError,带所有尝试过的候选路径,便于排错。
    """
    from pathlib import Path

    candidates: list[Path] = []

    env_ini = os.environ.get("DC_ALEMBIC_INI", "").strip()
    if env_ini:
        candidates.append(Path(env_ini))

    candidates.extend([
        Path.cwd() / "alembic.ini",               # 本地开发
        Path("/app/alembic.ini"),                 # docker WORKDIR
        Path(__file__).resolve().parents[3] / "alembic.ini",  # editable install
    ])

    for ini in candidates:
        if ini.is_file():
            # alembic.ini 同级或子目录下的 alembic/ 即 script_location
            alembic_dir = ini.parent / "alembic"
            if alembic_dir.is_dir():
                return alembic_dir

    searched = "\n  ".join(str(c) for c in candidates)
    raise RuntimeError(
        f"alembic.ini or alembic/ script dir not found.\n"
        f"Searched:\n  {searched}\n"
        f"Set DC_ALEMBIC_INI to the absolute path of alembic.ini, "
        f"or run from the repo root."
    )


def run_migrations() -> None:
    """启动期自动跑 `alembic upgrade head`(进程内 API,不起子进程)。

    保证 docker compose up 后无需手动进容器跑迁移即可用。失败抛错让
    uvicorn 拒绝启动,避免 schema 未就位导致运行时查询失败。

    实现说明:
    - 直接调用 alembic Python API(commands.upgrade + Config),不依赖子进程,
      避免 cwd / 环境变量继承的脆弱性。
    - DATABASE_URL 直接从 settings 注入到 env.py 配置,跳过 alembic.ini 占位 url。
    - alembic 脚本目录通过 _find_alembic_dir 多候选位置查找
      (DC_ALEMBIC_INI > cwd > /app > editable install)。

    进程内幂等:重复调用直接返回(alembic 本身也幂等,这只是避免日志重复)。
    测试场景需重跑时调用 `dispose_engine()`(同时重置此标志)。
    """
    global _migrations_applied
    if _migrations_applied:
        return

    from alembic import command
    from alembic.config import Config

    alembic_dir = _find_alembic_dir()
    cfg = Config()
    cfg.set_main_option("script_location", str(alembic_dir))
    # env.py 会从 settings.database_url 读取,这里同时设一份兜底
    cfg.set_main_option("sqlalchemy.url", settings.database_url)

    try:
        command.upgrade(cfg, "head")
        logger.info("alembic upgrade head completed (script_location=%s)", alembic_dir)
        _migrations_applied = True
    except Exception as exc:
        logger.exception("alembic upgrade head failed (script_location=%s)", alembic_dir)
        raise RuntimeError(
            f"alembic upgrade head failed: {exc}"
        ) from exc


@contextmanager
def session_scope() -> Iterator[Session]:
    """统一会话上下文:正常退出 commit,异常 rollback + 记日志(不抛)。

    设计为「写库失败不拖垮任务」:调用方拿到异常时,业务流程继续。
    """
    if _SessionFactory is None:
        raise RuntimeError("db session factory not initialized; call init_engine() first")
    session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        logger.warning("db session error: %s", exc)
        raise
    finally:
        session.close()
