"""db 包:任务记录与里程碑事件 CRUD 与列表查询测试。

依赖 Postgres。无 DATABASE_URL_TEST / DATABASE_URL 时自动跳过。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from document_comparison.db import repository as db_repo
from document_comparison.db.models import TaskEvent, TaskRecord
from document_comparison.models import (
    Diff,
    OverallRisk,
    StatementSummaryReport,
    TamperReport,
    TextDiffReport,
)


pytestmark = pytest.mark.usefixtures("db_isolated")


def test_task_record_lifecycle(db_isolated):
    """create → update_status → save_compare_report → 查询,字段正确。"""
    db_repo.create_task(
        "t1", "compare",
        source_name="a.docx",
        target_names=["b.pdf"],
        ocr_backend="llm",
    )

    rec = db_repo.get_task("t1")
    assert rec is not None
    assert rec.kind == "compare"
    assert rec.status == "pending"
    assert rec.source_name == "a.docx"
    assert rec.target_names == ["b.pdf"]
    assert rec.ocr_backend == "llm"

    db_repo.update_task_status(
        "t1", "running",
        stage_timings={"word": 0.5},
    )
    rec = db_repo.get_task("t1")
    assert rec.status == "running"
    assert rec.stage_timings == {"word": 0.5}

    report = TamperReport(
        source="a.docx",
        target="b.pdf",
        overall_risk="high",  # type: ignore[arg-type]
        change_status="changed",
    )
    db_repo.save_compare_report("t1", report)
    db_repo.update_task_status(
        "t1", "done",
        overall_risk="high",
        change_status="changed",
        elapsed=2.34,
        finished=True,
    )

    rec = db_repo.get_task("t1")
    assert rec is not None
    assert rec.status == "done"
    assert rec.overall_risk == "high"
    assert rec.change_status == "changed"
    assert rec.elapsed == pytest.approx(2.34)
    assert rec.finished_at is not None
    assert rec.report_compare is not None
    assert rec.report_compare["overall_risk"] == "high"
    assert rec.report_compare["source"] == "a.docx"


def test_save_raw_and_statement_reports(db_isolated):
    """raw / statement 报告分别写入对应 JSONB 列。"""
    db_repo.create_task("r1", "raw", target_names=["t.pdf"])
    db_repo.create_task("s1", "statement", target_names=["m1.pdf", "m2.pdf"])

    raw = TextDiffReport(source="a", target="b")
    db_repo.save_raw_report("r1", raw)

    stmt = StatementSummaryReport(
        files=[],
        grand_total=123.45,
        verdict="clean",
    )
    db_repo.save_statement_report("s1", stmt)

    r1 = db_repo.get_task("r1")
    s1 = db_repo.get_task("s1")
    assert r1.report_raw is not None
    assert r1.report_raw["source"] == "a"
    assert r1.report_compare is None
    assert r1.report_statement is None

    assert s1.report_statement is not None
    assert s1.report_statement["grand_total"] == 123.45
    assert s1.target_names == ["m1.pdf", "m2.pdf"]


def test_milestone_events_saved(db_isolated):
    """save_milestone_event 追加多条,get_task_events 按 id 升序返回。"""
    db_repo.create_task("m1", "compare")
    db_repo.save_milestone_event("m1", "start", 0.0, {})
    db_repo.save_milestone_event("m1", "word_done", 0.1, {"word": 0.5})
    db_repo.save_milestone_event("m1", "done", 1.0, {"word": 0.5, "ocr": 1.2})

    events = db_repo.get_task_events("m1")
    assert [e.stage for e in events] == ["start", "word_done", "done"]
    assert all(e.milestone is True for e in events)
    assert events[1].stage_timings == {"word": 0.5}


def test_save_milestone_event_orphan_skipped(db_isolated):
    """未注册 task_id 时事件不写入(避免孤儿)。"""
    db_repo.save_milestone_event("ghost", "start", 0.0, {})
    assert db_repo.get_task_events("ghost") == []


def test_get_task_events_after_incremental(db_isolated):
    """get_task_events_after 按 last_id 增量返回(id 升序)。"""
    db_repo.create_task("ea1", "compare")
    db_repo.save_milestone_event("ea1", "start", 0.0, {})
    db_repo.save_milestone_event("ea1", "word_done", 0.1, {"word": 0.5})
    db_repo.save_milestone_event("ea1", "done", 1.0, {"word": 0.5, "ocr": 1.2})

    all_events = db_repo.get_task_events("ea1")
    assert len(all_events) == 3

    # last_id=0 → 全部
    evs = db_repo.get_task_events_after("ea1", 0)
    assert [e.stage for e in evs] == ["start", "word_done", "done"]

    # last_id=第一条的 id → 剩余两条
    first_id = all_events[0].id
    evs2 = db_repo.get_task_events_after("ea1", first_id)
    assert [e.stage for e in evs2] == ["word_done", "done"]

    # last_id=最后一条的 id → 空
    last_id = all_events[-1].id
    assert db_repo.get_task_events_after("ea1", last_id) == []


def test_get_task_status(db_isolated):
    """get_task_status 返回状态字符串,不存在返回 None。"""
    db_repo.create_task("st1", "compare")
    db_repo.update_task_status("st1", "running")
    assert db_repo.get_task_status("st1") == "running"
    db_repo.update_task_status("st1", "done")
    assert db_repo.get_task_status("st1") == "done"
    assert db_repo.get_task_status("missing-task") is None


def test_count_active_tasks(db_isolated):
    """count_active_tasks 只统计 pending/running,跨 worker 全局计数。"""
    # 准备:2 pending + 1 running + 2 done + 1 failed
    db_repo.create_task("ca-p1", "compare")  # pending
    db_repo.create_task("ca-p2", "compare")  # pending
    db_repo.create_task("ca-r1", "compare")
    db_repo.update_task_status("ca-r1", "running")  # running
    db_repo.create_task("ca-d1", "compare")
    db_repo.update_task_status("ca-d1", "done")  # done
    db_repo.create_task("ca-d2", "compare")
    db_repo.update_task_status("ca-d2", "done")  # done
    db_repo.create_task("ca-f1", "compare")
    db_repo.update_task_status("ca-f1", "failed")  # failed

    assert db_repo.count_active_tasks() == 3  # 2 pending + 1 running

    # 一个 running 转为 done → 计数下降
    db_repo.update_task_status("ca-r1", "done")
    assert db_repo.count_active_tasks() == 2



def test_list_tasks_filter_and_pagination(db_isolated):
    """kind/status 过滤 + 分页 + 倒序。"""
    # 准备:3 条 compare done + 2 条 statement done + 1 条 raw running
    for i in range(3):
        db_repo.create_task(f"c{i}", "compare", target_names=[f"t{i}.pdf"])
        db_repo.update_task_status(f"c{i}", "done", overall_risk="clean")
    for i in range(2):
        db_repo.create_task(f"s{i}", "statement", target_names=[f"s{i}.pdf"])
        db_repo.update_task_status(f"s{i}", "done")
    db_repo.create_task("r0", "raw", target_names=["r.pdf"])
    db_repo.update_task_status("r0", "running")

    # 全量
    recs, total = db_repo.list_tasks(limit=50)
    assert total == 6
    assert len(recs) == 6
    # 倒序(后续创建的在前 — 因 created_at 相同时可能同秒,需看具体顺序)
    # 由于 created_at 精度到秒,这里仅校验数量
    recs_cmp, total_cmp = db_repo.list_tasks(kind="compare")
    assert total_cmp == 3
    assert {r.task_id for r in recs_cmp} == {"c0", "c1", "c2"}

    recs_stmt, total_stmt = db_repo.list_tasks(kind="statement")
    assert total_stmt == 2

    recs_run, total_run = db_repo.list_tasks(status="running")
    assert total_run == 1
    assert recs_run[0].task_id == "r0"

    # 分页:limit=2, offset=0 → 2 条
    page1, _ = db_repo.list_tasks(limit=2, offset=0)
    page2, _ = db_repo.list_tasks(limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 2
    assert {r.task_id for r in page1}.isdisjoint({r.task_id for r in page2})


def test_list_tasks_q_search(db_isolated):
    """q 模糊搜索命中 task_id / document_no / source_name / target_names,且 total 与 records 一致。"""
    db_repo.create_task(
        "task_alpha", "compare",
        source_name="contract_alpha.docx",
        target_names=["recovered_alpha.pdf"],
        document_no="DT-2024-0001",
    )
    db_repo.create_task(
        "task_beta", "raw",
        source_name="note_beta.txt",
        target_names=["scan_beta.pdf"],
        document_no="DT-2024-0002",
    )
    db_repo.create_task(
        "task_gamma", "statement",
        target_names=["bank_stmt.pdf"],
        document_no=None,
    )
    db_repo.update_task_status("task_alpha", "done")
    db_repo.update_task_status("task_beta", "done")
    db_repo.update_task_status("task_gamma", "done")

    # 命中 task_id
    recs, total = db_repo.list_tasks(q="task_alpha")
    assert total == 1
    assert [r.task_id for r in recs] == ["task_alpha"]

    # 命中 document_no(大小写不敏感)
    recs, total = db_repo.list_tasks(q="dt-2024-0001")
    assert total == 1
    assert recs[0].task_id == "task_alpha"

    # 命中 document_no 前缀 → 两条 DT-2024-*
    recs, total = db_repo.list_tasks(q="DT-2024")
    assert total == 2
    assert {r.task_id for r in recs} == {"task_alpha", "task_beta"}

    # 命中 source_name
    recs, total = db_repo.list_tasks(q="alpha.docx")
    assert total == 1
    assert recs[0].task_id == "task_alpha"

    # 命中 target_names(JSONB cast → text)
    recs, total = db_repo.list_tasks(q="bank_stmt")
    assert total == 1
    assert recs[0].task_id == "task_gamma"

    # 子串命中多条(都含 "_beta")
    recs, total = db_repo.list_tasks(q="beta")
    assert total == 1
    assert recs[0].task_id == "task_beta"

    # 无命中
    recs, total = db_repo.list_tasks(q="不存在的关键字xyz")
    assert total == 0
    assert recs == []

    # q 可与 kind 叠加
    recs, total = db_repo.list_tasks(q="DT-2024", kind="raw")
    assert total == 1
    assert recs[0].task_id == "task_beta"

    # 空白/空 q 等同未搜索(全量)
    recs, total = db_repo.list_tasks(q="")
    assert total == 3


def test_update_task_status_unknown_task_is_noop(db_isolated, caplog):
    """未知 task_id 的更新不应抛错,仅记日志。"""
    db_repo.update_task_status("nope", "done")
    # 无异常即通过
    assert db_repo.get_task("nope") is None


def test_to_dict_excludes_report_by_default(db_isolated):
    """to_dict 默认不含 report_*,include_report=True 才返回。"""
    db_repo.create_task("d1", "compare", target_names=["x.pdf"])
    db_repo.save_compare_report(
        "d1",
        TamperReport(source="a", target="b"),
    )
    rec = db_repo.get_task("d1")
    assert rec is not None

    light = db_repo.to_dict(rec)
    assert "report_compare" not in light
    assert light["task_id"] == "d1"
    assert light["kind"] == "compare"

    full = db_repo.to_dict(rec, include_report=True)
    assert full["report_compare"] is not None


def test_callback_url_and_status_persisted(db_isolated):
    """提交带 callback_url 时初始 callback_status="pending";to_dict 暴露 5 字段。"""
    db_repo.create_task(
        "cb1", "compare",
        target_names=["t.pdf"],
        callback_url="http://internal/hook",
    )
    rec = db_repo.get_task("cb1")
    assert rec is not None
    assert rec.callback_url == "http://internal/hook"
    assert rec.callback_status == "pending"

    data = db_repo.to_dict(rec)
    assert data["callback_url"] == "http://internal/hook"
    assert data["callback_status"] == "pending"
    assert data["callback_http_status"] is None
    assert data["callback_error"] is None
    assert data["callback_at"] is None


def test_callback_url_absent_leaves_status_none(db_isolated):
    """未配置回调时 callback_url/status 保持 None。"""
    db_repo.create_task("cb2", "compare", target_names=["t.pdf"])
    rec = db_repo.get_task("cb2")
    assert rec is not None
    assert rec.callback_url is None
    assert rec.callback_status is None


def test_update_callback_result_success(db_isolated):
    """成功交付回写 success + HTTP 状态码 + 时间。"""
    db_repo.create_task("cb3", "compare", callback_url="http://x/hook")
    db_repo.update_callback_result("cb3", success=True, http_status=200)
    rec = db_repo.get_task("cb3")
    assert rec is not None
    assert rec.callback_status == "success"
    assert rec.callback_http_status == 200
    assert rec.callback_error is None
    assert rec.callback_at is not None


def test_update_callback_result_failed_truncates_error(db_isolated):
    """失败交付回写 failed + 错误信息(截断 512);连接级失败 http_status=None。"""
    db_repo.create_task("cb4", "compare", callback_url="http://x/hook")
    long_err = "E" * 600
    db_repo.update_callback_result(
        "cb4", success=False, http_status=None, error=long_err,
    )
    rec = db_repo.get_task("cb4")
    assert rec is not None
    assert rec.callback_status == "failed"
    assert rec.callback_http_status is None
    assert len(rec.callback_error or "") == 512


def test_update_callback_result_unknown_task_is_noop(db_isolated, caplog):
    """任务记录不存在时只记日志,不抛。"""
    db_repo.update_callback_result("nope", success=True, http_status=200)
    assert any("not found" in r.message for r in caplog.records)


def test_save_callback_payload_persists_and_serializes(db_isolated):
    """save_callback_payload 落库业务 payload,to_dict 透出供重新推送还原。"""
    db_repo.create_task("cbp1", "compare", callback_url="http://x/hook")
    payload = {"event_type": "contract.compare.completed", "document_no": "B1"}
    db_repo.save_callback_payload("cbp1", payload)
    rec = db_repo.get_task("cbp1")
    assert rec is not None
    assert rec.callback_payload == payload
    assert db_repo.to_dict(rec)["callback_payload"] == payload


def test_save_callback_payload_unknown_task_is_noop(db_isolated, caplog):
    """任务记录不存在时只记日志,不抛。"""
    db_repo.save_callback_payload("nope", {"a": 1})
    assert any("not found" in r.message for r in caplog.records)


# —— LLM 配置 JSONB CRUD(单行表,id 固定为 1)——

def test_llm_config_get_empty_when_fresh(db_isolated):
    """空库无记录时,get_llm_config 返回空 dict。"""
    assert db_repo.get_llm_config() == {}


def test_llm_config_save_and_reload(db_isolated):
    """save_llm_config 覆盖整份配置,get_llm_config 能读回;白名单不做过滤(repo 透明)。"""
    payload = {
        "llm_api_base": "https://example.com/v1",
        "llm_api_key": "sk-test",
        "max_pdf_pages": 15,
        "pdf_render_dpi": 300,
    }
    db_repo.save_llm_config(payload)
    got = db_repo.get_llm_config()
    assert got["llm_api_base"] == "https://example.com/v1"
    assert got["llm_api_key"] == "sk-test"
    assert got["max_pdf_pages"] == 15
    assert got["pdf_render_dpi"] == 300

    # 第二次 save 是整份覆盖(不是合并);旧字段消失。
    db_repo.save_llm_config({"embed_backend": "mock"})
    got2 = db_repo.get_llm_config()
    assert got2 == {"embed_backend": "mock"}


# —— LLM 调用记录 CRUD(task_llm_calls 表)——

def test_save_llm_calls_batch_and_get(db_isolated):
    """批量写入 LlmCallRecord,按 id 升序读回,字段完整;payload 图片已脱敏。"""
    from document_comparison.observability import LlmCallRecord

    db_repo.create_task("llm-1", "compare", target_names=["t.pdf"])
    records = [
        LlmCallRecord(
            kind="ocr", attempt=1,
            payload={"model": "m", "image": {"url": "data:image/png;base64,AAAA"}},
        ),
        LlmCallRecord(
            kind="judge", attempt=2,
            payload={"model": "j"},
            status_code=429,
            elapsed_ms=120,
            error="transient HTTP 429",
        ),
        LlmCallRecord(
            kind="ocr", attempt=1,
            payload={"model": "m"},
            status_code=200,
            elapsed_ms=350,
            response={"choices": [{"message": {"content": "page1"}}]},
        ),
    ]
    # 这些 record 在收集时已经脱敏/补全,这里模拟「已脱敏」状态
    records[0].payload["image"]["url"] = {
        "data_url": "data:image/png;base64", "base64_chars": 4, "sha256": "abc123",
    }

    count = db_repo.save_llm_calls_batch("llm-1", records)
    assert count == 3

    got = db_repo.get_task_llm_calls("llm-1")
    assert len(got) == 3
    # 按 id 升序(写入顺序)
    assert [c.kind for c in got] == ["ocr", "judge", "ocr"]
    assert [c.attempt for c in got] == [1, 2, 1]
    # 字段完整
    assert got[0].status_code is None        # 第一条未补全(模拟请求发出但未记录响应)
    assert got[1].status_code == 429
    assert got[1].error == "transient HTTP 429"
    assert got[2].status_code == 200
    assert got[2].response["choices"][0]["message"]["content"] == "page1"
    # 图片脱敏字段存住
    img = got[0].payload["image"]["url"]
    assert img["sha256"] == "abc123"
    assert "AAAA" not in str(got[0].payload)


def test_save_llm_calls_batch_empty_is_noop(db_isolated):
    """空列表批量写入是 noop,返回 0。"""
    db_repo.create_task("llm-2", "compare")
    assert db_repo.save_llm_calls_batch("llm-2", []) == 0
    assert db_repo.get_task_llm_calls("llm-2") == []


def test_llm_calls_cascade_delete_with_task(db_isolated):
    """删除 task 后,其 llm_calls 自动级联删除(ON DELETE CASCADE)。"""
    from sqlalchemy import delete

    from document_comparison.observability import LlmCallRecord
    from document_comparison.db.models import TaskRecord

    db_repo.create_task("llm-3", "compare")
    db_repo.save_llm_calls_batch("llm-3", [
        LlmCallRecord(kind="ocr", attempt=1, payload={"model": "m"}),
    ])
    assert len(db_repo.get_task_llm_calls("llm-3")) == 1

    # 直接删 task_records(走 CASCADE)
    from document_comparison.db.engine import session_scope
    with session_scope() as s:
        s.execute(delete(TaskRecord).where(TaskRecord.task_id == "llm-3"))
    assert db_repo.get_task_llm_calls("llm-3") == []


def test_llm_call_to_dict_serialization(db_isolated):
    """llm_call_to_dict 字段齐全,JSON 友好(created_at ISO 化)。"""
    from document_comparison.observability import LlmCallRecord

    db_repo.create_task("llm-4", "compare")
    db_repo.save_llm_calls_batch("llm-4", [
        LlmCallRecord(
            kind="ocr", attempt=1, payload={"model": "m"},
            status_code=200, elapsed_ms=50,
            response={"ok": True},
        ),
    ])
    got = db_repo.get_task_llm_calls("llm-4")
    d = db_repo.llm_call_to_dict(got[0])
    assert d["task_id"] == "llm-4"
    assert d["kind"] == "ocr"
    assert d["status_code"] == 200
    assert d["elapsed_ms"] == 50
    assert d["response"] == {"ok": True}
    assert "created_at" in d and isinstance(d["created_at"], str)


def test_external_task_metadata_roundtrip_allows_duplicate_document_no(db_isolated):
    db_repo.create_task(
        "external-1",
        "compare",
        document_no="BILL-2026-001",
        external_request=True,
    )
    db_repo.create_task(
        "external-2",
        "compare",
        document_no="BILL-2026-001",
        external_request=True,
    )

    first = db_repo.get_task("external-1")
    second = db_repo.get_task("external-2")
    assert first is not None and second is not None
    assert first.document_no == second.document_no == "BILL-2026-001"
    assert first.external_request is True
    serialized = db_repo.to_dict(first)
    assert serialized["document_no"] == "BILL-2026-001"
    assert serialized["external_request"] is True
