"""任务进度与阶段耗时测试。"""
from __future__ import annotations

import json

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


async def test_external_task_callback_contract(monkeypatch):
    from document_comparison import tasks as tasks_module
    from document_comparison.models import TamperReport
    from document_comparison.tasks import TaskManager

    report = TamperReport(
        source="source.docx",
        target="target.pdf",
        overall_risk="clean",
        change_status="clean",
        summary={"status_counts": {}},
    )
    monkeypatch.setattr(tasks_module, "run_pipeline", lambda *_a, **_kw: report)
    monkeypatch.setattr(tasks_module, "render_external_highlight_images", lambda *_a: [])
    monkeypatch.setattr(
        tasks_module,
        "build_external_result",
        lambda *_a: {
            "change_status": "clean",
            "result_text": "未发现内容变化",
            "highlight_images": ["u"],
            "result_url": "https://example.test/result",
        },
    )
    for name in (
        "create_task", "update_task_status", "save_milestone_event",
        "save_compare_report", "save_llm_calls_batch",
    ):
        monkeypatch.setattr(tasks_module.db_repo, name, lambda *_a, **_kw: None)

    callback_results: list[dict] = []

    def _update_callback_result(task_id_, *, success, http_status=None, error=None):
        callback_results.append(
            {"task_id": task_id_, "success": success, "http_status": http_status, "error": error}
        )

    monkeypatch.setattr(
        tasks_module.db_repo, "update_callback_result", _update_callback_result
    )

    delivered: dict = {}

    async def _deliver(url, raw, event_id, **_kwargs):
        delivered.update(
            url=url,
            body=json.loads(raw),
            event_id=event_id,
        )
        return {"success": True, "http_status": 200, "error": None}

    monkeypatch.setattr(tasks_module.webhook, "deliver", _deliver)
    manager = TaskManager()
    task_id = manager.create(
        "compare",
        callback_url="http://internal/hook",
        document_no="BILL-1",
        external_request=True,
    )

    await manager.run(task_id, "source.docx", "target.pdf")

    body = delivered["body"]
    assert body["event_type"] == "contract.compare.completed"
    assert body["task_id"] == task_id
    assert body["document_no"] == "BILL-1"
    assert body["result_text"] == "未发现内容变化"
    assert body["highlight_images"] == ["u"]
    assert body["result_url"] == "https://example.test/result"
    assert delivered["event_id"] == body["event_id"]
    # 交付结果应回写为成功
    assert callback_results == [
        {"task_id": task_id, "success": True, "http_status": 200, "error": None}
    ]


async def test_external_image_failure_sends_failed_callback(monkeypatch):
    from document_comparison import tasks as tasks_module
    from document_comparison.models import TamperReport
    from document_comparison.tasks import TaskManager

    report = TamperReport(source="s.docx", target="t.pdf")
    monkeypatch.setattr(tasks_module, "run_pipeline", lambda *_a, **_kw: report)

    def _fail_render(*_args):
        raise RuntimeError("image render failed")

    monkeypatch.setattr(tasks_module, "render_external_highlight_images", _fail_render)
    for name in (
        "create_task", "update_task_status", "save_milestone_event",
        "save_compare_report", "save_llm_calls_batch",
    ):
        monkeypatch.setattr(tasks_module.db_repo, name, lambda *_a, **_kw: None)

    monkeypatch.setattr(
        tasks_module.db_repo, "update_callback_result", lambda *_a, **_kw: None
    )

    delivered: dict = {}

    async def _deliver(_url, raw, _event_id, **_kwargs):
        delivered.update(json.loads(raw))
        return {"success": True, "http_status": 200, "error": None}

    monkeypatch.setattr(tasks_module.webhook, "deliver", _deliver)
    manager = TaskManager()
    task_id = manager.create(
        "compare",
        callback_url="http://internal/hook",
        document_no="BILL-2",
        external_request=True,
    )

    await manager.run(task_id, "source.docx", "target.pdf")

    assert manager.get(task_id).info.status == "failed"
    assert delivered["event_type"] == "contract.compare.failed"
    assert delivered["document_no"] == "BILL-2"
    assert "highlight_images" not in delivered
    assert delivered["error"] == "image render failed"


# —— event_stream(从 PG task_events 增量推送里程碑)——


class _FakeEvent:
    """模拟 TaskEvent ORM 对象,仅含 event_stream 用到的字段。"""

    def __init__(self, id, stage, progress, stage_timings):
        self.id = id
        self.stage = stage
        self.progress = progress
        self.stage_timings = stage_timings


async def test_event_stream_reads_milestones_from_pg(monkeypatch):
    """event_stream 应从 PG 增量拉里程碑,并在终态后退出。"""
    from document_comparison import tasks as tasks_module
    from document_comparison.tasks import TaskManager

    # 模拟两次轮询:第一次返回 start 里程碑(running),第二次返回 done 里程碑(终态)。
    events_db = {
        0: [_FakeEvent(1, "start", 0.0, {})],
        1: [_FakeEvent(2, "done", 1.0, {"ocr": 5.0})],
        # 终态后再拉一次(id=2 已发,返回空)
    }
    status_seq = iter(["running", "done"])

    def _events_after(task_id, last_id):
        return events_db.get(last_id, [])

    def _status(task_id):
        return next(status_seq)

    monkeypatch.setattr(tasks_module.db_repo, "get_task_events_after", _events_after)
    monkeypatch.setattr(tasks_module.db_repo, "get_task_status", _status)
    monkeypatch.setattr(
        "document_comparison.tasks.asyncio.sleep", lambda _t: _noop_coro()
    )

    manager = TaskManager()
    collected = []
    async for ev in manager.event_stream("t1"):
        collected.append(ev)

    # 应只含两条里程碑,且 stage/progress/timings 正确
    assert [e["stage"] for e in collected] == ["start", "done"]
    assert collected[1]["stage_timings"] == {"ocr": 5.0}


async def test_event_stream_returns_when_task_not_found(monkeypatch):
    """任务不存在(status=None)时,event_stream 立即返回,不产生任何事件。"""
    from document_comparison import tasks as tasks_module
    from document_comparison.tasks import TaskManager

    monkeypatch.setattr(
        tasks_module.db_repo, "get_task_events_after", lambda *_a, **_kw: []
    )
    monkeypatch.setattr(tasks_module.db_repo, "get_task_status", lambda *_a, **_kw: None)

    manager = TaskManager()
    collected = []
    async for ev in manager.event_stream("missing"):
        collected.append(ev)

    assert collected == []


async def _noop_coro():
    """给 monkeypatch asyncio.sleep 用的空 awaitable。"""
    return None

