"""Gunicorn 配置(生产启动入口)。

通过 Dockerfile CMD `gunicorn -c gunicorn.conf.py document_comparison.api.app:app` 加载。
本地开发仍可用 `python -m document_comparison.api.app`(走 api/app.py 的 __main__,
单进程 uvicorn,不改本文件)。

并发模型:uvicorn worker(UvicornWorker),每个 worker 是独立的 uvicorn 进程,
内部各自跑一个 asyncio 事件循环。worker 之间通过 Postgres 共享任务状态
(见 tasks.py / api/app.py 的 PG 计数与 SSE 改造),无需共享内存或 Redis。
"""
import os

# worker 数,默认 1(行为与单进程 uvicorn 完全一致);>1 启用多 worker。
# 注意:每个 worker 独立持有 DB 连接池(pool_size+max_overflow),需确认
# PG max_connections 足够(默认 100;4 worker × 15 = 60,余量充足)。
workers = int(os.environ.get("DC_UVICORN_WORKERS", "1"))

# uvicorn 异步 worker:支持 async 路由 + SSE 长连接。
worker_class = "uvicorn.workers.UvicornWorker"

bind = f"{os.environ.get('DC_HOST', '0.0.0.0')}:{os.environ.get('DC_PORT', '8000')}"

# 比对任务可能跑几分钟,SSE / 外部同步模式是长连接;
# 设 0 禁用 gunicorn 的 worker 超时杀(由 uvicorn 自己管理连接超时)。
timeout = 0
graceful_timeout = 120
keepalive = 5

# 必须 False:TaskManager 单例 + asyncio.Semaphore 不能跨 fork 共享,
# 每个 worker 需独立 import、独立创建。
preload_app = False

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("DC_GUNICORN_LOG_LEVEL", "info")
