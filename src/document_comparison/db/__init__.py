"""SQLAlchemy + Postgres 持久化层。

包职责:
- `engine`:引擎/会话工厂、连接健康检查、session_scope 上下文。
- `models`:ORM 表定义(`task_records`、`task_events`、`llm_config`)。
- `repository`:同步数据访问 API,供 `tasks.py` / `api/app.py` 调用。

写库走 `asyncio.to_thread`,与现有 storage.py 文件风格一致,
不阻塞 asyncio 主循环。PG 为硬依赖,未配置 `DATABASE_URL` 时启动失败。
"""
from .engine import (
    Base,
    check_connection,
    create_all,
    dispose_engine,
    get_engine,
    init_engine,
    run_migrations,
    session_scope,
)
from .models import LlmConfigRecord, TaskEvent, TaskRecord
from . import repository

__all__ = [
    "Base",
    "LlmConfigRecord",
    "TaskEvent",
    "TaskRecord",
    "check_connection",
    "create_all",
    "dispose_engine",
    "get_engine",
    "init_engine",
    "run_migrations",
    "session_scope",
    "repository",
]
