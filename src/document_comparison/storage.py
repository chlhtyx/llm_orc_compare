"""文件与报告存储(本地文件系统,§11.2)。

生产可替换为对象存储。上传文件用后即删策略可由调用方按留存期清理。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import UploadFile

from .config import settings
from .models import StatementSummaryReport, TamperReport, TextDiffReport

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


def save_report(task_id: str, report: TamperReport) -> Path:
    settings.ensure_dirs()
    path = settings.reports_dir / f"{task_id}.json"
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    logger.debug("report saved task_id=%s path=%s", task_id, path)
    return path


def load_report(task_id: str) -> TamperReport | None:
    path = settings.reports_dir / f"{task_id}.json"
    if not path.exists():
        return None
    logger.debug("report loaded task_id=%s path=%s", task_id, path)
    return TamperReport.model_validate_json(path.read_text(encoding="utf-8"))


# —— 无标注版(纯文本 difflib)报告存取,文件名隔离 ——
def save_raw_report(task_id: str, report: TextDiffReport) -> Path:
    settings.ensure_dirs()
    path = settings.reports_dir / f"{task_id}.raw.json"
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_raw_report(task_id: str) -> TextDiffReport | None:
    path = settings.reports_dir / f"{task_id}.raw.json"
    if not path.exists():
        return None
    return TextDiffReport.model_validate_json(path.read_text(encoding="utf-8"))


# —— 对帐单金额统计报告存取(文件名 .statement.json 隔离)——
def save_statement_report(task_id: str, report: StatementSummaryReport) -> Path:
    settings.ensure_dirs()
    path = settings.reports_dir / f"{task_id}.statement.json"
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_statement_report(task_id: str) -> StatementSummaryReport | None:
    path = settings.reports_dir / f"{task_id}.statement.json"
    if not path.exists():
        return None
    return StatementSummaryReport.model_validate_json(path.read_text(encoding="utf-8"))


def report_path(task_id: str, fmt: str) -> Path:
    return settings.reports_dir / f"{task_id}.{fmt}"


def upload_path(task_id: str, role: str) -> Path | None:
    """查找已上传文件路径。role ∈ {source, target}。
    
    返回匹配的第一个文件(通过前缀 task_id-role 匹配,不限后缀)。
    不存在时返回 None。
    """
    for f in settings.uploads_dir.iterdir():
        if f.is_file() and f.name.startswith(f"{task_id}-{role}"):
            return f
    return None
