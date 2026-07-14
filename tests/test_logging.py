"""日志配置测试:验证 setup_logging 双输出(控制台 + 滚动文件)。"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from document_comparison.config import settings
from document_comparison.logging_config import setup_logging


@pytest.fixture
def isolated_logging(monkeypatch, tmp_path):
    """把 storage_dir 指到临时目录,避免污染真实日志目录。"""
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    # setup_logging 内部用模块级 _configured 幂等锁,force=True 强制重配
    setup_logging(force=True)
    yield
    # 清理 handler,避免污染后续测试
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:  # noqa: BLE001
            pass
    # 重置幂等锁,让后续进程内调用可重新配置
    from document_comparison import logging_config
    logging_config._configured = False


def _handler_types() -> set[str]:
    return {type(h).__name__ for h in logging.getLogger().handlers}


def test_console_and_file_handlers(isolated_logging):
    """setup 后根 logger 同时挂载 StreamHandler 与 RotatingFileHandler。"""
    types = _handler_types()
    assert "StreamHandler" in types
    assert "RotatingFileHandler" in types


def test_log_file_created(isolated_logging):
    """日志文件创建到 storage_dir/logs/app.log。"""
    log_path: Path = settings.storage_dir / "logs" / "app.log"
    assert log_path.exists()


def test_log_actually_written(isolated_logging):
    """记录的 INFO 消息真实落到文件。"""
    log = logging.getLogger("test.logging")
    log.info("a-unique-marker-9f3a")
    # flush 所有 handler
    for h in logging.getLogger().handlers:
        h.flush()
    content = (settings.storage_dir / "logs" / "app.log").read_text(encoding="utf-8")
    assert "a-unique-marker-9f3a" in content


def test_noisy_loggers_silenced(isolated_logging):
    """第三方噪音库被压到 WARNING。"""
    for name in ("httpx", "urllib3", "pymupdf"):
        assert logging.getLogger(name).level >= logging.WARNING


def test_level_env(isolated_logging, monkeypatch):
    """DC_LOG_LEVEL=DEBUG 后根 logger 级别变为 DEBUG。"""
    monkeypatch.setenv("DC_LOG_LEVEL", "DEBUG")
    setup_logging(force=True)
    assert logging.getLogger().level == logging.DEBUG


def test_idempotent(isolated_logging):
    """重复调用不重复堆叠 handler。"""
    before = len(logging.getLogger().handlers)
    setup_logging()  # 非 force
    after = len(logging.getLogger().handlers)
    assert before == after
