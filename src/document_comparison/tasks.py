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
import logging
import uuid
import time
from dataclasses import dataclass, field

from .config import settings
from .db import repository as db_repo
from .models import TaskInfo, TamperReport, TextDiffReport, StatementSummaryReport
from .pipeline import run_pipeline
from .raw_pipeline import run_raw_pipeline
from .statement_pipeline import run_statement_pipeline
from .observability import llm_call_collector
from .external_api import build_external_result, render_external_highlight_images
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
    "start",
    "done",
    "failed",
    "word_done",
    "ocr_done",
    "structure_done",
    "align_done",
    "compare_done",
    "report",
    "statement_aggregate",
})
_MILESTONE_STAGE_PREFIX = "statement_file_"  # 对帐单单文件里程碑(statement_file_N_done)


def _is_milestone(stage: str) -> bool:
    if stage in _MILESTONE_STAGES:
        return True
    # 对帐单单文件完成事件 stage 名形如 "statement_file_2_done"
    if stage.startswith(_MILESTONE_STAGE_PREFIX) and stage.endswith("_done"):
        return True
    return False


@dataclass
class Task:
    info: TaskInfo
    kind: str = ""  # compare | raw | statement(用于 PG 双写)
    report: TamperReport | None = None
    raw_report: TextDiffReport | None = None  # 无标注版报告(与 report 互斥)
    statement_report: StatementSummaryReport | None = None  # 对帐单金额统计报告(与上两者互斥)
    events: list[dict] = field(default_factory=list)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    callback_url: str | None = None
    callback_secret: str | None = None
    document_no: str | None = None
    external_request: bool = False
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

    def create(
        self,
        kind: str,
        *,
        source_name: str = "",
        target_names: list[str] | None = None,
        ocr_backend: str | None = None,
        callback_url: str | None = None,
        callback_secret: str | None = None,
        document_no: str | None = None,
        external_request: bool = False,
    ) -> str:
        """创建任务:内存登记 + PG 写入 pending 记录。

        kind 必填(compare/raw/statement),决定后续报告写入哪一列。
        """
        task_id = uuid.uuid4().hex[:16]
        self._tasks[task_id] = Task(
            info=TaskInfo(task_id=task_id, status="pending"),
            kind=kind,
            callback_url=callback_url,
            callback_secret=callback_secret,
            document_no=document_no,
            external_request=external_request,
        )
        logger.info(
            "task created task_id=%s kind=%s callback=%s",
            task_id, kind, "yes" if callback_url else "no",
        )
        # PG 写库失败不抛(任务本身仍可在内存运行,只是历史记录缺失)
        self._safe_db(
            db_repo.create_task,
            task_id, kind,
            source_name=source_name,
            target_names=target_names,
            ocr_backend=ocr_backend,
            document_no=document_no,
            external_request=external_request,
            callback_url=callback_url,
        )
        return task_id

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

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
        logger.info("task %s collected %d llm call record(s)", task_id, len(records))

    async def run(
        self, task_id: str, word_path: str, pdf_path: str,
        *, enable_llm_judge: bool = False, ocr_backend: str | None = None,
        enable_risk_assessment: bool = False,
    ) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        logger.info("task start task_id=%s word=%s pdf=%s llm_judge=%s ocr_backend=%s risk_assess=%s", task_id, word_path, pdf_path, enable_llm_judge, ocr_backend, enable_risk_assessment)
        await self._db_thread(
            db_repo.update_task_status, task_id, "running"
        )
        self._record_milestone(task, "start", 0.0)
        try:
            async with self._sem:
                task.info.status = "running"
                # pipeline 为同步阻塞(OCR/解析),放工作线程
                # 进度回调从工作线程实时推送(stage, fraction)
                # llm_call_collector 设 contextvar,to_thread 把它复制进工作线程;
                # OCR 内部 threading.Thread 经 _BoundedConcurrency.ctx.run 再传到孙线程,
                # 所有对话型 LLM 调用记录 append 到同一个 list,任务结束批量入库。
                with llm_call_collector() as llm_calls:
                    report = await asyncio.to_thread(
                        run_pipeline, word_path, pdf_path, settings,
                        on_progress=self._make_progress_cb(task),
                        enable_llm_judge=enable_llm_judge,
                        ocr_backend=ocr_backend,
                        enable_risk_assessment=enable_risk_assessment,
                    )
                await self._save_llm_calls(task_id, llm_calls)
            task.report = report
            await self._db_thread(
                db_repo.save_compare_report, task_id, report
            )
            # 外部任务把“全页高亮 PNG”作为成功结果的一部分。先持久化报告，
            # 再生成图片；渲染失败会进入 failed 回调，但报告仍保留便于排查。
            if task.external_request:
                await asyncio.to_thread(
                    render_external_highlight_images, task_id, pdf_path, report
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
                "task done task_id=%s risk=%s diffs=%s unmatched=%s",
                task_id, report.overall_risk,
                len(report.diffs), len(report.unmatched_clauses),
            )
            if task.external_request and task.document_no:
                external = build_external_result(task_id, task.document_no, report)
                await self._fire_callback(task_id, "done", {
                    "event_type": "contract.compare.completed",
                    "document_no": task.document_no,
                    "result": {
                        key: external[key]
                        for key in (
                            "change_status", "recognition_status", "location_status",
                            "summary", "result_text",
                        )
                    },
                    "highlight_images": external["highlight_images"],
                    "result_url": external["result_url"],
                })
            else:
                await self._fire_callback(task_id, "done", {
                    "overall_risk": report.overall_risk,
                    "summary": report.summary,
                    "report_url": f"/api/v1/compare/{task_id}/report?format=json",
                })
        except Exception as e:  # noqa: BLE001
            task.info.status = "failed"
            task.info.error = str(e)
            task.push_event("failed", task.info.progress)
            logger.exception("task failed task_id=%s", task_id)
            await self._db_thread(
                db_repo.update_task_status, task_id, "failed",
                error=str(e),
                stage_timings=dict(task.info.stage_timings),
            )
            self._record_milestone(task, "failed", task.info.progress)
            if task.external_request:
                await self._fire_callback(task_id, "failed", {
                    "event_type": "contract.compare.failed",
                    "document_no": task.document_no,
                    "error": str(e),
                })
            else:
                await self._fire_callback(task_id, "failed", {"error": str(e)})
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
                "task finished task_id=%s status=%s elapsed=%ss",
                task_id, task.info.status, task.info.elapsed,
            )

    async def run_raw(
        self, task_id: str, word_path: str, pdf_path: str,
        *, char_level: bool = True, ocr_backend: str | None = None,
    ) -> None:
        """无标注版任务:纯文本 difflib 流程,产出 TextDiffReport。"""
        task = self._tasks.get(task_id)
        if task is None:
            return
        logger.info("task start(raw) task_id=%s word=%s pdf=%s ocr_backend=%s", task_id, word_path, pdf_path, ocr_backend)
        await self._db_thread(
            db_repo.update_task_status, task_id, "running"
        )
        self._record_milestone(task, "start", 0.0)
        try:
            async with self._sem:
                task.info.status = "running"
                with llm_call_collector() as llm_calls:
                    report = await asyncio.to_thread(
                        run_raw_pipeline, word_path, pdf_path,
                        on_progress=self._make_progress_cb(task),
                        char_level=char_level,
                        ocr_backend=ocr_backend,
                    )
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
                "task done(raw) task_id=%s hunks=%s similarity=%s",
                task_id, len(report.hunks), report.stats.get("similarity"),
            )
            await self._fire_callback(task_id, "done", {
                "stats": report.stats,
                "report_url": f"/api/v1/raw-compare/{task_id}/report",
            })
        except Exception as e:  # noqa: BLE001
            task.info.status = "failed"
            task.info.error = str(e)
            task.push_event("failed", task.info.progress)
            logger.exception("task failed(raw) task_id=%s", task_id)
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
                "task finished(raw) task_id=%s status=%s elapsed=%ss",
                task_id, task.info.status, task.info.elapsed,
            )

    async def run_statement(
        self, task_id: str, pdf_paths: list[str], file_names: list[str],
        *, ocr_backend: str | None = None,
        amount_column_keywords: list[str] | None = None,
        enable_llm_column_detection: bool = True,
    ) -> None:
        """对帐单金额统计任务:多文件串行 OCR + 表格抽取 + 代码确定性求和。"""
        task = self._tasks.get(task_id)
        if task is None:
            return
        logger.info(
            "task start(statement) task_id=%s files=%s ocr_backend=%s",
            task_id, len(pdf_paths), ocr_backend,
        )
        await self._db_thread(
            db_repo.update_task_status, task_id, "running"
        )
        self._record_milestone(task, "start", 0.0)
        try:
            async with self._sem:
                task.info.status = "running"
                with llm_call_collector() as llm_calls:
                    report = await asyncio.to_thread(
                        run_statement_pipeline, pdf_paths, file_names,
                        on_progress=self._make_progress_cb(task),
                        ocr_backend=ocr_backend,
                        amount_column_keywords=amount_column_keywords,
                        enable_llm_column_detection=enable_llm_column_detection,
                    )
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
                "task done(statement) task_id=%s grand_total=%s verdict=%s",
                task_id, report.grand_total, report.verdict,
            )
            await self._fire_callback(task_id, "done", {
                "grand_total": report.grand_total,
                "verdict": report.verdict,
                "report_url": f"/api/v1/statement/{task_id}/report",
            })
        except Exception as e:  # noqa: BLE001
            task.info.status = "failed"
            task.info.error = str(e)
            task.push_event("failed", task.info.progress)
            logger.exception("task failed(statement) task_id=%s", task_id)
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
                "task finished(statement) task_id=%s status=%s elapsed=%ss",
                task_id, task.info.status, task.info.elapsed,
            )

    def _make_progress_cb(self, task: Task):
        """构造 pipeline 进度回调:更新内存状态 + 命中里程碑时写 PG。

        pipeline 的进度回调是同步函数(在线程池里调用),PG 写入同样同步,
        避免引入 event loop 跨线程调度复杂度。
        """
        def _cb(stage: str, frac: float) -> None:
            task.push_event(stage, frac)
            self._record_milestone(task, stage, frac)
        return _cb

    async def _fire_callback(self, task_id: str, status: str, payload: dict) -> None:
        task = self._tasks.get(task_id)
        if not task or not task.callback_url:
            return
        event, raw = webhook.build_event(task_id, status, payload)
        result = await webhook.deliver(
            task.callback_url,
            raw,
            task.callback_secret,
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

    async def event_stream(self, task_id: str):
        """SSE 事件生成器:增量推送事件,直到任务终态。"""
        task = self._tasks.get(task_id)
        if task is None:
            return
        last = 0
        while True:
            while last < len(task.events):
                yield task.events[last]
                last += 1
            if task.done.is_set() and last >= len(task.events):
                return
            await asyncio.sleep(0.3)


# 进程内单例。多进程部署需换共享存储。
task_manager = TaskManager()
