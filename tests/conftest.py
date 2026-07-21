"""pytest 公共 fixture。

为 db 相关测试提供隔离的 Postgres 测试库:
- 优先用 DATABASE_URL_TEST(独立测试库);否则回退 DATABASE_URL。
- 未配置时,需要 db 的测试通过 test_db_url fixture 自动跳过,其它测试不受影响。

设置方式(示例):
    export DATABASE_URL_TEST="postgresql+psycopg://dc:dcpass@localhost:5432/doc_compare_test"

CI 推荐:启动 postgres 容器,把 DATABASE_URL_TEST 指向它。
"""
from __future__ import annotations

import os

import pytest


def _test_database_url() -> str | None:
    """返回测试用 DATABASE_URL,未配置返回 None。"""
    return os.environ.get("DATABASE_URL_TEST") or os.environ.get("DATABASE_URL")


@pytest.fixture()
def test_db_url() -> str:
    """需要 db 的测试显式 yield 依赖此 fixture;无 db url 时跳过。"""
    url = _test_database_url()
    if not url:
        pytest.skip("no DATABASE_URL_TEST / DATABASE_URL configured; skipping db tests")
    return url


@pytest.fixture()
def db_isolated(monkeypatch, test_db_url):
    """注入测试库引擎 + 清空所有表,保证每个用例从空表开始。

    需要 db 的测试显式声明参数 `db_isolated`,触发此 fixture。
    不会影响未声明此参数的纯单元测试。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from document_comparison.config import settings
    from document_comparison.db import Base
    from document_comparison.db import engine as engine_mod
    from document_comparison.db import models  # noqa: F401

    monkeypatch.setattr(settings, "database_url", test_db_url)

    # 全新引擎实例,避免污染进程级单例
    eng = create_engine(test_db_url, future=True)
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)

    # 顶替 repository.session_scope 使用的工厂,让所有调用走测试库
    test_factory = sessionmaker(bind=eng, expire_on_commit=False, future=True)
    monkeypatch.setattr(engine_mod, "_SessionFactory", test_factory)
    monkeypatch.setattr(engine_mod, "_engine", eng)
    monkeypatch.setattr(engine_mod, "get_engine", lambda: eng)
    monkeypatch.setattr(engine_mod, "check_connection", lambda: None)
    # 测试已用 create_all 建表,跳过 alembic 子进程(避免每个用例都跑迁移)
    monkeypatch.setattr(engine_mod, "_migrations_applied", True)
    monkeypatch.setattr(engine_mod, "run_migrations", lambda: None)

    yield eng

    eng.dispose()
