"""_maybe_import_legacy_llm_config_file 一次性数据迁移测试。

依赖 Postgres。无 DATABASE_URL_TEST / DATABASE_URL 时自动跳过。
"""
from __future__ import annotations

import json

import pytest

from document_comparison.config import (
    _legacy_llm_config_path,
    _maybe_import_legacy_llm_config_file,
)
from document_comparison.db import repository as db_repo


pytestmark = pytest.mark.usefixtures("db_isolated")


def _patch_legacy_path(monkeypatch, tmp_path, *, exists: bool, payload=None):
    """把 _legacy_llm_config_path 指向 tmp_path 下可控文件。

    exists=False 时只改路径不写文件;exists=True 时按 payload 写 JSON。
    """
    target = tmp_path / "llm_config.json"
    if exists:
        target.write_text(json.dumps(payload or {}), encoding="utf-8")
    monkeypatch.setattr(
        "document_comparison.config._legacy_llm_config_path", lambda: target
    )
    return target


def test_imports_from_file_when_pg_empty(db_isolated, monkeypatch, tmp_path):
    """PG 空 + 文件存在 → 导入字段到 PG;文件保留不删。"""
    path = _patch_legacy_path(
        monkeypatch, tmp_path, exists=True,
        payload={
            "llm_api_base": "https://example.com/v1",
            "llm_api_key": "sk-legacy",
            "max_pdf_pages": 9,
            # 非白名单字段应被丢弃
            "evil_extra": "should-be-dropped",
            # 空值字段应被剔除(回退默认)
            "llm_model": "",
        },
    )

    _maybe_import_legacy_llm_config_file()

    persisted = db_repo.get_llm_config()
    assert persisted["llm_api_base"] == "https://example.com/v1"
    assert persisted["llm_api_key"] == "sk-legacy"
    assert persisted["max_pdf_pages"] == 9
    assert "evil_extra" not in persisted       # 白名单过滤
    assert "llm_model" not in persisted        # 空值剔除
    # 文件保留作备份
    assert path.exists(), "legacy file should be kept as backup"


def test_skips_import_when_pg_already_has_data(db_isolated, monkeypatch, tmp_path):
    """PG 已有配置 → 不再读文件,保留现状。"""
    db_repo.save_llm_config({"max_pdf_pages": 42})
    path = _patch_legacy_path(
        monkeypatch, tmp_path, exists=True,
        payload={"max_pdf_pages": 999, "llm_api_key": "sk-overwrite"},
    )

    _maybe_import_legacy_llm_config_file()

    persisted = db_repo.get_llm_config()
    assert persisted["max_pdf_pages"] == 42, "PG 既有数据不应被文件覆盖"
    assert "llm_api_key" not in persisted
    # 文件不应被删除
    assert path.exists()


def test_corrupt_file_is_warned_not_raised(db_isolated, monkeypatch, tmp_path):
    """文件损坏 → 仅 warning,不抛;PG 保持空。"""
    path = tmp_path / "llm_config.json"
    path.write_text("not a valid json {{{", encoding="utf-8")
    monkeypatch.setattr(
        "document_comparison.config._legacy_llm_config_path", lambda: path
    )

    # 不应抛错
    _maybe_import_legacy_llm_config_file()

    assert db_repo.get_llm_config() == {}, "PG should remain empty on import failure"


def test_no_file_starts_fresh(db_isolated, monkeypatch, tmp_path):
    """文件不存在 → 仅记 info,PG 保持空(稍后由默认值起步)。"""
    _patch_legacy_path(monkeypatch, tmp_path, exists=False)

    _maybe_import_legacy_llm_config_file()

    assert db_repo.get_llm_config() == {}


def test_file_with_only_empty_values_is_skipped(db_isolated, monkeypatch, tmp_path):
    """文件存在但所有白名单字段均为空 → 跳过导入,PG 保持空。"""
    _patch_legacy_path(
        monkeypatch, tmp_path, exists=True,
        payload={"llm_api_base": "", "llm_api_key": "", "llm_model": ""},
    )

    _maybe_import_legacy_llm_config_file()

    assert db_repo.get_llm_config() == {}
