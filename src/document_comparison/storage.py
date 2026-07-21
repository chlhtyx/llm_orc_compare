"""文件存储(本地文件系统,§11.2)。

职责仅限于**上传的原始文件**(source docx / target pdf)落盘与查找;
比对报告 JSON 不再写文件,统一持久化到 Postgres(见 db/repository.py)。
生产可替换为对象存储。
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import UploadFile

from .config import settings

logger = logging.getLogger(__name__)


def save_upload(upload: UploadFile, task_id: str, role: str, index: int | None = None) -> Path:
    """保存上传文件。role ∈ {source, target}。

    index 非 None 时(对帐单多文件场景),命名为 f"{task_id}-{role}-{index}{suffix}",
    避免同 task 多 PDF 互相覆盖。index 为 None 保持原命名(单文件 source/target 向后兼容)。
    """
    settings.ensure_dirs()
    suffix = Path(upload.filename or "").suffix or ".bin"
    if index is None:
        path = settings.uploads_dir / f"{task_id}-{role}{suffix}"
    else:
        path = settings.uploads_dir / f"{task_id}-{role}-{index}{suffix}"
    with path.open("wb") as f:
        f.write(upload.file.read())
    return path


def upload_path(task_id: str, role: str) -> Path | None:
    """查找已上传文件路径。role ∈ {source, target}。

    返回匹配的第一个文件(通过前缀 task_id-role 匹配,不限后缀)。
    不存在时返回 None。
    """
    for f in settings.uploads_dir.iterdir():
        if f.is_file() and f.name.startswith(f"{task_id}-{role}"):
            return f
    return None
