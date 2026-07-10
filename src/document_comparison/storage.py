"""文件与报告存储(本地文件系统,§11.2)。

生产可替换为对象存储。上传文件用后即删策略可由调用方按留存期清理。
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import UploadFile

from .config import settings
from .models import TamperReport


def save_upload(upload: UploadFile, task_id: str, role: str) -> Path:
    """保存上传文件。role ∈ {source, target}。"""
    settings.ensure_dirs()
    suffix = Path(upload.filename or "").suffix or ".bin"
    path = settings.uploads_dir / f"{task_id}-{role}{suffix}"
    with path.open("wb") as f:
        f.write(upload.file.read())
    return path


def save_report(task_id: str, report: TamperReport) -> Path:
    settings.ensure_dirs()
    path = settings.reports_dir / f"{task_id}.json"
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_report(task_id: str) -> TamperReport | None:
    path = settings.reports_dir / f"{task_id}.json"
    if not path.exists():
        return None
    return TamperReport.model_validate_json(path.read_text(encoding="utf-8"))


def report_path(task_id: str, fmt: str) -> Path:
    return settings.reports_dir / f"{task_id}.{fmt}"
