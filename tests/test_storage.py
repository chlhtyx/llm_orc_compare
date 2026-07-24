"""任务文件存储选择。"""
from pathlib import Path

from document_comparison.config import settings
from document_comparison.storage import compared_pdf_path, effective_target_path


def test_effective_target_prefers_persisted_compared_pdf(monkeypatch, tmp_path: Path):
    """截断任务的预览/导出必须取实际参与比对的 PDF。"""
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    settings.ensure_dirs()
    task_id = "truncated-task"
    original = settings.uploads_dir / f"{task_id}-target.pdf"
    original.write_bytes(b"original-full-pdf")

    assert effective_target_path(task_id) == original

    compared = compared_pdf_path(task_id)
    compared.write_bytes(b"truncated-pdf")
    assert effective_target_path(task_id) == compared
