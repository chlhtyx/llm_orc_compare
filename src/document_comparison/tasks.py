"""异步任务管理(内存态 + Postgres 持久化,§7、§11.2)。

设计要点:
- 内存 `Task` 仍是 SSE 实时消费的源(全量进度事件),保证低延迟推送。
- Postgres 双写:任务记录(元数据 + 完整报告 JSONB) + 关键里程碑事件。
  - 写库失败只记日志,不抛、不阻塞主流程(DB 抖动不应拖垮业务)。
  - 写库走 `asyncio.to_thread`,与 pipeline 的同步执行隔离。
- 进程重启后内存丢失,查询端点(api/app.py)从 PG 兜底还原报告。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
import time
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path

from .config import settings
from .db import repository as db_repo
from .models import TaskInfo, TamperReport, TextDiffReport, StatementSummaryReport
from .pipeline import run_pipeline
from .raw_pipeline import run_raw_pipeline
from .statement_pipeline import run_statement_pipeline
from .observability import llm_call_collector, log_context, set_log_fields
from .external_api import (
    build_external_result,
    build_external_statement_result,
    render_external_highlight_images,
    write_external_html_report,
    write_external_pdf_report,
)
from .storage import compared_pdf_path, effective_target_path
from .storage import rendered_source_pdf_path
from .parsing import DocxRenderError, count_pages, render_docx_to_pdf
from . import webhook

logger = logging.getLogger(__name__)

_STAGE_TIMING_KEY = {
    "parse_word": "word",
    "word_parsing": "word",
    "word_done": "word",
    "ocr_pdf": "ocr",
    "ocr": "ocr",
    "ocr_done": "ocr",
    "structure": "structure",
    "structure_done": "structure",
    "align": "align",
    "align_done": "align",
    "compare": "compare",
    "compare_done": "compare",
    "report": "compare",
    "normalize": "normalize",
    "diff": "diff",
    "statement_start": "statement",
    "statement_aggregate": "statement",
}

# —— 里程碑事件白名单:仅这些 stage 入库 task_events ——
# 高频进度事件(中间 stage、子进度)不入库,避免写放大。
# 对帐单动态 stage(statement_file_N_done)用前缀匹配。
_MILESTONE_STAGES: frozenset[str] = frozenset({
    "queued",
    "start",
    "done",
    "failed",
    "word_done",
    "pdf_truncated",
    "ocr_done",
    "structure_done",
    "align_done",
    "compare_done",
    "report",
    "statement_aggregate",
})
_MILESTONE_STAGE_PREFIX = "statement_file_"  # 对帐单单文件里程碑(statement_file_N_done)


def _step_label(stage: str) -> str | None:
    """把流水线进度 stage 名映射为日志【业务-步骤】标注的步骤名。

    与 ``_STAGE_TIMING_KEY`` 的计时桶共用主映射(word/ocr/structure/align/
    compare/normalize/diff);对帐单动态 stage 细分为 file-N / start / aggregate。
    任务级事件(start/done/failed/report)返回 None——不切换步骤,
    日志保留上一阶段或仅显示业务名。
    """
    if stage.startswith(_MILESTONE_STAGE_PREFIX):
        core = stage[len(_MILESTONE_STAGE_PREFIX):]
        if core.endswith("_done"):
            core = core[: -len("_done")]
        return f"file-{core}" if core else None
    if stage == "statement_start":
        return "start"
    if stage == "statement_aggregate":
        return "aggregate"
    if stage in _STAGE_TIMING_KEY:
        return _STAGE_TIMING_KEY[stage]
    return None


def _is_milestone(stage: str) -> bool:
    if stage in _MILESTONE_STAGES:
        return True
    # 对帐单单文件完成事件 stage 名形如 "statement_file_2_done"
    if stage.startswith(_MILESTONE_STAGE_PREFIX) and stage.endswith("_done"):
        return True
    return False


def _with_task_log_context(fn):
    """让一个任务执行期间的所有日志自动带 task_id。"""
    @wraps(fn)
    async def wrapped(self, task_id: str, *args, **kwargs):
        task = self._tasks.get(task_id)
        with log_context(task_id=task_id, task_kind=task.kind if task else None):
            return await fn(self, task_id, *args, **kwargs)
    return wrapped


@dataclass
class Task:
    info: TaskInfo
    kind: str = ""  # compare | raw | statement(用于 PG 双写)
    report: TamperReport | None = None
    raw_report: TextDiffReport | None = None  # 无标注版报告(与 report 互斥)
    statement_report: StatementSummaryReport | None = None  # 金额统计报告(与上两者互斥)
    events: list[dict] = field(default_factory=list)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    callback_url: str | None = None
    document_no: str | None = None
    # 单据类型细分(statement 金额统计任务):"1"=发票 | "2"=对帐单
    document_type: str | None = None
    external_request: bool = False
    sync_mode: bool = False  # 外部接口同步模式:webhook 后台发送,不阻塞 HTTP 响应
    pending_callbacks: list = field(default_factory=list)  # sync 模式后台 webhook 任务
    start_time: float = field(default_factory=time.monotonic)
    active_timing_stage: str | None = None
    active_timing_started_at: float | None = None

    def finalize_elapsed(self) -> None:
        """终结时计算总耗时(秒),写入 info.elapsed。"""
        if self.start_time:
            self.info.elapsed = round(time.monotonic() - self.start_time, 2)

    def push_event(self, stage: str, progress: float) -> None:
        now = time.monotonic()
        timing_stage = _STAGE_TIMING_KEY.get(stage)
        # 对帐单多文件串行使用动态 stage 名(statement_file_N / statement_file_N_done),
        # 统一映射到 "statement" 计时桶。
        if timing_stage is None and stage.startswith("statement"):
            timing_stage = "statement"
        if timing_stage != self.active_timing_stage:
            self._finish_active_timing(now)
            if timing_stage is not None:
                self.active_timing_stage = timing_stage
                self.active_timing_started_at = now
        self.info.stage_timings = dict(self.info.stage_timings)
        self.events.append({
            "stage": stage,
            "progress": progress,
            "stage_timings": dict(self.info.stage_timings),
        })
        self.info.stage = stage
        self.info.progress = progress

    def _finish_active_timing(self, now: float) -> None:
        if self.active_timing_stage is None or self.active_timing_started_at is None:
            return
        elapsed = max(0.0, now - self.active_timing_started_at)
        previous = self.info.stage_timings.get(self.active_timing_stage, 0.0)
        self.info.stage_timings[self.active_timing_stage] = round(previous + elapsed, 3)
        self.active_timing_stage = None
        self.active_timing_started_at = None


class TaskManager:
    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._sem = asyncio.Semaphore(settings.max_concurrent_tasks)
        self._queue_wakeup: asyncio.Event | None = None
        self._queue_dispatcher: asyncio.Task | None = None
        self._queue_stop = False
        self._queue_running: set[asyncio.Task] = set()
        self._queue_worker_id = f"{uuid.uuid4().hex[:12]}"

    def create(
        self,
        kind: str,
        *,
        source_name: str = "",
        target_names: list[str] | None = None,
        ocr_backend: str | None = None,
        callback_url: str | None = None,
        document_no: str | None = None,
        document_type: str | None = None,
        external_request: bool = False,
        sync_mode: bool = False,
    ) -> str:
        """创建任务:内存登记 + PG 写入 pending 记录。

        kind 必填(compare/raw/statement),决定后续报告写入哪一列。
        document_type:单据类型细分("1"=发票 | "2"=对帐单),
        仅 statement 任务使用,用于记录标识与后续查询。
        sync_mode 标记外部接口同步提交:webhook 改后台发送,不阻塞 HTTP 响应。
        """
        task_id = uuid.uuid4().hex[:16]
        self._tasks[task_id] = Task(
            info=TaskInfo(task_id=task_id, status="pending"),
            kind=kind,
            callback_url=callback_url,
            document_no=document_no,
            document_type=document_type,
            external_request=external_request,
            sync_mode=sync_mode,
        )
        with log_context(task_id=task_id, task_kind=kind):
            logger.info(
                "task created kind=%s callback=%s",
                kind, "yes" if callback_url else "no",
            )
        # PG 写库失败不抛(任务本身仍可在内存运行,只是历史记录缺失)
        self._safe_db(
            db_repo.create_task,
            task_id, kind,
            source_name=source_name,
            target_names=target_names,
            ocr_backend=ocr_backend,
            document_no=document_no,
            document_type=document_type,
            external_request=external_request,
            callback_url=callback_url,
        )
        return task_id

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    async def enqueue(self, task_id: str, payload: dict) -> bool:
        """将文件已落盘的任务转为可由 PG 队列领取的作业。"""
        queued = await asyncio.to_thread(db_repo.set_task_queue_payload, task_id, payload)
        if queued:
            task = self._tasks.get(task_id)
            if task is not None:
                task.info.status = "pending"
                task.info.stage = "queued"
                task.push_event("queued", 0.0)
                self._record_milestone(task, "queued", 0.0)
            self.wake_queue()
        return queued

    async def queue_is_full(self) -> bool:
        """用于上传文件前的有界队列预检；领取端仍负责执行并发原子约束。"""
        if settings.max_queued_tasks > 0:
            return (
                await asyncio.to_thread(db_repo.count_queued_tasks)
            ) >= settings.max_queued_tasks
        # 0 表示不保留等待名额：仅在所有执行槽位均被占用时拒绝。
        return (
            await asyncio.to_thread(db_repo.count_running_tasks)
        ) >= settings.max_concurrent_tasks

    def wake_queue(self) -> None:
        if self._queue_wakeup is not None:
            self._queue_wakeup.set()

    async def start_queue_dispatcher(self) -> None:
        """在每个 FastAPI worker 生命周期内启动 PG 队列调度器。"""
        if self._queue_dispatcher is not None and not self._queue_dispatcher.done():
            return
        self._queue_stop = False
        self._queue_wakeup = asyncio.Event()
        self._queue_dispatcher = asyncio.create_task(
            self._queue_dispatch_loop(), name="document-comparison-queue"
        )
        self.wake_queue()

    async def stop_queue_dispatcher(self) -> None:
        self._queue_stop = True
        self.wake_queue()
        if self._queue_dispatcher is not None:
            self._queue_dispatcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._queue_dispatcher
        self._queue_dispatcher = None

    async def wait_for_sync_queue(self, task_id: str) -> bool:
        """等到任务领取或终态；False 表示仍 pending 且同步排队预算耗尽。"""
        deadline = time.monotonic() + max(0.0, settings.sync_queue_wait_seconds)
        while True:
            status = await asyncio.to_thread(db_repo.get_task_status, task_id)
            if status is None or status in ("done", "failed", "running"):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.5)

    async def wait_for_terminal(self, task_id: str) -> None:
        """跨 worker 等待终态；不能依赖仅本进程可见的 Task.done。"""
        while True:
            status = await asyncio.to_thread(db_repo.get_task_status, task_id)
            if status is None or status in ("done", "failed"):
                return
            await asyncio.sleep(0.5)

    def _hydrate_claimed_task(self, rec) -> Task:
        task = self._tasks.get(rec.task_id)
        if task is None:
            task = Task(
                info=TaskInfo(task_id=rec.task_id, status="running"),
                kind=rec.kind,
                callback_url=rec.callback_url,
                document_no=rec.document_no,
                document_type=rec.document_type,
                external_request=rec.external_request,
                sync_mode=bool((rec.queue_payload or {}).get("sync_mode", False)),
            )
            self._tasks[rec.task_id] = task
        task.info.status = "running"
        return task

    async def _renew_lease_until_done(self, task_id: str) -> None:
        interval = max(1.0, settings.task_queue_lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            renewed = await asyncio.to_thread(
                db_repo.renew_task_lease,
                task_id,
                self._queue_worker_id,
                settings.task_queue_lease_seconds,
            )
            if not renewed:
                return

    async def _run_claimed_queue_task(self, rec) -> None:
        self._hydrate_claimed_task(rec)
        payload = dict(rec.queue_payload or {})
        renewer = asyncio.create_task(self._renew_lease_until_done(rec.task_id))
        try:
            runner = payload.get("runner")
            if runner == "compare":
                await self.run(
                    rec.task_id, payload["word_path"], payload["pdf_path"],
                    enable_llm_judge=bool(payload.get("enable_llm_judge", False)),
                    enable_llm_alignment=bool(payload.get("enable_llm_alignment", False)),
                    ocr_backend=payload.get("ocr_backend"),
                    enable_risk_assessment=bool(payload.get("enable_risk_assessment", False)),
                    enable_llm_direct_diff=bool(payload.get("enable_llm_direct_diff", False)),
                    truncate_to_original_pages=bool(payload.get("truncate_to_original_pages", False)),
                    original_page_count=payload.get("original_page_count"),
                )
            elif runner == "statement":
                await self.run_statement(
                    rec.task_id, list(payload["pdf_paths"]), list(payload["file_names"]),
                    ocr_backend=payload.get("ocr_backend"),
                    amount_column_keywords=payload.get("amount_column_keywords"),
                    enable_llm_column_detection=bool(payload.get("enable_llm_column_detection", True)),
                )
            elif runner == "raw":
                await self.run_raw(
                    rec.task_id, payload["word_path"], payload["pdf_path"],
                    char_level=bool(payload.get("char_level", True)),
                    ocr_backend=payload.get("ocr_backend"),
                )
            else:
                raise ValueError(f"未知队列 runner: {runner!r}")
        except Exception:  # run* normally swallows pipeline errors; keep dispatcher alive on bad payload.
            logger.exception("queued task dispatch failed task=%s", rec.task_id)
            await self._db_thread(
                db_repo.update_task_status, rec.task_id, "failed",
                error="队列任务参数无效或调度失败",
                finished=True,
            )
        finally:
            renewer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renewer

    async def _queue_dispatch_loop(self) -> None:
        assert self._queue_wakeup is not None
        while not self._queue_stop:
            claimed_any = False
            while len(self._queue_running) < settings.max_concurrent_tasks:
                try:
                    rec = await asyncio.to_thread(
                        db_repo.claim_next_queued_task,
                        self._queue_worker_id,
                        settings.max_concurrent_tasks,
                        settings.task_queue_lease_seconds,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("queue claim failed")
                    break
                if rec is None:
                    break
                claimed_any = True
                future = asyncio.create_task(self._run_claimed_queue_task(rec))
                self._queue_running.add(future)
                future.add_done_callback(self._queue_running.discard)
            if claimed_any:
                continue
            try:
                await asyncio.wait_for(self._queue_wakeup.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            self._queue_wakeup.clear()

    @staticmethod
    def _safe_db(fn, *args, **kwargs) -> None:
        """把同步 DB 调用包成「吞异常」的同步函数。

        在 TaskManager 里通过 asyncio.to_thread 调用(避免阻塞事件循环),
        任何异常只记日志,不上抛,保证 DB 抖动不拖垮任务主流程。
        """
        try:
            fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("db write failed in %s: %s", getattr(fn, "__name__", fn), exc)

    async def _db_thread(self, fn, *args, **kwargs) -> None:
        """asyncio 友好版的 _safe_db:在线程池里执行,不阻塞。"""
        await asyncio.to_thread(self._safe_db, fn, *args, **kwargs)

    @contextlib.asynccontextmanager
    async def _acquire_sem(self):
        """获取执行槽位,超时抛错,防止同步模式请求在槽位满时永久 hang。

        `asyncio.Semaphore.acquire` 本身无超时;`asyncio.wait_for` 包一层,
        超时抛 TimeoutError,被各 run* 的 except 捕获 → 置 failed + 回调。
        注意:`wait_for` 超时会取消底层 acquire 协程,不会泄漏等待者。
        """
        try:
            await asyncio.wait_for(
                self._sem.acquire(), timeout=settings.task_acquire_timeout
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"等待执行槽位超时({settings.task_acquire_timeout}s)"
            ) from exc
        try:
            yield
        finally:
            self._sem.release()

    def _record_milestone(self, task: Task, stage: str, progress: float | None = None) -> None:
        """里程碑事件入库(同步包装,只记异常不抛)。"""
        if not task.kind:
            return
        if not _is_milestone(stage):
            return
        prog = task.info.progress if progress is None else progress
        self._safe_db(
            db_repo.save_milestone_event,
            task.info.task_id, stage, prog, dict(task.info.stage_timings),
        )

    async def _save_llm_calls(self, task_id: str, records: list) -> None:
        """把收集器收集到的对话型 LLM 调用记录批量入库(吞异常)。"""
        if not records:
            return
        await self._db_thread(db_repo.save_llm_calls_batch, task_id, list(records))
        failures = sum(
            record.error is not None
            or record.status_code is None
            or record.status_code >= 400
            for record in records
        )
        kinds = sorted({record.kind for record in records})
        logger.info(
            "task model calls persisted count=%s failures=%s kinds=%s",
            len(records), failures, kinds,
        )

    @_with_task_log_context
    async def run(
        self, task_id: str, word_path: str, pdf_path: str,
        *, enable_llm_judge: bool = False, ocr_backend: str | None = None,
        enable_llm_alignment: bool = False,
        enable_risk_assessment: bool = False,
        enable_llm_direct_diff: bool = False,
        truncate_to_original_pages: bool = False,
        original_page_count: int | None = None,
    ) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        logger.info("task start input=word+pdf llm_direct=%s llm_alignment=%s llm_judge=%s ocr_backend=%s risk_assess=%s truncate=%s orig_pages=%s", enable_llm_direct_diff, enable_llm_alignment, enable_llm_judge, ocr_backend, enable_risk_assessment, truncate_to_original_pages, original_page_count)
        await self._db_thread(
            db_repo.update_task_status, task_id, "running"
        )
        self._record_milestone(task, "start", 0.0)
        try:
            async with self._acquire_sem():
                task.info.status = "running"
                # pipeline 为同步阻塞(OCR/解析),放工作线程
                # 进度回调从工作线程实时推送(stage, fraction)
                # llm_call_collector 设 contextvar,to_thread 把它复制进工作线程;
                # OCR 内部 threading.Thread 经 _BoundedConcurrency.ctx.run 再传到孙线程,
                # 所有对话型 LLM 调用记录 append 到同一个 list,任务结束批量入库。
                with llm_call_collector() as llm_calls:
                    try:
                        source_annotation_pdf_path: Path | None = None
                        if Path(word_path).suffix.lower() == ".docx":
                            rendered_source = rendered_source_pdf_path(task_id)
                            try:
                                source_annotation_pdf_path = await asyncio.to_thread(
                                    render_docx_to_pdf,
                                    word_path,
                                    rendered_source,
                                    executable=settings.docx_renderer_path,
                                    timeout_seconds=settings.docx_render_timeout_seconds,
                                )
                            except DocxRenderError as exc:
                                # DOCX 可视化标注是增强能力；渲染失败不应阻止原始的
                                # 结构化文本比对，报告会明确标记原件侧不可用。
                                logger.warning("source DOCX rendering unavailable: %s", exc)
                            if (
                                truncate_to_original_pages
                                and original_page_count is None
                                and source_annotation_pdf_path is not None
                            ):
                                # 渲染出的原件 PDF 页数是真实排过版的页数,远比
                                # OOXML 估算可靠;估算在无分页证据时只能返回 0
                                # (页数未知→管线跳过截取),按臆测页数截取会截掉
                                # 回收件真实内容,使差异整体失真。
                                original_page_count = await asyncio.to_thread(
                                    count_pages, source_annotation_pdf_path
                                )
                                logger.info(
                                    "truncation base from rendered source pdf pages=%s",
                                    original_page_count,
                                )
                        report = await asyncio.to_thread(
                            run_pipeline, word_path, pdf_path, settings,
                            on_progress=self._make_progress_cb(task),
                            enable_llm_judge=enable_llm_judge,
                            enable_llm_alignment=enable_llm_alignment,
                            ocr_backend=ocr_backend,
                            enable_risk_assessment=enable_risk_assessment,
                            enable_llm_direct_diff=enable_llm_direct_diff,
                            truncate_to_original_pages=truncate_to_original_pages,
                            original_page_count=original_page_count,
                            truncated_pdf_output_path=compared_pdf_path(task_id),
                            source_annotation_pdf_path=source_annotation_pdf_path,
                        )
                    finally:
                        # 模型超时/解析异常会让 pipeline 抛错;仍要落库已收集的
                        # request/failure attempt,否则历史页看不到模型/OCR记录。
                        await self._save_llm_calls(task_id, llm_calls)
            task.report = report
            await self._db_thread(
                db_repo.save_compare_report, task_id, report
            )
            # 外部任务把“比对范围内的全页高亮 PNG”作为成功结果的一部分。先持久化报告，
            # 再生成图片；渲染失败会进入 failed 回调，但报告仍保留便于排查。
            if task.external_request:
                # 正常任务必有上传件；这里保留调用方传入路径作兼容兜底，
                # 使不落盘的测试/自定义执行器仍可生成外部产物。
                effective_pdf_path = effective_target_path(task_id) or Path(pdf_path)
                source_pdf_path = (
                    Path(word_path)
                    if Path(word_path).suffix.lower() == ".pdf"
                    else rendered_source_pdf_path(task_id)
                )
                render_args = (task_id, effective_pdf_path, report)
                if source_pdf_path.is_file():
                    render_args = (*render_args, source_pdf_path)
                await asyncio.to_thread(render_external_highlight_images, *render_args)
                # 外部产物同步生成自包含 HTML 报告(供 result_url 下载);
                # 渲染失败只记日志,不阻断任务主流程(done 状态与回调照常)。
                try:
                    await asyncio.to_thread(
                        write_external_html_report,
                        task_id,
                        task.document_no or "",
                        report,
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "external html report generation failed task=%s", task_id, exc_info=True
                    )
                # 同一份内容的 PDF 版本(结果字段 result_url 的下载目标),失败同样不阻断。
                try:
                    await asyncio.to_thread(
                        write_external_pdf_report,
                        task_id,
                        task.document_no or "",
                        report,
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "external pdf report generation failed task=%s", task_id, exc_info=True
                    )
            task.info.overall_risk = report.overall_risk
            task.info.status = "done"
            task.push_event("done", 1.0)
            await self._db_thread(
                db_repo.update_task_status, task_id, "done",
                overall_risk=report.overall_risk,
                change_status=report.change_status,
                stage_timings=dict(task.info.stage_timings),
            )
            self._record_milestone(task, "done", 1.0)
            logger.info(
                "task done risk=%s diffs=%s unmatched=%s stage_timings=%s",
                report.overall_risk, len(report.diffs), len(report.unmatched_clauses),
                task.info.stage_timings,
            )
            if task.external_request and task.document_no:
                external = build_external_result(task_id, task.document_no, report)
                payload = {
                    "event_type": "contract.compare.completed",
                    "document_no": task.document_no,
                }
                payload.update(external)
                await self._fire_or_schedule_callback(task_id, "done", payload)
            else:
                await self._fire_or_schedule_callback(task_id, "done", {
                    "overall_risk": report.overall_risk,
                    "summary": report.summary,
                    "report_url": f"/api/v1/compare/{task_id}/report?format=json",
                })
        except Exception as e:  # noqa: BLE001
            task.info.status = "failed"
            task.info.error = str(e)
            task.push_event("failed", task.info.progress)
            logger.exception("task failed")
            await self._db_thread(
                db_repo.update_task_status, task_id, "failed",
                error=str(e),
                stage_timings=dict(task.info.stage_timings),
            )
            self._record_milestone(task, "failed", task.info.progress)
            if task.external_request:
                await self._fire_or_schedule_callback(task_id, "failed", {
                    "event_type": "contract.compare.failed",
                    "document_no": task.document_no,
                    "error": str(e),
                })
            else:
                await self._fire_or_schedule_callback(task_id, "failed", {"error": str(e)})
        finally:
            task.done.set()
            task.finalize_elapsed()
            # finally 里再补一次 elapsed + finished_at(任何路径都写)
            await self._db_thread(
                db_repo.update_task_status, task_id, task.info.status,
                elapsed=task.info.elapsed,
                finished=True,
            )
            logger.info(
                "task finished status=%s elapsed=%ss stage_timings=%s",
                task.info.status, task.info.elapsed, task.info.stage_timings,
            )

    @_with_task_log_context
    async def run_raw(
        self, task_id: str, word_path: str, pdf_path: str,
        *, char_level: bool = True, ocr_backend: str | None = None,
    ) -> None:
        """无标注版任务:纯文本 difflib 流程,产出 TextDiffReport。"""
        task = self._tasks.get(task_id)
        if task is None:
            return
        logger.info("task start(raw) input=word+pdf ocr_backend=%s", ocr_backend)
        await self._db_thread(
            db_repo.update_task_status, task_id, "running"
        )
        self._record_milestone(task, "start", 0.0)
        try:
            async with self._acquire_sem():
                task.info.status = "running"
                with llm_call_collector() as llm_calls:
                    try:
                        report = await asyncio.to_thread(
                            run_raw_pipeline, word_path, pdf_path,
                            on_progress=self._make_progress_cb(task),
                            char_level=char_level,
                            ocr_backend=ocr_backend,
                        )
                    finally:
                        await self._save_llm_calls(task_id, llm_calls)
            task.raw_report = report
            task.info.status = "done"
            task.push_event("done", 1.0)
            await self._db_thread(
                db_repo.save_raw_report, task_id, report
            )
            await self._db_thread(
                db_repo.update_task_status, task_id, "done",
                stage_timings=dict(task.info.stage_timings),
            )
            self._record_milestone(task, "done", 1.0)
            logger.info(
                "task done(raw) hunks=%s similarity=%s stage_timings=%s",
                len(report.hunks), report.stats.get("similarity"), task.info.stage_timings,
            )
            await self._fire_callback(task_id, "done", {
                "stats": report.stats,
                "report_url": f"/api/v1/raw-compare/{task_id}/report",
            })
        except Exception as e:  # noqa: BLE001
            task.info.status = "failed"
            task.info.error = str(e)
            task.push_event("failed", task.info.progress)
            logger.exception("task failed(raw)")
            await self._db_thread(
                db_repo.update_task_status, task_id, "failed",
                error=str(e),
                stage_timings=dict(task.info.stage_timings),
            )
            self._record_milestone(task, "failed", task.info.progress)
            await self._fire_callback(task_id, "failed", {"error": str(e)})
        finally:
            task.done.set()
            task.finalize_elapsed()
            await self._db_thread(
                db_repo.update_task_status, task_id, task.info.status,
                elapsed=task.info.elapsed,
                finished=True,
            )
            logger.info(
                "task finished(raw) status=%s elapsed=%ss stage_timings=%s",
                task.info.status, task.info.elapsed, task.info.stage_timings,
            )

    @_with_task_log_context
    async def run_statement(
        self, task_id: str, pdf_paths: list[str], file_names: list[str],
        *, ocr_backend: str | None = None,
        amount_column_keywords: list[str] | None = None,
        enable_llm_column_detection: bool = True,
    ) -> None:
        """金额统计任务:多文件串行 OCR + 表格抽取 + 代码确定性求和。"""
        task = self._tasks.get(task_id)
        if task is None:
            return
        logger.info(
            "task start(statement) files=%s ocr_backend=%s",
            len(pdf_paths), ocr_backend,
        )
        await self._db_thread(
            db_repo.update_task_status, task_id, "running"
        )
        self._record_milestone(task, "start", 0.0)
        try:
            async with self._acquire_sem():
                task.info.status = "running"
                with llm_call_collector() as llm_calls:
                    try:
                        report = await asyncio.to_thread(
                            run_statement_pipeline, pdf_paths, file_names,
                            on_progress=self._make_progress_cb(task),
                            ocr_backend=ocr_backend,
                            amount_column_keywords=amount_column_keywords,
                            enable_llm_column_detection=enable_llm_column_detection,
                        )
                    finally:
                        await self._save_llm_calls(task_id, llm_calls)
            task.statement_report = report
            task.info.status = "done"
            task.push_event("done", 1.0)
            await self._db_thread(
                db_repo.save_statement_report, task_id, report
            )
            await self._db_thread(
                db_repo.update_task_status, task_id, "done",
                change_status=report.verdict,
                stage_timings=dict(task.info.stage_timings),
            )
            self._record_milestone(task, "done", 1.0)
            logger.info(
                "task done(statement) grand_total=%s verdict=%s stage_timings=%s",
                report.grand_total, report.verdict, task.info.stage_timings,
            )
            if task.external_request and task.document_no:
                external = build_external_statement_result(
                    task_id, task.document_no, report,
                    document_type=task.document_type,
                )
                payload = {
                    "event_type": "statement.summary.completed",
                    "document_no": task.document_no,
                    "document_type": task.document_type,
                }
                payload.update(external)
                await self._fire_or_schedule_callback(task_id, "done", payload)
            else:
                await self._fire_callback(task_id, "done", {
                    "grand_total": report.grand_total,
                    "verdict": report.verdict,
                    "report_url": f"/api/v1/statement/{task_id}/report",
                })
        except Exception as e:  # noqa: BLE001
            task.info.status = "failed"
            task.info.error = str(e)
            task.push_event("failed", task.info.progress)
            logger.exception("task failed(statement)")
            await self._db_thread(
                db_repo.update_task_status, task_id, "failed",
                error=str(e),
                stage_timings=dict(task.info.stage_timings),
            )
            self._record_milestone(task, "failed", task.info.progress)
            if task.external_request:
                await self._fire_or_schedule_callback(task_id, "failed", {
                    "event_type": "statement.summary.failed",
                    "document_no": task.document_no,
                    "document_type": task.document_type,
                    "error": str(e),
                })
            else:
                await self._fire_callback(task_id, "failed", {"error": str(e)})
        finally:
            task.done.set()
            task.finalize_elapsed()
            await self._db_thread(
                db_repo.update_task_status, task_id, task.info.status,
                elapsed=task.info.elapsed,
                finished=True,
            )
            logger.info(
                "task finished(statement) status=%s elapsed=%ss stage_timings=%s",
                task.info.status, task.info.elapsed, task.info.stage_timings,
            )

    def _make_progress_cb(self, task: Task):
        """构造 pipeline 进度回调:更新内存状态 + 命中里程碑时写 PG。

        pipeline 的进度回调是同步函数(在线程池里调用),PG 写入同样同步,
        避免引入 event loop 跨线程调度复杂度。

        阶段变化时同步切换日志上下文的 ``step``:pipeline 在单个工作线程内
        顺序执行,set 只影响该线程(及其后创建的 OCR 孙线程),后续阶段日志
        带上【业务-步骤】标注;主协程的任务级日志不受影响。
        """
        last_stage: list[str | None] = [None]

        def _cb(stage: str, frac: float) -> None:
            if stage != last_stage[0]:
                last_stage[0] = stage
                set_log_fields(step=_step_label(stage))
            task.push_event(stage, frac)
            self._record_milestone(task, stage, frac)
        return _cb

    async def _fire_callback(self, task_id: str, status: str, payload: dict) -> None:
        task = self._tasks.get(task_id)
        if not task or not task.callback_url:
            return
        # 先持久化业务 payload,供「重新推送」端点 100% 还原原始内容。
        # envelope(event_id/task_id/status)由 build_event 现拼,不存。
        with contextlib.suppress(Exception):
            await self._db_thread(db_repo.save_callback_payload, task_id, payload)
        event, raw = webhook.build_event(task_id, status, payload)
        result = await webhook.deliver(
            task.callback_url,
            raw,
            event["event_id"],
            max_retries=settings.webhook_max_retries,
            timeout=settings.webhook_timeout_seconds,
        )
        # 回写 callback 交付结果(吞异常,不阻塞任务主流程)。
        await self._db_thread(
            db_repo.update_callback_result, task_id,
            success=result["success"],
            http_status=result["http_status"],
            error=result["error"],
        )

    async def _fire_or_schedule_callback(
        self, task_id: str, status: str, payload: dict
    ) -> None:
        """对外部接口任务:sync 模式后台发送 webhook,避免阻塞 HTTP 响应。

        - sync_mode=True:把 _fire_callback 挂到事件循环后台执行,立即返回;
          webhook 投递(最多 retry × timeout 秒)不再拖住 sync 响应。
          后台任务引用保留在 task.pending_callbacks,便于测试观测。
        - 其余(异步模式 / 内部接口):沿用同步 await,行为不变。
        """
        task = self._tasks.get(task_id)
        if task is None or not task.callback_url:
            return
        if task.sync_mode:
            coro = self._fire_callback(task_id, status, payload)
            task.pending_callbacks.append(asyncio.create_task(coro))
            return
        await self._fire_callback(task_id, status, payload)

    async def event_stream(self, task_id: str):
        """SSE 事件生成器:从 PG task_events 增量推送里程碑,直到任务终态。

        多 worker 安全:任意 worker 都能服务 SSE,因为只依赖 PG。
        进度粒度从"逐页 OCR"降级为"里程碑"(约 6-10 个节点/任务),
        与 AGENTS.md「高频进度事件只留内存,仅里程碑入库」一致。
        """
        last_id = 0
        while True:
            events = await asyncio.to_thread(
                db_repo.get_task_events_after, task_id, last_id
            )
            for ev in events:
                yield {
                    "stage": ev.stage,
                    "progress": ev.progress,
                    "stage_timings": dict(ev.stage_timings or {}),
                }
                last_id = ev.id
            status = await asyncio.to_thread(db_repo.get_task_status, task_id)
            if status is None:
                # 任务不存在:无任何事件可推,直接结束。
                return
            if status in ("done", "failed"):
                # 终态:再拉一次确保补齐终态事件后退出(避免漏推 done/failed)。
                tail = await asyncio.to_thread(
                    db_repo.get_task_events_after, task_id, last_id
                )
                for ev in tail:
                    yield {
                        "stage": ev.stage,
                        "progress": ev.progress,
                        "stage_timings": dict(ev.stage_timings or {}),
                    }
                return
            await asyncio.sleep(1.0)  # 里程碑低频,1s 轮询足够


# 进程内单例。多进程部署需换共享存储。
task_manager = TaskManager()
