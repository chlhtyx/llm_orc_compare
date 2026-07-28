"""任务进度与阶段耗时测试。"""
from __future__ import annotations

import json
from pathlib import Path

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


async def test_external_task_renders_persisted_truncated_pdf(monkeypatch, tmp_path: Path):
    """外部高亮图不能重新使用上传的全页回收件。"""
    from document_comparison import tasks as tasks_module
    from document_comparison.config import settings
    from document_comparison.models import TamperReport, TruncationRecord
    from document_comparison.storage import compared_pdf_path
    from document_comparison.tasks import TaskManager

    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    report = TamperReport(
        source="source.docx",
        target="compared.pdf",
        truncation=TruncationRecord(
            original_pdf_page_count=3,
            truncated_pdf_page_count=1,
            original_doc_page_count=1,
            doc_page_count_source="explicit",
        ),
    )
    captured: dict[str, Path] = {}

    def _pipeline(*_args, **kwargs):
        compared = Path(kwargs["truncated_pdf_output_path"])
        compared.parent.mkdir(parents=True, exist_ok=True)
        compared.write_bytes(b"truncated")
        return report

    def _render(task_id, pdf_path, _report):
        captured["path"] = Path(pdf_path)
        assert Path(pdf_path) == compared_pdf_path(task_id)
        return []

    monkeypatch.setattr(tasks_module, "run_pipeline", _pipeline)
    monkeypatch.setattr(tasks_module, "render_external_highlight_images", _render)
    for name in (
        "create_task", "update_task_status", "save_milestone_event",
        "save_compare_report", "save_llm_calls_batch",
    ):
        monkeypatch.setattr(tasks_module.db_repo, name, lambda *_a, **_kw: None)

    manager = TaskManager()
    task_id = manager.create("compare", external_request=True)
    await manager.run(task_id, "source.docx", "uploaded-full.pdf", truncate_to_original_pages=True)

    assert captured["path"] == compared_pdf_path(task_id)


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


async def test_semaphore_acquire_timeout_marks_task_failed(monkeypatch):
    """信号量获取超时应置 task=failed 并设 task.done,而非永久 hang。

    复现同步模式卡死场景:槽位被占满后,新任务 acquire 应在
    settings.task_acquire_timeout 秒后超时,被 run 的 except 捕获。
    """
    import asyncio

    from document_comparison import tasks as tasks_module
    from document_comparison.config import settings
    from document_comparison.tasks import TaskManager

    # 超时设短,加速测试;绝不为 0(0 会立即失败,无法验证等待语义)。
    monkeypatch.setattr(settings, "task_acquire_timeout", 0.1)
    # run_pipeline 不应被调用(超时在进入流水线前触发)。
    pipeline_called = []

    def _pipeline(*_a, **_kw):
        pipeline_called.append(True)
        raise AssertionError("run_pipeline should not be reached on timeout")

    monkeypatch.setattr(tasks_module, "run_pipeline", _pipeline)
    for name in (
        "create_task", "update_task_status", "save_milestone_event",
        "save_compare_report", "save_llm_calls_batch",
        "update_callback_result",
    ):
        monkeypatch.setattr(tasks_module.db_repo, name, lambda *_a, **_kw: None)

    manager = TaskManager()
    # 把信号量替换成容量 0 且预占满的:acquire 永远拿不到,必走超时分支。
    manager._sem = asyncio.Semaphore(0)

    task_id = manager.create(
        "compare",
        callback_url="http://internal/hook",
        document_no="BILL-TIMEOUT",
        external_request=True,
    )

    await manager.run(task_id, "source.docx", "target.pdf")

    task = manager.get(task_id)
    assert task is not None
    assert task.info.status == "failed"
    assert "等待执行槽位超时" in (task.info.error or "")
    assert task.done.is_set()
    assert pipeline_called == []  # 流水线未执行


async def test_sync_mode_schedules_callback_without_blocking(monkeypatch):
    """sync_mode=True 时 webhook 应后台发送,_fire_callback 不阻塞 run 返回。

    验证:run 返回时 task.pending_callbacks 非空,且后台任务最终完成。
    """
    import asyncio

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
        tasks_module, "build_external_result",
        lambda *_a: {"change_status": "clean"},
    )
    for name in (
        "create_task", "update_task_status", "save_milestone_event",
        "save_compare_report", "save_llm_calls_batch",
        "update_callback_result",
    ):
        monkeypatch.setattr(tasks_module.db_repo, name, lambda *_a, **_kw: None)

    deliver_started = []

    async def _slow_deliver(*_a, **_kw):
        # 模拟慢 webhook:进入即标记,然后让出控制权(让 run 先返回)。
        deliver_started.append(True)
        await asyncio.sleep(0.05)
        return {"success": True, "http_status": 200, "error": None}

    monkeypatch.setattr(tasks_module.webhook, "deliver", _slow_deliver)

    manager = TaskManager()
    task_id = manager.create(
        "compare",
        callback_url="http://internal/hook",
        document_no="BILL-SYNC",
        external_request=True,
        sync_mode=True,  # 关键:同步模式
    )

    await manager.run(task_id, "source.docx", "target.pdf")

    task = manager.get(task_id)
    assert task is not None
    assert task.info.status == "done"
    # run 返回时,后台 callback 任务应已挂起(pending_callbacks 非空)。
    assert len(task.pending_callbacks) == 1
    # 等待后台任务完成,确认 webhook 确实被调用。
    await asyncio.gather(*task.pending_callbacks)
    assert deliver_started == [True]


async def test_async_mode_still_awaits_callback_inline(monkeypatch):
    """非 sync_mode(异步模式)时 webhook 仍同步 await,行为不变。"""
    from document_comparison import tasks as tasks_module
    from document_comparison.models import TamperReport
    from document_comparison.tasks import TaskManager

    report = TamperReport(
        source="source.docx", target="target.pdf",
        overall_risk="clean", change_status="clean",
        summary={"status_counts": {}},
    )
    monkeypatch.setattr(tasks_module, "run_pipeline", lambda *_a, **_kw: report)
    monkeypatch.setattr(tasks_module, "render_external_highlight_images", lambda *_a: [])
    monkeypatch.setattr(
        tasks_module, "build_external_result",
        lambda *_a: {"change_status": "clean"},
    )
    for name in (
        "create_task", "update_task_status", "save_milestone_event",
        "save_compare_report", "save_llm_calls_batch",
        "update_callback_result",
    ):
        monkeypatch.setattr(tasks_module.db_repo, name, lambda *_a, **_kw: None)

    delivered = []

    async def _deliver(*_a, **_kw):
        delivered.append(True)
        return {"success": True, "http_status": 200, "error": None}

    monkeypatch.setattr(tasks_module.webhook, "deliver", _deliver)

    manager = TaskManager()
    task_id = manager.create(
        "compare",
        callback_url="http://internal/hook",
        document_no="BILL-ASYNC",
        external_request=True,
        sync_mode=False,  # 异步模式
    )

    await manager.run(task_id, "source.docx", "target.pdf")

    task = manager.get(task_id)
    assert task is not None
    # 异步模式:run 返回前 webhook 已同步发完,无后台任务。
    assert task.pending_callbacks == []
    assert delivered == [True]
