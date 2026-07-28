"""统一日志配置:控制台 + 滚动文件双输出。

调用 `setup_logging()` 后,根 logger 同时输出到:
- 控制台(stderr):供 Docker / 终端实时捕获
- 文件 `logs/app.log`:`RotatingFileHandler`,单文件 10MB、保留 5 个

级别统一由环境变量 `DC_LOG_LEVEL` 控制(默认 INFO)。
第三方库(httpx / urllib3 / pymupdf)压到 WARNING,避免刷屏。

幂等:重复调用只配置一次(避免 handler 重复堆叠)。
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from .config import settings
from .observability import get_log_context

_FMT = "%(asctime)s | %(levelname)-7s | %(name)s | task=%(task_id)s req=%(request_id)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

# 噪音较大的第三方库统一压到 WARNING
_NOISY_LOGGERS = ("httpx", "httpcore", "urllib3", "pymupdf", "fitz", "openai")

_configured = False


class _ContextFilter(logging.Filter):
    """为每条日志补齐 task/request 标识，未关联时显式显示为 ``-``。"""

    def filter(self, record: logging.LogRecord) -> bool:
        context = get_log_context()
        record.task_id = context.get("task_id", "-")
        record.request_id = context.get("request_id", "-")
        return True


def setup_logging(force: bool = False) -> None:
    """配置根 logger(幂等)。

    - force=True:重新配置(测试用,清掉旧 handler)
    """
    global _configured
    root = logging.getLogger()

    if _configured and not force:
        return
    if force:
        for h in list(root.handlers):
            root.removeHandler(h)

    level = _parse_level(os.environ.get("DC_LOG_LEVEL", "INFO"))
    root.setLevel(level)

    formatter = logging.Formatter(_FMT, datefmt=_DATEFMT)

    # —— 控制台(stderr)——
    console = logging.StreamHandler(stream=sys.stderr)
    console.setFormatter(formatter)
    console.setLevel(level)
    console.addFilter(_ContextFilter())
    root.addHandler(console)

    # —— 滚动文件(storage_dir/logs/app.log)——
    # 目录创建失败不致命:退化为仅控制台,避免启动被日志卡住。
    try:
        log_dir = settings.storage_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "app.log",
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        file_handler.addFilter(_ContextFilter())
        root.addHandler(file_handler)
    except OSError as exc:
        # 此时日志系统尚未完全就绪,直接 stderr 提示
        sys.stderr.write(f"[logging] 文件 handler 初始化失败,仅控制台输出: {exc}\n")

    # 第三方库降噪
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    _configured = True


def _parse_level(value: str) -> int:
    """解析日志级别字符串(大小写不敏感),非法值回退 INFO。"""
    return getattr(logging, value.upper(), logging.INFO)
