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
            "recognition_status": "reliable",
            "location_status": "complete",
            "summary": {"status_counts": {}},
            "result_text": "未发现内容变化",
            "highlight_images": [{"page_number": 1, "has_highlight": False, "url": "u"}],
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

    async def _deliver(url, raw, secret, event_id, **_kwargs):
        delivered.update(
            url=url,
            body=json.loads(raw),
            secret=secret,
            event_id=event_id,
        )
        return {"success": True, "http_status": 200, "error": None}

    monkeypatch.setattr(tasks_module.webhook, "deliver", _deliver)
    manager = TaskManager()
    task_id = manager.create(
        "compare",
        callback_url="http://internal/hook",
        callback_secret="secret",
        document_no="BILL-1",
        external_request=True,
    )

    await manager.run(task_id, "source.docx", "target.pdf")

    body = delivered["body"]
    assert body["event_type"] == "contract.compare.completed"
    assert body["task_id"] == task_id
    assert body["document_no"] == "BILL-1"
    assert body["result"]["result_text"] == "未发现内容变化"
    assert body["highlight_images"][0]["page_number"] == 1
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

    async def _deliver(_url, raw, _secret, _event_id, **_kwargs):
        delivered.update(json.loads(raw))
        return {"success": True, "http_status": 200, "error": None}

    monkeypatch.setattr(tasks_module.webhook, "deliver", _deliver)
    manager = TaskManager()
    task_id = manager.create(
        "compare",
        callback_url="http://internal/hook",
        callback_secret="secret",
        document_no="BILL-2",
        external_request=True,
    )

    await manager.run(task_id, "source.docx", "target.pdf")

    assert manager.get(task_id).info.status == "failed"
    assert delivered["event_type"] == "contract.compare.failed"
    assert delivered["document_no"] == "BILL-2"
    assert "highlight_images" not in delivered
    assert delivered["error"] == "image render failed"
