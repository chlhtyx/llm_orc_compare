"""LLM 调用记录端到端追踪:验证从 pipeline 深处(asyncio.to_thread + threading.Thread
孙线程)经 contextvar 收集器,到 tasks.py 批量入库的完整链路。

不启动真实 OCR/LLM,而是用 monkeypatch 把 pipeline 函数替换成「模拟在孙线程里
调 log_model_request/response」的桩,这样能精确控制调用次数与 kind,断言收集行为。
"""
from __future__ import annotations

import asyncio
import contextvars
import threading

import pytest

from document_comparison.observability import (
    llm_call_collector,
    log_model_request,
    log_model_response,
)
from document_comparison.db import repository as db_repo


pytestmark = pytest.mark.usefixtures("db_isolated")


def _fake_pipeline_that_calls_llm(kinds: list[str], *, via_thread: bool = True):
    """返回一个 callable,模拟 pipeline 在工作线程(可选孙线程)里调若干次 LLM。

    via_thread=True 时,LLM 调用在 threading.Thread 孙线程里发生(模拟 OCR 逐页并发),
    验证 _BoundedConcurrency 的 ctx.run 传播。
    """
    import logging
    log = logging.getLogger("fake-pipeline")

    def _call_all():
        for i, kind in enumerate(kinds):
            t = log_model_request(log, kind, "http://x/v1", {"model": "m", "idx": i}, 1)
            log_model_response(log, kind, 200, {"choices": [{"message": {"content": f"resp-{i}"}}]}, t)

    def _runner():
        if via_thread:
            # 模拟 _BoundedConcurrency:捕获 ctx 并在孙线程 ctx.run
            ctx = contextvars.copy_context()
            done = threading.Event()
            errors: list[BaseException] = []

            def _in_thread():
                try:
                    ctx.run(_call_all)
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)
                finally:
                    done.set()

            t = threading.Thread(target=_in_thread, daemon=True)
            t.start()
            done.wait(5.0)
            if errors:
                raise errors[0]
        else:
            _call_all()

    return _runner


# —— 纯收集器传播(不涉及 DB)——

def test_collector_propagates_through_to_thread_and_threading():
    """contextvar 经 asyncio.to_thread → threading.Thread(ctx.run) 正确传播。

    这是 OCR 逐页调用的真实路径:event-loop → to_thread 工作线程 →
    _BoundedConcurrency 孙线程。
    """
    fake = _fake_pipeline_that_calls_llm(["ocr", "ocr", "judge"], via_thread=True)

    with llm_call_collector() as recs:
        asyncio.run(asyncio.to_thread(fake))

    assert len(recs) == 3
    assert [r.kind for r in recs] == ["ocr", "ocr", "judge"]
    assert all(r.status_code == 200 for r in recs)
    assert all(r.response is not None for r in recs)


def test_collector_propagates_without_threading():
    """contextvar 经 asyncio.to_thread(直接在工作线程调用)正确传播。"""
    fake = _fake_pipeline_that_calls_llm(["ocr", "llm-diff"], via_thread=False)

    with llm_call_collector() as recs:
        asyncio.run(asyncio.to_thread(fake))

    assert len(recs) == 2
    assert [r.kind for r in recs] == ["ocr", "llm-diff"]


# —— 完整链路:收集 → 批量入库 → 查询 ——

def test_end_to_end_collect_then_persist(db_isolated):
    """模拟 tasks.py 的完整流程:llm_call_collector 包 to_thread → save_llm_calls_batch → get。"""
    fake = _fake_pipeline_that_calls_llm(
        ["ocr", "ocr", "ocr", "judge", "statement-column"],
        via_thread=True,
    )

    with llm_call_collector() as recs:
        asyncio.run(asyncio.to_thread(fake))

    assert len(recs) == 5
    # 批量入库
    db_repo.create_task("e2e-1", "compare")
    n = db_repo.save_llm_calls_batch("e2e-1", recs)
    assert n == 5

    got = db_repo.get_task_llm_calls("e2e-1")
    assert len(got) == 5
    assert [c.kind for c in got] == ["ocr", "ocr", "ocr", "judge", "statement-column"]
    assert all(c.status_code == 200 for c in got)
    # payload 里带 idx
    assert got[0].payload["idx"] == 0
    assert got[3].payload["idx"] == 3
    # response 透传
    assert got[4].response["choices"][0]["message"]["content"] == "resp-4"
