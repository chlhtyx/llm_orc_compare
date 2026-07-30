"""外部接口入站请求审计记录测试。

覆盖 /api/v1/external/* 的提交/查询/图片请求,以及 401 鉴权失败、413/422 校验
失败等无 task_id 的场景。审计中间件用 asyncio.create_task 异步落库,测试通过
轮询 repository 等待记录出现(测试库事务在独立 session,需提交后可见)。
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import time

import pymupdf
import pytest
from docx import Document

from document_comparison.config import settings
from document_comparison.db import repository as db_repo
from document_comparison.external_api import render_external_highlight_images
from document_comparison.models import Diff, DiffSegment, PageMeta, PageRegion, TamperReport


def _docx_bytes() -> io.BytesIO:
    document = Document()
    document.add_paragraph("第一条 原始内容")
    output = io.BytesIO()
    document.save(output)
    output.seek(0)
    return output


def _pdf_bytes(page_count: int = 1) -> io.BytesIO:
    output = io.BytesIO()
    doc = pymupdf.open()
    for index in range(page_count):
        page = doc.new_page(width=300, height=400)
        page.insert_text((30, 50), f"page {index + 1}")
    doc.save(output)
    doc.close()
    output.seek(0)
    return output


def _changed_report(page_count: int = 1) -> TamperReport:
    return TamperReport(
        source="source.docx",
        target="target.pdf",
        overall_risk="changed",
        change_status="changed",
        summary={"status_counts": {"modified": 1}},
        diffs=[
            Diff(
                alignment_id="al1",
                status="modified",
                verdict="changed",
                number="第一条",
                title="付款",
                segments=[
                    DiffSegment(op="equal", text="金额"),
                    DiffSegment(op="delete", text="100"),
                    DiffSegment(op="insert", text="900"),
                    DiffSegment(op="equal", text="元"),
                ],
                page_regions=[PageRegion(page_index=0, bbox=[0.1, 0.1, 0.6, 0.2])],
            )
        ],
        page_meta=[
            PageMeta(
                page_index=i, width_px=300, height_px=400, pdf_width_pt=300, pdf_height_pt=400
            )
            for i in range(page_count)
        ],
    )


@pytest.fixture()
def external_client(db_isolated, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from document_comparison.api.app import create_app, task_manager

    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    task_manager._tasks.clear()
    client = TestClient(create_app())
    monkeypatch.setattr(settings, "external_api_key", "external-test-key")
    monkeypatch.setattr(settings, "external_public_base_url", "https://dc.example.test")
    monkeypatch.setattr(settings, "external_max_upload_mb", 50)
    monkeypatch.setattr(settings, "external_ocr_backend", "paddleocr")
    monkeypatch.setattr(settings, "external_enable_llm_judge", False)
    monkeypatch.setattr(settings, "external_enable_risk_assessment", False)
    yield client
    task_manager._tasks.clear()


def _wait_for_call_count(predicate, *, timeout: float = 2.0, interval: float = 0.02):
    """轮询直到 predicate() 通过或超时。

    审计中间件用 asyncio.create_task 异步落库,响应返回时任务可能尚未完成。
    TestClient 请求结束后 anyio portal 会排空已注册的任务,但为确保跨平台稳定,
    这里额外轮询一小段时间(总上限 2s)。
    """
    deadline = time.perf_counter() + timeout
    last_exc: Exception | None = None
    while time.perf_counter() < deadline:
        try:
            if predicate():
                return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
        time.sleep(interval)
    if last_exc:
        raise AssertionError(f"predicate never satisfied within {timeout}s; last error: {last_exc}")
    raise AssertionError(f"predicate never satisfied within {timeout}s")


def _all_calls() -> list:
    records, _ = db_repo.list_external_calls(limit=500)
    return records


def test_submit_success_records_audit_with_task_and_document(external_client, monkeypatch):
    from document_comparison.api.app import task_manager

    async def _no_run(*_args, **_kwargs):
        return None

    monkeypatch.setattr(task_manager, "run", _no_run)
    response = external_client.post(
        "/api/v1/external/contractCompare",
        headers={"X-API-Key": "external-test-key"},
        data={
            "document_no": "BILL-AUDIT-1",
            "callback_url": "http://internal/callback",
        },
        files={
            "source": ("source.docx", _docx_bytes(), "application/octet-stream"),
            "target": ("target.pdf", _pdf_bytes(), "application/pdf"),
        },
    )
    assert response.status_code == 202, response.json()
    task_id = response.json()["task_id"]
    expected_fp = hashlib.sha256(b"external-test-key").hexdigest()

    _wait_for_call_count(
        lambda: any(c.task_id == task_id and c.endpoint == "contractCompare.submit" for c in _all_calls())
    )
    rec = next(c for c in _all_calls() if c.task_id == task_id)
    assert rec.method == "POST"
    assert rec.status_code == 202
    assert rec.document_no == "BILL-AUDIT-1"
    assert rec.api_key_sha256 == expected_fp
    assert rec.error is None
    assert rec.elapsed_ms is not None and rec.elapsed_ms >= 0
    # 响应头携带 request_id,与审计记录一致
    assert response.headers.get("X-Request-Id") == rec.request_id


def test_result_query_records_audit(external_client, monkeypatch, tmp_path):
    from document_comparison.api.app import task_manager

    monkeypatch.setattr(settings, "external_image_dpi", 72)
    report = _changed_report(page_count=1)
    pdf_path = tmp_path / "q-target.pdf"
    pdf_path.write_bytes(_pdf_bytes().getvalue())
    task_id = task_manager.create("compare", document_no="BILL-Q", external_request=True)
    task = task_manager.get(task_id)
    assert task is not None
    task.report = report
    task.info.status = "done"
    render_external_highlight_images(task_id, pdf_path, report)

    response = external_client.get(
        f"/api/v1/external/contractCompare/{task_id}",
        headers={"X-API-Key": "external-test-key"},
    )
    assert response.status_code == 200
    _wait_for_call_count(
        lambda: any(
            c.task_id == task_id and c.endpoint == "contractCompare.result" for c in _all_calls()
        )
    )
    rec = next(c for c in _all_calls() if c.task_id == task_id and c.endpoint == "contractCompare.result")
    assert rec.method == "GET"
    assert rec.status_code == 200
    assert rec.error is None


def test_image_request_records_audit(external_client, monkeypatch, tmp_path):
    from document_comparison.api.app import task_manager

    monkeypatch.setattr(settings, "external_image_dpi", 72)
    report = _changed_report(page_count=1)
    pdf_path = tmp_path / "img-target.pdf"
    pdf_path.write_bytes(_pdf_bytes().getvalue())
    task_id = task_manager.create("compare", document_no="BILL-IMG", external_request=True)
    task = task_manager.get(task_id)
    assert task is not None
    task.report = report
    task.info.status = "done"
    render_external_highlight_images(task_id, pdf_path, report)

    img = external_client.get(
        f"/api/v1/external/contractCompare/{task_id}/images/1",
        headers={"X-API-Key": "external-test-key"},
    )
    assert img.status_code == 200
    _wait_for_call_count(
        lambda: any(
            c.task_id == task_id and c.endpoint == "contractCompare.image" for c in _all_calls()
        )
    )
    rec = next(c for c in _all_calls() if c.task_id == task_id and c.endpoint == "contractCompare.image")
    assert rec.status_code == 200


def test_auth_failure_401_records_audit_without_task_id(external_client):
    """401 鉴权失败:端点未执行,task_id 为 None,但审计仍记录。"""
    external_client.get(
        "/api/v1/external/contractCompare/nonexistent",
        headers={"X-API-Key": "wrong-key"},
    )
    wrong_fp = hashlib.sha256(b"wrong-key").hexdigest()

    def _has_401() -> bool:
        return any(
            c.status_code == 401
            and c.task_id is None
            and c.api_key_sha256 == wrong_fp
            for c in _all_calls()
        )

    _wait_for_call_count(_has_401)
    rec = next(c for c in _all_calls() if c.status_code == 401 and c.api_key_sha256 == wrong_fp)
    assert rec.task_id is None
    assert rec.document_no is None
    assert rec.error == "invalid external API key"
    assert rec.api_key_sha256 == wrong_fp


def test_413_upload_limit_records_audit(external_client, monkeypatch):
    monkeypatch.setattr(settings, "external_max_upload_mb", 1)
    response = external_client.post(
        "/api/v1/external/contractCompare",
        headers={"X-API-Key": "external-test-key"},
        data={
            "document_no": "BILL-BIG",
            "callback_url": "http://internal/callback",
        },
        files={
            "source": ("source.docx", io.BytesIO(b"x" * (1024 * 1024 + 1)), "application/octet-stream"),
            "target": ("target.pdf", _pdf_bytes(), "application/pdf"),
        },
    )
    assert response.status_code == 413
    _wait_for_call_count(lambda: any(c.status_code == 413 for c in _all_calls()))
    rec = next(c for c in _all_calls() if c.status_code == 413)
    assert rec.error == "payload too large"
    # 413 发生在 task 创建之前
    assert rec.task_id is None


def test_per_task_endpoint_and_to_dict(external_client, monkeypatch):
    from document_comparison.api.app import task_manager

    async def _no_run(*_args, **_kwargs):
        return None

    monkeypatch.setattr(task_manager, "run", _no_run)
    response = external_client.post(
        "/api/v1/external/contractCompare",
        headers={"X-API-Key": "external-test-key"},
        data={
            "document_no": "BILL-EP",
            "callback_url": "http://internal/callback",
        },
        files={
            "source": ("source.docx", _docx_bytes(), "application/octet-stream"),
            "target": ("target.pdf", _pdf_bytes(), "application/pdf"),
        },
    )
    task_id = response.json()["task_id"]
    _wait_for_call_count(
        lambda: any(c.task_id == task_id for c in _all_calls())
    )

    # 通过 API 端点查询该任务的调用记录
    api_resp = external_client.get(f"/api/v1/tasks/{task_id}/external-calls")
    assert api_resp.status_code == 200
    items = api_resp.json()["items"]
    assert items
    item = items[0]
    assert item["task_id"] == task_id
    assert item["endpoint"] == "contractCompare.submit"
    assert item["status_code"] == 202
    assert item["document_no"] == "BILL-EP"
    # to_dict 序列化字段齐全
    for key in ("id", "method", "client_ip", "api_key_sha256", "elapsed_ms", "request_id", "created_at"):
        assert key in item


def test_global_list_endpoint_supports_filters(external_client, monkeypatch):
    from document_comparison.api.app import task_manager

    async def _no_run(*_args, **_kwargs):
        return None

    monkeypatch.setattr(task_manager, "run", _no_run)
    external_client.post(
        "/api/v1/external/contractCompare",
        headers={"X-API-Key": "external-test-key"},
        data={
            "document_no": "BILL-LIST",
            "callback_url": "http://internal/callback",
        },
        files={
            "source": ("source.docx", _docx_bytes(), "application/octet-stream"),
            "target": ("target.pdf", _pdf_bytes(), "application/pdf"),
        },
    )
    _wait_for_call_count(lambda: any(c.document_no == "BILL-LIST" for c in _all_calls()))

    # 按 endpoint 过滤
    resp = external_client.get("/api/v1/external-calls?endpoint=contractCompare.submit")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    assert all(it["endpoint"] == "contractCompare.submit" for it in body["items"])

    # 按 document_no 过滤
    resp2 = external_client.get("/api/v1/external-calls?document_no=BILL-LIST")
    assert resp2.status_code == 200
    assert resp2.json()["total"] >= 1

    # 按 status_code 过滤(401)
    resp3 = external_client.get("/api/v1/external-calls?status_code=999")
    assert resp3.json()["total"] == 0


def test_audit_record_survives_task_deletion(external_client, monkeypatch):
    """task_id 无外键:即使 task_records 行被删,审计记录保留。"""
    from sqlalchemy import delete

    from document_comparison.api.app import task_manager
    from document_comparison.db.engine import session_scope
    from document_comparison.db.models import TaskRecord

    async def _no_run(*_args, **_kwargs):
        return None

    monkeypatch.setattr(task_manager, "run", _no_run)
    response = external_client.post(
        "/api/v1/external/contractCompare",
        headers={"X-API-Key": "external-test-key"},
        data={
            "document_no": "BILL-DEL",
            "callback_url": "http://internal/callback",
        },
        files={
            "source": ("source.docx", _docx_bytes(), "application/octet-stream"),
            "target": ("target.pdf", _pdf_bytes(), "application/pdf"),
        },
    )
    task_id = response.json()["task_id"]
    _wait_for_call_count(lambda: any(c.task_id == task_id for c in _all_calls()))

    # 物理删除 task_records 行(模拟 task 删除)
    with session_scope() as s:
        s.execute(delete(TaskRecord).where(TaskRecord.task_id == task_id))

    # 审计记录仍在(无 CASCADE)
    records, total = db_repo.list_external_calls(document_no="BILL-DEL")
    assert total >= 1
    assert any(r.task_id == task_id for r in records)


def test_statement_submit_and_result_record_audit(external_client, monkeypatch):
    """金额统计对外 API:提交与查询均落库审计,endpoint 标签为 amountStat.*。"""
    from document_comparison.api.app import task_manager

    async def _no_run(*_args, **_kwargs):
        return None

    monkeypatch.setattr(task_manager, "run_statement", _no_run)
    submit = external_client.post(
        "/api/v1/external/amountStat",
        headers={"X-API-Key": "external-test-key"},
        data={
            "document_no": "STMT-AUDIT-1",
            "callback_url": "http://internal/callback",
        },
        files=[
            ("target", ("a.pdf", _pdf_bytes(), "application/pdf")),
        ],
    )
    assert submit.status_code == 202, submit.json()
    task_id = submit.json()["task_id"]

    _wait_for_call_count(
        lambda: any(
            c.task_id == task_id and c.endpoint == "amountStat.submit"
            for c in _all_calls()
        )
    )
    rec = next(
        c for c in _all_calls()
        if c.task_id == task_id and c.endpoint == "amountStat.submit"
    )
    assert rec.method == "POST"
    assert rec.status_code == 202
    assert rec.document_no == "STMT-AUDIT-1"
    assert rec.error is None
    assert submit.headers.get("X-Request-Id") == rec.request_id

    # 查询端点
    query = external_client.get(
        f"/api/v1/external/amountStat/{task_id}",
        headers={"X-API-Key": "external-test-key"},
    )
    assert query.status_code == 200
    _wait_for_call_count(
        lambda: any(
            c.task_id == task_id and c.endpoint == "amountStat.result"
            for c in _all_calls()
        )
    )
    rec2 = next(
        c for c in _all_calls()
        if c.task_id == task_id and c.endpoint == "amountStat.result"
    )
    assert rec2.method == "GET"
    assert rec2.status_code == 200
