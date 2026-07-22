"""启动期自动迁移(alembic upgrade head)的端到端测试。

独立于 test_db.py:不使用 db_isolated fixture(它 mock 掉 run_migrations),
需要真实跑 alembic 进程内 API 验证 schema 自动就位的行为,模拟 docker compose 首次启动。
"""
from __future__ import annotations

import pytest


def test_run_migrations_creates_missing_tables(test_db_url, monkeypatch):
    """run_migrations 应能从空库自动建表。

    模拟 docker compose 首次启动:库存在但表未建,启动时 alembic upgrade head
    应建出 task_records / task_events / alembic_version。
    """
    from sqlalchemy import create_engine, inspect, text

    from document_comparison.config import settings
    from document_comparison.db import engine as engine_mod

    monkeypatch.setattr(settings, "database_url", test_db_url)
    # dispose 让 _migrations_applied 重置,确保真的会跑一次
    engine_mod.dispose_engine()

    # 1. drop 所有表(包含 alembic_version),确保库为空
    eng = create_engine(test_db_url, future=True)
    with eng.begin() as conn:
        for table in (
            "external_api_calls", "task_llm_calls", "task_events",
            "task_records", "llm_config", "alembic_version",
        ):
            conn.execute(text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))
    assert inspect(eng).get_table_names() == []

    # 2. 跑 run_migrations(真实子进程)
    engine_mod.run_migrations()

    # 3. 验证表都被建回来
    tables = set(inspect(eng).get_table_names())
    assert "task_records" in tables
    assert "task_events" in tables
    assert "llm_config" in tables
    assert "task_llm_calls" in tables
    assert "external_api_calls" in tables
    assert "alembic_version" in tables

    eng.dispose()
    engine_mod.dispose_engine()


def test_run_migrations_idempotent_within_process(test_db_url, monkeypatch):
    """run_migrations 进程内幂等:第二次调用直接返回,不重复调 alembic command。"""
    from document_comparison.config import settings
    from document_comparison.db import engine as engine_mod

    monkeypatch.setattr(settings, "database_url", test_db_url)
    engine_mod.dispose_engine()

    # 第一次:真实跑
    engine_mod.run_migrations()
    assert engine_mod._migrations_applied is True

    # 第二次:应该直接返回(用 monkeypatch 探测 alembic command.upgrade 未被调用)
    called = {"v": False}
    from alembic import command
    orig_upgrade = command.upgrade

    def _spy(*args, **kwargs):
        called["v"] = True
        return orig_upgrade(*args, **kwargs)
    monkeypatch.setattr(command, "upgrade", _spy)

    engine_mod.run_migrations()
    assert called["v"] is False, "run_migrations 应在已应用后跳过 alembic command 调用"

    engine_mod.dispose_engine()


def test_run_migrations_failure_raises(test_db_url, monkeypatch):
    """alembic 迁移失败时应抛 RuntimeError,让 uvicorn 拒绝启动。"""
    from document_comparison.config import settings
    from document_comparison.db import engine as engine_mod

    monkeypatch.setattr(settings, "database_url", test_db_url)
    engine_mod.dispose_engine()

    # 让 alembic 的 command.upgrade 抛错
    def _fail(*args, **kwargs):
        raise RuntimeError("simulated alembic error")
    from alembic import command
    monkeypatch.setattr(command, "upgrade", _fail)

    with pytest.raises(RuntimeError, match="alembic upgrade head failed"):
        engine_mod.run_migrations()

    engine_mod.dispose_engine()


def test_find_alembic_dir_explicit_env(monkeypatch, tmp_path):
    """DC_ALEMBIC_INI 环境变量优先级最高,允许显式指定。"""
    # 造一个假的 alembic 目录结构
    fake_ini = tmp_path / "alembic.ini"
    fake_ini.write_text("# test")
    fake_dir = tmp_path / "alembic"
    fake_dir.mkdir()
    monkeypatch.setenv("DC_ALEMBIC_INI", str(fake_ini))

    from document_comparison.db.engine import _find_alembic_dir
    assert _find_alembic_dir() == fake_dir
