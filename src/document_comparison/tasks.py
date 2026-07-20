"""异步任务管理(内存态,§7、§11.2)。

生产可替换为 Celery / RQ + Redis。当前用 asyncio + Semaphore 控并发。
任务进度以事件日志形式记录,供 SSE 与轮询消费。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
import time
from dataclasses import dataclass, field

from .config import settings
from .models import TaskInfo, TamperReport, TextDiffReport, StatementSummaryReport
from .pipeline import run_pipeline
from .raw_pipeline import run_raw_pipeline
from .statement_pipeline import run_statement_pipeline
from . import storage, webhook

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


@dataclass
class Task:
    info: TaskInfo
    report: TamperReport | None = None
    raw_report: TextDiffReport | None = None  # 无标注版报告(与 report 互斥)
    statement_report: StatementSummaryReport | None = None  # 对帐单金额统计报告(与上两者互斥)
    events: list[dict] = field(default_factory=list)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    callback_url: str | None = None
    callback_secret: str | None = None
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
        self, callback_url: str | None = None, callback_secret: str | None = None
    ) -> str:
        task_id = uuid.uuid4().hex[:16]
        self._tasks[task_id] = Task(
            info=TaskInfo(task_id=task_id, status="pending"),
            callback_url=callback_url,
            callback_secret=callback_secret,
        )
        logger.info(
            "task created task_id=%s callback=%s",
            task_id, "yes" if callback_url else "no",
        )
        return task_id

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    async def run(
        self, task_id: str, word_path: str, pdf_path: str,
        *, enable_llm_judge: bool = False, ocr_backend: str | None = None,
    ) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        logger.info("task start task_id=%s word=%s pdf=%s llm_judge=%s ocr_backend=%s", task_id, word_path, pdf_path, enable_llm_judge, ocr_backend)
        try:
            async with self._sem:
                task.info.status = "running"
                # pipeline 为同步阻塞(OCR/解析),放工作线程
                # 进度回调从工作线程实时推送(stage, fraction)
                report = await asyncio.to_thread(
                    run_pipeline, word_path, pdf_path, settings,
                    on_progress=lambda stage, frac: task.push_event(stage, frac),
                    enable_llm_judge=enable_llm_judge,
                    ocr_backend=ocr_backend,
                )
            task.report = report
            task.info.overall_risk = report.overall_risk
            task.info.status = "done"
            task.push_event("done", 1.0)
            storage.save_report(task_id, report)
            logger.info(
                "task done task_id=%s risk=%s diffs=%s unmatched=%s",
                task_id, report.overall_risk,
                len(report.diffs), len(report.unmatched_clauses),
            )
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
            await self._fire_callback(task_id, "failed", {"error": str(e)})
        finally:
            task.done.set()
            task.finalize_elapsed()
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
        try:
            async with self._sem:
                task.info.status = "running"
                report = await asyncio.to_thread(
                    run_raw_pipeline, word_path, pdf_path,
                    on_progress=lambda stage, frac: task.push_event(stage, frac),
                    char_level=char_level,
                    ocr_backend=ocr_backend,
                )
            task.raw_report = report
            task.info.status = "done"
            task.push_event("done", 1.0)
            storage.save_raw_report(task_id, report)
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
            await self._fire_callback(task_id, "failed", {"error": str(e)})
        finally:
            task.done.set()
            task.finalize_elapsed()
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
        try:
            async with self._sem:
                task.info.status = "running"
                report = await asyncio.to_thread(
                    run_statement_pipeline, pdf_paths, file_names,
                    on_progress=lambda stage, frac: task.push_event(stage, frac),
                    ocr_backend=ocr_backend,
                    amount_column_keywords=amount_column_keywords,
                    enable_llm_column_detection=enable_llm_column_detection,
                )
            task.statement_report = report
            task.info.status = "done"
            task.push_event("done", 1.0)
            storage.save_statement_report(task_id, report)
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
            await self._fire_callback(task_id, "failed", {"error": str(e)})
        finally:
            task.done.set()
            task.finalize_elapsed()
            logger.info(
                "task finished(statement) task_id=%s status=%s elapsed=%ss",
                task_id, task.info.status, task.info.elapsed,
            )

    async def _fire_callback(self, task_id: str, status: str, payload: dict) -> None:
        task = self._tasks.get(task_id)
        if not task or not task.callback_url or not task.callback_secret:
            return
        event, raw = webhook.build_event(task_id, status, payload)
        await webhook.deliver(
            task.callback_url,
            raw,
            task.callback_secret,
            event["event_id"],
            max_retries=settings.webhook_max_retries,
            timeout=settings.webhook_timeout_seconds,
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
