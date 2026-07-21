"""数据访问层:任务记录与里程碑事件的 CRUD。

同步 API;上层(tasks.py / api/app.py)通过 asyncio.to_thread 调用,
与现有 storage.py 风格一致,不阻塞 asyncio 事件循环。

报告 JSONB 写入:接收 pydantic 模型,内部 model_dump() 后存 JSONB;
读取时调用方负责按 kind 还原成对应 pydantic 模型。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    StatementSummaryReport,
    TamperReport,
    TextDiffReport,
    TaskStatus,
)
from .engine import session_scope
from .models import LlmConfigRecord, TaskEvent, TaskRecord

logger = logging.getLogger(__name__)


# —— 任务记录 CRUD ——

def create_task(
    task_id: str,
    kind: str,
    *,
    source_name: str = "",
    target_names: Iterable[str] | None = None,
    ocr_backend: str | None = None,
) -> None:
    """任务提交时调用,创建一条 pending 记录。

    幂等性:若已存在同 task_id(同进程内极少发生),更新而非报错。
    """
    targets = list(target_names) if target_names else []
    with session_scope() as s:
        existing = s.get(TaskRecord, task_id)
        if existing is not None:
            existing.kind = kind
            existing.source_name = source_name
            existing.target_names = targets
            existing.ocr_backend = ocr_backend
            existing.status = "pending"
            return
        s.add(TaskRecord(
            task_id=task_id,
            kind=kind,
            status="pending",
            source_name=source_name,
            target_names=targets,
            ocr_backend=ocr_backend,
        ))


def update_task_status(
    task_id: str,
    status: TaskStatus,
    *,
    overall_risk: str | None = None,
    change_status: str | None = None,
    elapsed: float | None = None,
    stage_timings: dict[str, float] | None = None,
    error: str | None = None,
    finished: bool = False,
) -> None:
    """更新任务状态;None 字段保持原值(不覆盖)。finished=True 时写 finished_at。"""
    with session_scope() as s:
        rec = s.get(TaskRecord, task_id)
        if rec is None:
            logger.warning("update_task_status: task_id=%s not found", task_id)
            return
        rec.status = status
        if overall_risk is not None:
            rec.overall_risk = overall_risk
        if change_status is not None:
            rec.change_status = change_status
        if elapsed is not None:
            rec.elapsed = elapsed
        if stage_timings is not None:
            rec.stage_timings = dict(stage_timings)
        if error is not None:
            # 截断防止超长错误堆栈撑爆列
            rec.error = error[:2048]
        if finished:
            rec.finished_at = datetime.now(timezone.utc)


def save_compare_report(task_id: str, report: TamperReport) -> None:
    """存标准条款比对报告 JSONB。"""
    _save_report(task_id, "report_compare", report.model_dump(mode="json"))


def save_raw_report(task_id: str, report: TextDiffReport) -> None:
    """存无标注版纯文本比对报告 JSONB。"""
    _save_report(task_id, "report_raw", report.model_dump(mode="json"))


def save_statement_report(task_id: str, report: StatementSummaryReport) -> None:
    """存对帐单金额统计报告 JSONB。"""
    _save_report(task_id, "report_statement", report.model_dump(mode="json"))


def _save_report(task_id: str, column: str, payload: dict[str, Any]) -> None:
    with session_scope() as s:
        rec = s.get(TaskRecord, task_id)
        if rec is None:
            logger.warning("_save_report: task_id=%s not found", task_id)
            return
        setattr(rec, column, payload)


def save_milestone_event(
    task_id: str,
    stage: str,
    progress: float,
    stage_timings: dict[str, float],
) -> None:
    """追加一条里程碑事件。调用方已在白名单中过滤,本函数不再判。"""
    with session_scope() as s:
        # 仅当任务记录存在才写事件(避免孤儿)
        exists = s.get(TaskRecord, task_id) is not None
        if not exists:
            logger.warning(
                "save_milestone_event: task_id=%s not found, skip event stage=%s",
                task_id, stage,
            )
            return
        s.add(TaskEvent(
            task_id=task_id,
            stage=stage,
            progress=float(progress),
            stage_timings=dict(stage_timings),
            milestone=True,
        ))


# —— 查询 API ——

def get_task(task_id: str) -> TaskRecord | None:
    """单条任务记录(含报告)。返回 ORM 对象;调用方需在 session 内或用 expire_on_commit=False。"""
    with session_scope() as s:
        rec = s.get(TaskRecord, task_id)
        if rec is None:
            return None
        # 触发 lazy 字段(events)在 session 关闭前加载
        _ = rec.events
        return rec


def list_tasks(
    *,
    kind: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[TaskRecord], int]:
    """分页查询任务列表,按 created_at 倒序。返回 (records, total)。"""
    base = select(TaskRecord)
    count_q = select(func.count()).select_from(TaskRecord)
    if kind:
        base = base.where(TaskRecord.kind == kind)
        count_q = count_q.where(TaskRecord.kind == kind)
    if status:
        base = base.where(TaskRecord.status == status)
        count_q = count_q.where(TaskRecord.status == status)

    base = base.order_by(TaskRecord.created_at.desc()).limit(limit).offset(offset)

    with session_scope() as s:
        total = s.scalar(count_q) or 0
        records = list(s.scalars(base).unique())
        # 触发 lazy load 在 session 关闭前
        for r in records:
            _ = r.target_names
        return records, total


def get_task_events(task_id: str) -> list[TaskEvent]:
    """返回该任务所有里程碑事件(按 id 升序)。"""
    with session_scope() as s:
        return list(s.scalars(
            select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.id)
        ))


def to_dict(rec: TaskRecord, *, include_report: bool = False) -> dict[str, Any]:
    """把 TaskRecord 序列化为 JSON 友好 dict(供 API 返回)。

    include_report=False:列表页用,排除三个大 JSONB 字段。
    include_report=True:详情页用,带报告字段(调用方按 kind 选择)。
    """
    data: dict[str, Any] = {
        "task_id": rec.task_id,
        "kind": rec.kind,
        "status": rec.status,
        "source_name": rec.source_name,
        "target_names": list(rec.target_names or []),
        "ocr_backend": rec.ocr_backend,
        "overall_risk": rec.overall_risk,
        "change_status": rec.change_status,
        "elapsed": rec.elapsed,
        "stage_timings": dict(rec.stage_timings or {}),
        "error": rec.error,
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
        "finished_at": rec.finished_at.isoformat() if rec.finished_at else None,
    }
    if include_report:
        data["report_compare"] = rec.report_compare
        data["report_raw"] = rec.report_raw
        data["report_statement"] = rec.report_statement
    return data


def event_to_dict(ev: TaskEvent) -> dict[str, Any]:
    return {
        "id": ev.id,
        "stage": ev.stage,
        "progress": ev.progress,
        "stage_timings": dict(ev.stage_timings or {}),
        "milestone": ev.milestone,
        "created_at": ev.created_at.isoformat() if ev.created_at else None,
    }


# —— LLM 配置 CRUD(单行 JSONB,id 固定为 1)——

# 单行固定主键:整份配置作为一个 JSONB blob,镜像原 llm_config.json 语义。
_LLM_CONFIG_ROW_ID = 1


def get_llm_config() -> dict[str, Any]:
    """读取持久化的 LLM 配置。无记录返回空 dict(调用方合并内置默认值)。"""
    with session_scope() as s:
        rec = s.get(LlmConfigRecord, _LLM_CONFIG_ROW_ID)
        if rec is None:
            return {}
        return dict(rec.config or {})


def save_llm_config(config: dict[str, Any]) -> None:
    """upsert 整份配置(覆盖 id=1 这一行的 config 字段)。

    调用方负责白名单过滤与空值剔除,这里只做 upsert 与时间戳更新。
    """
    payload = {k: v for k, v in (config or {}).items()}
    with session_scope() as s:
        rec = s.get(LlmConfigRecord, _LLM_CONFIG_ROW_ID)
        if rec is None:
            s.add(LlmConfigRecord(id=_LLM_CONFIG_ROW_ID, config=payload))
        else:
            rec.config = payload
