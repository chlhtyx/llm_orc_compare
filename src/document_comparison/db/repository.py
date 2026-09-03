"""数据访问层:任务记录与里程碑事件的 CRUD。

同步 API;上层(tasks.py / api/app.py)通过 asyncio.to_thread 调用,
与现有 storage.py 风格一致,不阻塞 asyncio 事件循环。

报告 JSONB 写入:接收 pydantic 模型,内部 model_dump() 后存 JSONB;
读取时调用方负责按 kind 还原成对应 pydantic 模型。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import String, case, cast, func, or_, select, text

from ..models import (
    StatementSummaryReport,
    TamperReport,
    TextDiffReport,
    TaskStatus,
)
from .engine import session_scope
from .models import ExternalApiCall, LlmConfigRecord, TaskEvent, TaskLlmCall, TaskRecord

logger = logging.getLogger(__name__)


# —— 任务记录 CRUD ——

def create_task(
    task_id: str,
    kind: str,
    *,
    source_name: str = "",
    target_names: Iterable[str] | None = None,
    ocr_backend: str | None = None,
    document_no: str | None = None,
    document_type: str | None = None,
    external_request: bool = False,
    callback_url: str | None = None,
) -> None:
    """任务提交时调用,创建一条 pending 记录。

    幂等性:若已存在同 task_id(同进程内极少发生),更新而非报错。
    callback_url 存在时,初始 callback_status 置 "pending"(交付后由
    update_callback_result 改写为 success/failed);无 callback 则保持 None。
    document_type:单据类型细分("1"=发票 | "2"=对帐单),仅
    statement 金额统计任务使用,其他任务为 None。
    """
    targets = list(target_names) if target_names else []
    # 有回调地址即视为待交付。
    cb_status = "pending" if callback_url else None
    with session_scope() as s:
        existing = s.get(TaskRecord, task_id)
        if existing is not None:
            existing.kind = kind
            existing.source_name = source_name
            existing.target_names = targets
            existing.ocr_backend = ocr_backend
            existing.document_no = document_no
            existing.document_type = document_type
            existing.external_request = external_request
            existing.callback_url = callback_url
            existing.callback_status = cb_status
            existing.status = "pending"
            existing.stop_requested = False
            return
        s.add(TaskRecord(
            task_id=task_id,
            kind=kind,
            status="pending",
            source_name=source_name,
            target_names=targets,
            ocr_backend=ocr_backend,
            document_no=document_no,
            document_type=document_type,
            external_request=external_request,
            callback_url=callback_url,
            callback_status=cb_status,
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
        if status in ("done", "failed"):
            rec.lease_owner = None
            rec.lease_expires_at = None
            rec.stop_requested = False
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


def update_callback_result(
    task_id: str,
    *,
    success: bool,
    http_status: int | None = None,
    error: str | None = None,
) -> None:
    """回写 callback 交付结果(任务终态触发回调后调用)。

    success=True → callback_status="success";否则 "failed"。
    http_status 为最终 HTTP 状态码(连接级失败时为 None);error 截断 512。
    记录不存在时只记日志(吞异常语义,不阻塞任务主流程)。
    """
    with session_scope() as s:
        rec = s.get(TaskRecord, task_id)
        if rec is None:
            logger.warning("update_callback_result: task_id=%s not found", task_id)
            return
        rec.callback_status = "success" if success else "failed"
        rec.callback_http_status = http_status
        rec.callback_error = error[:512] if error else None
        rec.callback_at = datetime.now(timezone.utc)


def save_callback_payload(task_id: str, payload: dict[str, Any]) -> None:
    """持久化首次回调交付的业务 payload,供「重新推送」端点还原原始内容。

    只存业务字段;envelope(event_id/task_id/status)由 webhook.build_event
    在交付时现拼,因此每次重推都得到新的 event_id。payload 中若含不可序列化
    对象,JSON 序列化会抛 ValueError,由调用方决定是否吞掉。
    """
    with session_scope() as s:
        rec = s.get(TaskRecord, task_id)
        if rec is None:
            logger.warning("save_callback_payload: task_id=%s not found", task_id)
            return
        rec.callback_payload = payload


def save_compare_report(task_id: str, report: TamperReport) -> None:
    """存标准条款比对报告 JSONB。"""
    _save_report(task_id, "report_compare", report.model_dump(mode="json"))


def save_raw_report(task_id: str, report: TextDiffReport) -> None:
    """存无标注版纯文本比对报告 JSONB。"""
    _save_report(task_id, "report_raw", report.model_dump(mode="json"))


def save_statement_report(task_id: str, report: StatementSummaryReport) -> None:
    """存金额统计报告 JSONB。"""
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
    document_type: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[TaskRecord], int]:
    """分页查询任务列表,按 created_at 倒序。返回 (records, total)。

    q 非空时,在 task_id / document_no / source_name / target_names(JSONB) 上
    做不区分大小写的模糊匹配;调用方应自行 strip 并判空,避免空串退化为全表扫。
    document_type 精确过滤("1"=发票 | "2"=对帐单),供按单据类型查询。
    """
    base = select(TaskRecord)
    count_q = select(func.count()).select_from(TaskRecord)
    if kind:
        base = base.where(TaskRecord.kind == kind)
        count_q = count_q.where(TaskRecord.kind == kind)
    if status:
        base = base.where(TaskRecord.status == status)
        count_q = count_q.where(TaskRecord.status == status)
    if document_type:
        base = base.where(TaskRecord.document_type == document_type)
        count_q = count_q.where(TaskRecord.document_type == document_type)
    if q:
        # target_names 是 JSONB list[str];cast 成 text 后整体 ILIKE,
        # 对 list[str] 命中足够准确,且无需展开数组的子查询开销。
        pat = f"%{q}%"
        cond = or_(
            TaskRecord.task_id.ilike(pat),
            TaskRecord.document_no.ilike(pat),
            TaskRecord.source_name.ilike(pat),
            cast(TaskRecord.target_names, String).ilike(pat),
        )
        base = base.where(cond)
        count_q = count_q.where(cond)

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


def get_task_events_after(task_id: str, last_id: int) -> list[TaskEvent]:
    """返回该任务 id > last_id 的所有事件(按 id 升序),供 SSE 增量轮询。"""
    with session_scope() as s:
        return list(s.scalars(
            select(TaskEvent)
            .where(TaskEvent.task_id == task_id, TaskEvent.id > last_id)
            .order_by(TaskEvent.id)
        ))


def get_task_status(task_id: str) -> str | None:
    """轻量查任务状态(不拉报告 JSONB),供 SSE 终态检测。

    返回 None 表示任务不存在。
    """
    with session_scope() as s:
        return s.scalar(
            select(TaskRecord.status).where(TaskRecord.task_id == task_id)
        )


def count_active_tasks() -> int:
    """统计 pending/running 状态的任务数。

    供 API 层 429 限流,实现跨 worker 的全局并发上限。
    注意:这是事务级一致性检查,两个并发请求可能同时读到同一计数后双双通过,
    导致瞬时略超 max_concurrent_tasks;此误差由执行端的进程内信号量
    (tasks.py::TaskManager._sem)与任务自然排队消化,可接受。
    """
    with session_scope() as s:
        return s.scalar(
            select(func.count()).select_from(TaskRecord)
            .where(TaskRecord.status.in_(("pending", "running")))
        ) or 0


def count_queued_tasks() -> int:
    """统计已经完整入队、尚未获得执行槽位的任务数。"""
    with session_scope() as s:
        return s.scalar(
            select(func.count()).select_from(TaskRecord).where(
                TaskRecord.status == "pending",
                TaskRecord.queue_payload.is_not(None),
            )
        ) or 0


def count_running_tasks() -> int:
    """统计已领取执行槽位的任务数。"""
    with session_scope() as s:
        return s.scalar(
            select(func.count()).select_from(TaskRecord).where(
                TaskRecord.status == "running"
            )
        ) or 0


def set_task_queue_payload(task_id: str, payload: dict[str, Any]) -> bool:
    """使一个已创建任务成为可领取的持久化队列任务。

    这里不做容量检查；提交端在文件落盘前检查队列容量，避免队列满时留下
    上传文件。调度领取使用另一把 PG 事务锁保证执行并发不会超限。
    """
    with session_scope() as s:
        rec = s.get(TaskRecord, task_id)
        if rec is None or rec.status != "pending":
            return False
        rec.queue_payload = dict(payload)
        return True


def recover_failed_task(task_id: str) -> str:
    """将保留队列参数的失败任务安全地重新入队。

    返回 ``recovered`` / ``not_found`` / ``invalid_status`` /
    ``not_recoverable``。与领取端共用 advisory lock，避免管理员恢复与
    dispatcher 领取任务发生状态竞争。
    """
    with session_scope() as s:
        s.execute(text("SELECT pg_advisory_xact_lock(92134017)"))
        rec = s.get(TaskRecord, task_id)
        if rec is None:
            return "not_found"
        if rec.status != "failed":
            return "invalid_status"
        if not rec.queue_payload:
            return "not_recoverable"
        rec.status = "pending"
        rec.error = None
        rec.finished_at = None
        rec.lease_owner = None
        rec.lease_expires_at = None
        rec.stop_requested = False
        return "recovered"


def request_task_stop(task_id: str) -> str:
    """停止尚未领取的任务，或登记运行中任务的协作式停止请求。

    与领取端共用 advisory lock，确保任务要么被停止，要么先被领取并由调用方
    进入“停止请求中”处理，不会出现两个终态写入者。返回 ``stopped`` /
    ``stop_requested`` / ``not_found`` / ``invalid_status``。
    """
    with session_scope() as s:
        s.execute(text("SELECT pg_advisory_xact_lock(92134017)"))
        rec = s.get(TaskRecord, task_id)
        if rec is None:
            return "not_found"
        if rec.status == "pending":
            rec.status = "failed"
            rec.error = "用户手动停止：任务尚未开始执行"
            rec.finished_at = datetime.now(timezone.utc)
            rec.lease_owner = None
            rec.lease_expires_at = None
            rec.stop_requested = False
            return "stopped"
        if rec.status == "running":
            # 新持久化队列任务在领取时一定同时写入 payload 和 lease_owner。
            # 两者缺失说明是旧版直跑任务或 worker 已消失的遗留记录，没有执行者
            # 可以消费协作式停止请求，必须直接转终态，不能继续显示“处理中”。
            if not rec.queue_payload or not rec.lease_owner:
                rec.status = "failed"
                rec.error = "用户手动停止：历史任务无有效队列租约"
                rec.finished_at = datetime.now(timezone.utc)
                rec.lease_owner = None
                rec.lease_expires_at = None
                rec.stop_requested = False
                return "stopped"
            rec.stop_requested = True
            rec.error = "用户请求停止：当前处理阶段结束后终止"
            return "stop_requested"
        return "invalid_status"


def task_stop_requested(task_id: str) -> bool:
    """读取运行中任务的跨 worker 停止请求标记。"""
    with session_scope() as s:
        return bool(s.scalar(
            select(TaskRecord.stop_requested).where(TaskRecord.task_id == task_id)
        ))


def claim_next_queued_task(
    worker_id: str,
    max_running: int,
    lease_seconds: float,
) -> TaskRecord | None:
    """原子领取最早的 pending 任务，跨 worker 严格限制 running 数。

    PostgreSQL advisory transaction lock 覆盖“统计 running + 领取”这个组合操作；
    单纯 count 后 update 在多 worker 下会产生竞态。过期租约的任务先退回
    pending，以便 worker 异常退出后重新执行。
    """
    with session_scope() as s:
        s.execute(text("SELECT pg_advisory_xact_lock(92134017)"))
        now = datetime.now(timezone.utc)
        s.query(TaskRecord).filter(
            TaskRecord.status == "running",
            TaskRecord.lease_expires_at.is_not(None),
            TaskRecord.lease_expires_at < now,
        ).update(
            {
                TaskRecord.status: "pending",
                TaskRecord.lease_owner: None,
                TaskRecord.lease_expires_at: None,
            },
            synchronize_session=False,
        )
        running = s.scalar(
            select(func.count()).select_from(TaskRecord).where(
                TaskRecord.status == "running"
            )
        ) or 0
        if running >= max_running:
            return None
        rec = s.scalar(
            select(TaskRecord)
            .where(
                TaskRecord.status == "pending",
                TaskRecord.queue_payload.is_not(None),
            )
            .order_by(TaskRecord.created_at.asc())
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if rec is None:
            return None
        rec.status = "running"
        rec.lease_owner = worker_id
        rec.lease_expires_at = now + timedelta(seconds=max(1.0, lease_seconds))
        return rec


def renew_task_lease(task_id: str, worker_id: str, lease_seconds: float) -> bool:
    """仅持有当前租约的 worker 可以续租，避免旧 worker 覆盖新领取者。"""
    with session_scope() as s:
        rec = s.get(TaskRecord, task_id)
        if rec is None or rec.status != "running" or rec.lease_owner != worker_id:
            return False
        rec.lease_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=max(1.0, lease_seconds)
        )
        return True


def to_dict(rec: TaskRecord, *, include_report: bool = False) -> dict[str, Any]:
    """把 TaskRecord 序列化为 JSON 友好 dict(供 API 返回)。

    include_report=False:列表页用,排除三个大 JSONB 字段。
    include_report=True:详情页用,带报告字段(调用方按 kind 选择)。
    """
    data: dict[str, Any] = {
        "task_id": rec.task_id,
        "kind": rec.kind,
        "status": rec.status,
        "stop_requested": rec.stop_requested,
        "source_name": rec.source_name,
        "target_names": list(rec.target_names or []),
        "ocr_backend": rec.ocr_backend,
        "document_no": rec.document_no,
        "document_type": rec.document_type,
        "external_request": rec.external_request,
        "overall_risk": rec.overall_risk,
        "change_status": rec.change_status,
        "elapsed": rec.elapsed,
        "stage_timings": dict(rec.stage_timings or {}),
        "error": rec.error,
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
        "finished_at": rec.finished_at.isoformat() if rec.finished_at else None,
        "callback_url": rec.callback_url,
        "callback_status": rec.callback_status,
        "callback_http_status": rec.callback_http_status,
        "callback_error": rec.callback_error,
        "callback_at": rec.callback_at.isoformat() if rec.callback_at else None,
        "callback_payload": rec.callback_payload,
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


def get_llm_config_updated_at():
    """读取 llm_config 行的 updated_at 时间戳(多 worker 配置同步用)。

    返回 None 表示无记录。供 config.ensure_llm_config_fresh() 比对版本,
    让每个 worker 在 GET/引擎构造前发现 PG 配置比自己内存新时重新 apply。
    """
    with session_scope() as s:
        rec = s.get(LlmConfigRecord, _LLM_CONFIG_ROW_ID)
        if rec is None:
            return None
        ts = rec.updated_at
        # 统一成 naive UTC datetime 便于跨 worker 比较(SQLAlchemy 可能给 aware/naive 混合)
        if ts is None:
            return None
        if isinstance(ts, datetime) and ts.tzinfo is not None:
            return ts.astimezone(timezone.utc).replace(tzinfo=None)
        return ts


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


# —— 模型调用与 OCR 解析结果记录 CRUD(embedding 不入)——


def save_llm_calls_batch(
    task_id: str, records: "list[Any]"
) -> int:
    """批量插入一个任务收集到的模型调用/OCR 解析结果记录。

    records 元素是 observability.LlmCallRecord dataclass。一次 session 批量 add,
    失败由 session_scope 记日志并回滚(调用方 _safe_db 会再吞一次)。
    返回写入条数(供 tasks.py 日志)。空列表直接返回 0。
    """
    if not records:
        return 0
    rows = [
        TaskLlmCall(
            task_id=task_id,
            kind=rec.kind,
            attempt=int(rec.attempt),
            status_code=rec.status_code,
            elapsed_ms=rec.elapsed_ms,
            error=rec.error,
            payload=rec.payload if rec.payload is not None else {},
            response=rec.response,
        )
        for rec in records
    ]
    with session_scope() as s:
        s.add_all(rows)
    return len(rows)


def get_task_llm_calls(task_id: str) -> list[TaskLlmCall]:
    """返回该任务所有 LLM 调用记录(按 id 升序,即调用发生顺序)。"""
    with session_scope() as s:
        return list(s.scalars(
            select(TaskLlmCall)
            .where(TaskLlmCall.task_id == task_id)
            .order_by(TaskLlmCall.id)
        ))


def llm_call_to_dict(rec: TaskLlmCall) -> dict[str, Any]:
    """把 TaskLlmCall 序列化为 JSON 友好 dict(供 API 返回)。

    payload/response 保持原结构(图片已脱敏、response 已截断)。
    """
    return {
        "id": rec.id,
        "task_id": rec.task_id,
        "kind": rec.kind,
        "attempt": rec.attempt,
        "status_code": rec.status_code,
        "elapsed_ms": rec.elapsed_ms,
        "error": rec.error,
        "payload": rec.payload,
        "response": rec.response,
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
    }


# —— 外部接口调用记录 CRUD(入站 HTTP 请求审计)——


def save_external_call(
    *,
    task_id: str | None,
    endpoint: str,
    method: str,
    document_no: str | None,
    client_ip: str | None,
    api_key_sha256: str | None,
    status_code: int,
    elapsed_ms: int | None,
    error: str | None,
    request_id: str,
    content_length: int | None,
    request_params: dict[str, Any] | None = None,
) -> None:
    """追加一条外部接口入站请求审计记录。

    由 api/app.py 审计中间件在请求结束时(异步、不阻塞响应)调用。写库失败
    只记日志(session_scope 已吞并回滚),遵循 task_records 双写规则——审计
    失败绝不影响请求主流程。
    """
    with session_scope() as s:
        s.add(ExternalApiCall(
            task_id=task_id,
            endpoint=endpoint,
            method=method,
            document_no=document_no,
            client_ip=client_ip,
            api_key_sha256=api_key_sha256,
            status_code=status_code,
            elapsed_ms=elapsed_ms,
            error=error[:512] if error else None,
            request_id=request_id,
            content_length=content_length,
            request_params=request_params,
        ))


def get_task_external_calls(task_id: str) -> list[ExternalApiCall]:
    """返回该任务所有外部接口调用记录(按 id 升序,即调用发生顺序)。"""
    with session_scope() as s:
        return list(s.scalars(
            select(ExternalApiCall)
            .where(ExternalApiCall.task_id == task_id)
            .order_by(ExternalApiCall.id)
        ))


def list_external_calls(
    *,
    endpoint: str | None = None,
    status_code: int | None = None,
    document_no: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ExternalApiCall], int]:
    """分页查询外部接口调用记录(全局审计视角),按 created_at 倒序。

    支持 endpoint/status_code/document_no 精确过滤 + q 在 task_id/document_no/
    client_ip/request_id 上的模糊匹配。覆盖无 task_id 的 401/422 失败记录。
    返回 (records, total)。
    """
    base = select(ExternalApiCall)
    count_q = select(func.count()).select_from(ExternalApiCall)
    if endpoint:
        base = base.where(ExternalApiCall.endpoint == endpoint)
        count_q = count_q.where(ExternalApiCall.endpoint == endpoint)
    if status_code is not None:
        base = base.where(ExternalApiCall.status_code == status_code)
        count_q = count_q.where(ExternalApiCall.status_code == status_code)
    if document_no:
        base = base.where(ExternalApiCall.document_no == document_no)
        count_q = count_q.where(ExternalApiCall.document_no == document_no)
    if q:
        pat = f"%{q}%"
        cond = or_(
            ExternalApiCall.task_id.ilike(pat),
            ExternalApiCall.document_no.ilike(pat),
            ExternalApiCall.client_ip.ilike(pat),
            ExternalApiCall.request_id.ilike(pat),
        )
        base = base.where(cond)
        count_q = count_q.where(cond)

    base = base.order_by(ExternalApiCall.created_at.desc()).limit(limit).offset(offset)
    with session_scope() as s:
        total = s.scalar(count_q) or 0
        records = list(s.scalars(base))
        return records, total


def external_call_to_dict(rec: ExternalApiCall) -> dict[str, Any]:
    """把 ExternalApiCall 序列化为 JSON 友好 dict(供 API 返回)。

    api_key_sha256 仅返回指纹(本就非明文),永不涉及明文。
    """
    return {
        "id": rec.id,
        "task_id": rec.task_id,
        "endpoint": rec.endpoint,
        "method": rec.method,
        "document_no": rec.document_no,
        "client_ip": rec.client_ip,
        "api_key_sha256": rec.api_key_sha256,
        "status_code": rec.status_code,
        "elapsed_ms": rec.elapsed_ms,
        "error": rec.error,
        "request_id": rec.request_id,
        "content_length": rec.content_length,
        "request_params": rec.request_params,
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
    }


# —— 每日调用统计(控制台看板)——

# 统计口径的「天」按北京时间切分,与外部交付报告文件名口径一致;
# 库里 created_at 均为 UTC timestamptz,SQL 内 AT TIME ZONE 转换后按天分组。
_STATS_TZ = "Asia/Shanghai"


def daily_stats(
    *,
    days: int = 14,
    start: date | None = None,
    end: date | None = None,
) -> dict[str, Any]:
    """按天聚合区间内任务数、模型调用数、外部接口调用数(看板用)。

    区间(北京时间日期):start/end 任一提供即用显式区间——缺 end 补今天,
    缺 start 由 days 回推;end 晚于今天截断到今天。start/end 均缺省时为
    [今天-days+1, 今天]。start 晚于(截断后)end 或跨度超过 366 天抛
    ValueError,由 API 层转 400。

    三条 GROUP BY 查询后在 Python 侧合并,缺失日期补零,series 按日期升序:
      tasks          = {total, by_status, by_kind}
      llm_calls      = {total, success, failed, by_kind}
      external_calls = {total, success, failed, by_endpoint}

    成功口径:模型调用 error 为空且 status_code 在 [200, 400);外部调用
    status_code < 400。注意 task_llm_calls.created_at 是任务结束时的批量
    落库时间,统计意义为「归属任务完成日」而非精确调用时刻。
    """
    tzinfo = ZoneInfo(_STATS_TZ)
    today = datetime.now(tzinfo).date()
    if start is None and end is None:
        start_day = today - timedelta(days=days - 1)
        end_day = today
    else:
        end_day = min(end, today) if end is not None else today
        if start is not None:
            start_day = start
        else:
            start_day = end_day - timedelta(days=days - 1)
        if start_day > end_day:
            raise ValueError("start 不能晚于 end(或今天)")
        if (end_day - start_day).days > 365:
            raise ValueError("统计区间跨度不能超过 366 天")
    # 北京时间 start_day 00:00 对应的 UTC 时刻,作为聚合下界
    start_dt = datetime.combine(start_day, time.min, tzinfo=tzinfo).astimezone(timezone.utc)

    def _day(col: Any) -> Any:
        # .label 后按输出列名分组(PG 特性):否则 SELECT 与 GROUP BY 里的表达式
        # 会渲染成不同绑定参数编号,PG 无法识别为同一表达式而报 GroupingError
        return func.to_char(col.op("AT TIME ZONE")(_STATS_TZ), "YYYY-MM-DD").label("day")

    task_day = _day(TaskRecord.created_at)
    llm_day = _day(TaskLlmCall.created_at)
    ext_day = _day(ExternalApiCall.created_at)

    llm_ok = case(
        (
            TaskLlmCall.error.is_(None)
            & TaskLlmCall.status_code.isnot(None)
            & (TaskLlmCall.status_code >= 200)
            & (TaskLlmCall.status_code < 400),
            1,
        ),
        else_=0,
    )
    ext_ok = case((ExternalApiCall.status_code < 400, 1), else_=0)

    with session_scope() as s:
        task_rows = s.execute(
            select(task_day, TaskRecord.kind, TaskRecord.status, func.count())
            .where(TaskRecord.created_at >= start_dt)
            .group_by(task_day, TaskRecord.kind, TaskRecord.status)
        ).all()
        llm_rows = s.execute(
            select(llm_day, TaskLlmCall.kind, llm_ok, func.count())
            .where(TaskLlmCall.created_at >= start_dt)
            .group_by(llm_day, TaskLlmCall.kind, llm_ok)
        ).all()
        ext_rows = s.execute(
            select(ext_day, ExternalApiCall.endpoint, ext_ok, func.count())
            .where(ExternalApiCall.created_at >= start_dt)
            .group_by(ext_day, ExternalApiCall.endpoint, ext_ok)
        ).all()

    series: dict[str, dict[str, Any]] = {}
    day = start_day
    while day <= end_day:
        key = day.isoformat()
        series[key] = {
            "date": key,
            "tasks": {"total": 0, "by_status": {}, "by_kind": {}},
            "llm_calls": {"total": 0, "success": 0, "failed": 0, "by_kind": {}},
            "external_calls": {"total": 0, "success": 0, "failed": 0, "by_endpoint": {}},
        }
        day += timedelta(days=1)

    for d, kind, status, cnt in task_rows:
        point = series.get(d)
        if point is None:
            continue
        point["tasks"]["total"] += cnt
        point["tasks"]["by_status"][status] = point["tasks"]["by_status"].get(status, 0) + cnt
        point["tasks"]["by_kind"][kind] = point["tasks"]["by_kind"].get(kind, 0) + cnt

    for d, kind, ok, cnt in llm_rows:
        point = series.get(d)
        if point is None:
            continue
        point["llm_calls"]["total"] += cnt
        point["llm_calls"]["success" if ok else "failed"] += cnt
        point["llm_calls"]["by_kind"][kind] = point["llm_calls"]["by_kind"].get(kind, 0) + cnt

    for d, endpoint, ok, cnt in ext_rows:
        point = series.get(d)
        if point is None:
            continue
        point["external_calls"]["total"] += cnt
        point["external_calls"]["success" if ok else "failed"] += cnt
        point["external_calls"]["by_endpoint"][endpoint] = (
            point["external_calls"]["by_endpoint"].get(endpoint, 0) + cnt
        )

    return {
        "days": (end_day - start_day).days + 1,
        "timezone": _STATS_TZ,
        "start": start_day.isoformat(),
        "end": end_day.isoformat(),
        "series": [series[k] for k in sorted(series)],
    }
