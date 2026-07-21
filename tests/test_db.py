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
