"""任务文件存储选择。"""
import io
from pathlib import Path

from fastapi import UploadFile

from document_comparison.config import settings
from document_comparison.storage import (
    compared_pdf_path,
    effective_target_path,
    finalize_temp_file,
    save_upload,
    upload_path,
)


def _upload(name: str, payload: bytes = b"x") -> UploadFile:
    return UploadFile(file=io.BytesIO(payload), filename=name)


def test_save_upload_collects_files_into_task_dir(monkeypatch, tmp_path: Path):
    """上传件按 task_id 归集到 uploads/{task_id}/ 子目录,文件名不再带任务前缀。"""
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    settings.ensure_dirs()
    task_id = "task-dir-1"
    source = save_upload(_upload("contract.docx", b"docx"), task_id, "source")
    target = save_upload(_upload("scan.pdf", b"pdf"), task_id, "target")
    multi = save_upload(_upload("stmt-0.pdf", b"pdf0"), task_id, "target", index=0)

    assert source == settings.uploads_dir / task_id / "source.docx"
    assert target == settings.uploads_dir / task_id / "target.pdf"
    assert multi == settings.uploads_dir / task_id / "target-0.pdf"
    assert upload_path(task_id, "source") == source
    # 同目录同时存在单文件与多文件命名时,单文件优先
    assert upload_path(task_id, "target") == target


def test_upload_path_prefers_indexed_file_when_single_missing(monkeypatch, tmp_path: Path):
    """对帐单任务只有 target-N 命名时,仍能按 role 命中第一个分片。"""
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    settings.ensure_dirs()
    task_id = "task-stmt-1"
    first = save_upload(_upload("stmt-0.pdf", b"pdf0"), task_id, "target", index=0)
    save_upload(_upload("stmt-1.pdf", b"pdf1"), task_id, "target", index=1)
    assert upload_path(task_id, "target") == first


def test_finalize_temp_file_moves_into_task_dir(monkeypatch, tmp_path: Path):
    """URL 下载的临时文件落定到任务子目录,与 save_upload 命名一致。"""
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    settings.ensure_dirs()
    task_id = "task-url-1"
    temp = settings.uploads_dir / ".pending-target-abc123.pdf"
    temp.write_bytes(b"downloaded")

    final = finalize_temp_file(temp, task_id, "target", "downloaded.pdf")

    assert final == settings.uploads_dir / task_id / "target.pdf"
    assert final.read_bytes() == b"downloaded"
    assert not temp.exists()
    assert upload_path(task_id, "target") == final


def test_upload_path_falls_back_to_legacy_flat_naming(monkeypatch, tmp_path: Path):
    """历史平铺命名(uploads/{task_id}-role.*)无需迁移,仍可读取。"""
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    settings.ensure_dirs()
    legacy_source = settings.uploads_dir / "legacy-task-source.pdf"
    legacy_target = settings.uploads_dir / "legacy-task-target.pdf"
    legacy_source.write_bytes(b"old-source")
    legacy_target.write_bytes(b"old-target")

    assert upload_path("legacy-task", "source") == legacy_source
    assert upload_path("legacy-task", "target") == legacy_target


def test_upload_path_task_dir_takes_priority_over_legacy(monkeypatch, tmp_path: Path):
    """新归集目录命中时优先于旧平铺残留。"""
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    settings.ensure_dirs()
    task_id = "task-both"
    nested = save_upload(_upload("new.pdf", b"new"), task_id, "target")
    legacy = settings.uploads_dir / f"{task_id}-target.pdf"
    legacy.write_bytes(b"legacy")

    assert upload_path(task_id, "target") == nested


def test_effective_target_prefers_persisted_compared_pdf(monkeypatch, tmp_path: Path):
    """截断任务的预览/导出必须取实际参与比对的 PDF。"""
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    settings.ensure_dirs()
    task_id = "truncated-task"
    original = save_upload(_upload("full.pdf", b"original-full-pdf"), task_id, "target")

    assert effective_target_path(task_id) == original

    compared = compared_pdf_path(task_id)
    compared.write_bytes(b"truncated-pdf")
    assert effective_target_path(task_id) == compared
