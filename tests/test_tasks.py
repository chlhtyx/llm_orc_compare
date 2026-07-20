"""任务进度与阶段耗时测试。"""
from __future__ import annotations

from document_comparison.models import TaskInfo
from document_comparison.tasks import Task


def test_progress_events_and_task_info_include_stage_timings(monkeypatch):
    ticks = iter([100.0, 102.0, 107.5, 108.0])
    monkeypatch.setattr("document_comparison.tasks.time.monotonic", lambda: next(ticks))
    task = Task(
        info=TaskInfo(task_id="timed-task"),
        start_time=100.0,
    )

    task.push_event("word_parsing", 0.02)
    task.push_event("ocr", 0.10)
    task.push_event("structure", 0.72)
    task.push_event("done", 1.0)

    assert task.info.stage_timings == {
        "word": 2.0,
        "ocr": 5.5,
        "structure": 0.5,
    }
    assert task.events[-1]["stage_timings"] == task.info.stage_timings
