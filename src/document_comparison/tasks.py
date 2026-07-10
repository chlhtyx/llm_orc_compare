"""异步任务管理(内存态,§7、§11.2)。

生产可替换为 Celery / RQ + Redis。当前用 asyncio + Semaphore 控并发。
任务进度以事件日志形式记录,供 SSE 与轮询消费。
"""
from __future__ import annotations

import asyncio
import uuid
import time
from dataclasses import dataclass, field

from .config import settings
from .models import TaskInfo, TamperReport
from .pipeline import run_pipeline
from . import storage, webhook


@dataclass
class Task:
    info: TaskInfo
    report: TamperReport | None = None
    events: list[dict] = field(default_factory=list)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    callback_url: str | None = None
    callback_secret: str | None = None
    start_time: float = field(default_factory=time.monotonic)

    def finalize_elapsed(self) -> None:
        """终结时计算总耗时(秒),写入 info.elapsed。"""
        if self.start_time:
            self.info.elapsed = round(time.monotonic() - self.start_time, 2)

    def push_event(self, stage: str, progress: float) -> None:
        self.events.append({"stage": stage, "progress": progress})
        self.info.stage = stage
        self.info.progress = progress


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
        return task_id

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    async def run(self, task_id: str, word_path: str, pdf_path: str) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        try:
            async with self._sem:
                task.info.status = "running"
                # pipeline 为同步阻塞(OCR/解析),放工作线程
                # 进度回调从工作线程实时推送(stage, fraction)
                report = await asyncio.to_thread(
                    run_pipeline, word_path, pdf_path, settings,
                    on_progress=lambda stage, frac: task.push_event(stage, frac),
                )
            task.report = report
            task.info.overall_risk = report.overall_risk
            task.info.status = "done"
            task.push_event("done", 1.0)
            storage.save_report(task_id, report)
            await self._fire_callback(task_id, "done", {
                "overall_risk": report.overall_risk,
                "summary": report.summary,
                "report_url": f"/api/v1/compare/{task_id}/report?format=json",
            })
        except Exception as e:  # noqa: BLE001
            task.info.status = "failed"
            task.info.error = str(e)
            task.push_event("failed", task.info.progress)
            await self._fire_callback(task_id, "failed", {"error": str(e)})
        finally:
            task.done.set()
            task.finalize_elapsed()

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
