"""外部合同比对 API、结果文本与高亮图片测试。"""
from __future__ import annotations

import io
import json

import pymupdf
import pytest
from docx import Document

from document_comparison.config import settings
from document_comparison.external_api import (
    build_external_result,
    build_external_statement_result,
    build_result_text,
    external_images_dir,
    render_external_highlight_images,
    require_external_api_key,
    validate_callback_url,
)
from document_comparison.models import (
    Diff,
    DiffSegment,
    PageMeta,
    PageRegion,
    StatementFileSummary,
    StatementSummaryReport,
    TamperReport,
)


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


def _changed_report(page_count: int = 2) -> TamperReport:
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
                page_regions=[
                    PageRegion(page_index=0, bbox=[0.1, 0.1, 0.6, 0.2])
                ],
            )
        ],
        page_meta=[
            PageMeta(
                page_index=index,
                width_px=300,
                height_px=400,
                pdf_width_pt=300,
                pdf_height_pt=400,
            )
            for index in range(page_count)
        ],
    )


def _with_source_highlights(report: TamperReport) -> TamperReport:
    value = report.model_copy(deep=True)
    value.source = "source.pdf"
    value.source_page_meta = list(value.page_meta)
    value.source_annotation_status = "available"
    value.diffs[0].source_page_regions = [
        PageRegion(page_index=0, bbox=[0.1, 0.1, 0.6, 0.2])
    ]
    return value


@pytest.fixture()
def external_client(db_isolated, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from document_comparison.api.app import create_app

    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    from document_comparison.api.app import task_manager

    task_manager._tasks.clear()
    client = TestClient(create_app())
    # create_app 会从 PG 应用持久化配置；测试覆盖应在初始化之后注入。
    monkeypatch.setattr(settings, "external_api_key", "external-test-key")
    monkeypatch.setattr(settings, "external_public_base_url", "https://dc.example.test")
    monkeypatch.setattr(settings, "external_max_upload_mb", 50)
    monkeypatch.setattr(settings, "external_ocr_backend", "paddleocr")
    monkeypatch.setattr(settings, "external_enable_llm_judge", False)
    monkeypatch.setattr(settings, "external_enable_llm_alignment", False)
    monkeypatch.setattr(settings, "external_enable_risk_assessment", False)
    yield client
    task_manager._tasks.clear()


def test_external_auth_missing_wrong_and_unconfigured(external_client, monkeypatch):
    # 配置了 Key:缺失/错误头 → 401
    assert external_client.get("/api/v1/external/contractCompare/missing").status_code == 401
    assert external_client.get(
        "/api/v1/external/contractCompare/missing", headers={"X-API-Key": "wrong"}
    ).status_code == 401
    # 未配置 Key:鉴权跳过,放行到业务层(查不到 task → 404,而非 401/503)
    monkeypatch.setattr(settings, "external_api_key", "")
    assert external_client.get(
        "/api/v1/external/contractCompare/missing"
    ).status_code == 404
    assert external_client.get(
        "/api/v1/external/contractCompare/missing",
        headers={"X-API-Key": "anything"},
    ).status_code == 404


def test_external_submit_echoes_document_no_and_allows_duplicates(
    external_client, monkeypatch
):
    from document_comparison.api.app import task_manager

    async def _no_run(*_args, **_kwargs):
        return None

    monkeypatch.setattr(task_manager, "run", _no_run)
    headers = {"X-API-Key": "external-test-key"}
    data = {
        "document_no": "BILL-2026-001",
        "callback_url": "http://10.0.0.8/callback",
    }

    def submit():
        return external_client.post(
            "/api/v1/external/contractCompare",
            headers=headers,
            data=data,
            files={
                "source": ("source.docx", _docx_bytes(), "application/octet-stream"),
                "target": ("target.pdf", _pdf_bytes(), "application/pdf"),
            },
        )

    first = submit()
    second = submit()
    assert first.status_code == 202, first.json()
    assert second.status_code == 202, second.json()
    assert first.json()["document_no"] == "BILL-2026-001"
    assert first.json()["task_id"] != second.json()["task_id"]
    task = task_manager.get(first.json()["task_id"])
    assert task is not None and task.external_request is True
    assert task.document_no == "BILL-2026-001"


def test_external_submit_accepts_pdf_source(external_client, monkeypatch):
    """原始合同允许 .pdf(与 .docx 并列);.txt 仍被拒。"""
    from document_comparison.api.app import task_manager

    async def _no_run(*_args, **_kwargs):
        return None

    monkeypatch.setattr(task_manager, "run", _no_run)
    headers = {"X-API-Key": "external-test-key"}

    # .pdf source 被接受
    ok = external_client.post(
        "/api/v1/external/contractCompare",
        headers=headers,
        data={"document_no": "BILL-PDF-001", "callback_url": "http://10.0.0.8/cb"},
        files={
            "source": ("source.pdf", _pdf_bytes(), "application/pdf"),
            "target": ("target.pdf", _pdf_bytes(), "application/pdf"),
        },
    )
    assert ok.status_code == 202, ok.json()
    assert ok.json()["document_no"] == "BILL-PDF-001"

    # .txt source 仍被拒
    bad = external_client.post(
        "/api/v1/external/contractCompare",
        headers=headers,
        data={"document_no": "BILL-BAD-001", "callback_url": "http://10.0.0.8/cb"},
        files={
            "source": ("source.txt", io.BytesIO(b"text"), "text/plain"),
            "target": ("target.pdf", _pdf_bytes(), "application/pdf"),
        },
    )
    assert bad.status_code == 400
    msg = bad.json()["message"]
    assert "docx" in msg and "pdf" in msg


def test_compare_page_api_test_reuses_external_artifact_flow_without_callback(
    external_client, monkeypatch
):
    from document_comparison.api.app import task_manager

    captured: dict = {}

    async def _capture_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(task_manager, "run", _capture_run)
    response = external_client.post(
        "/api/v1/compare/api-test",
        files={
            "source": ("source.docx", _docx_bytes(), "application/octet-stream"),
            "target": ("target.pdf", _pdf_bytes(), "application/pdf"),
        },
    )
    assert response.status_code == 202, response.json()
    body = response.json()
    assert body["document_no"].startswith("API-TEST-")
    task = task_manager.get(body["task_id"])
    assert task is not None
    assert task.external_request is True
    assert task.callback_url is None
    assert captured["kwargs"]["enable_llm_judge"] is False
    assert captured["kwargs"]["enable_llm_alignment"] is False
    assert captured["kwargs"]["ocr_backend"] == "paddleocr"
    assert captured["kwargs"]["enable_risk_assessment"] is False


def test_api_comparison_options_apply_to_test_and_external_tasks(
    external_client, monkeypatch
):
    from document_comparison.api.app import task_manager

    captured: list[dict] = []

    async def _capture_run(*_args, **kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(task_manager, "run", _capture_run)
    monkeypatch.setattr(settings, "external_ocr_backend", "llm")
    monkeypatch.setattr(settings, "external_enable_llm_judge", True)
    monkeypatch.setattr(settings, "external_enable_llm_alignment", True)
    monkeypatch.setattr(settings, "external_enable_risk_assessment", True)
    monkeypatch.setattr(settings, "external_truncate_to_original_pages", True)

    test_response = external_client.post(
        "/api/v1/compare/api-test",
        files={
            "source": ("source.docx", _docx_bytes(), "application/octet-stream"),
            "target": ("target.pdf", _pdf_bytes(), "application/pdf"),
        },
    )
    external_response = external_client.post(
        "/api/v1/external/contractCompare",
        headers={"X-API-Key": "external-test-key"},
        data={
            "document_no": "BILL-OPTIONS",
            "callback_url": "http://internal/callback",
            "original_page_count": "3",
        },
        files={
            "source": ("source.docx", _docx_bytes(), "application/octet-stream"),
            "target": ("target.pdf", _pdf_bytes(), "application/pdf"),
        },
    )

    assert test_response.status_code == 202
    assert external_response.status_code == 202
    assert len(captured) == 2
    assert all(item["ocr_backend"] == "llm" for item in captured)
    assert all(item["enable_llm_judge"] is True for item in captured)
    assert all(item["enable_llm_alignment"] is True for item in captured)
    assert all(item["enable_risk_assessment"] is True for item in captured)
    assert captured[0].get("original_page_count") is None
    assert captured[1]["truncate_to_original_pages"] is True
    assert captured[1]["original_page_count"] == 3


def test_compare_page_api_test_requires_complete_external_config(external_client, monkeypatch):
    monkeypatch.setattr(settings, "external_api_key", "")
    response = external_client.post(
        "/api/v1/compare/api-test",
        files={
            "source": ("source.docx", _docx_bytes(), "application/octet-stream"),
            "target": ("target.pdf", _pdf_bytes(), "application/pdf"),
        },
    )
    assert response.status_code == 400
    assert "先保存" in response.json()["message"]


@pytest.mark.parametrize(
    ("data_override", "expected"),
    [
        ({"document_no": "   "}, "document_no"),
        ({"callback_url": "ftp://internal/callback"}, "HTTP/HTTPS"),
    ],
)
def test_external_submit_validation(external_client, data_override, expected):
    data = {
        "document_no": "BILL-1",
        "callback_url": "http://internal/callback",
        **data_override,
    }
    response = external_client.post(
        "/api/v1/external/contractCompare",
        headers={"X-API-Key": "external-test-key"},
        data=data,
        files={
            "source": ("source.docx", _docx_bytes(), "application/octet-stream"),
            "target": ("target.pdf", _pdf_bytes(), "application/pdf"),
        },
    )
    assert response.status_code == 400
    assert expected in response.json()["message"]


def test_external_upload_limit(external_client, monkeypatch):
    monkeypatch.setattr(settings, "external_max_upload_mb", 1)
    response = external_client.post(
        "/api/v1/external/contractCompare",
        headers={"X-API-Key": "external-test-key"},
        data={
            "document_no": "BILL-1",
            "callback_url": "http://internal/callback",
        },
        files={
            "source": ("source.docx", io.BytesIO(b"x" * (1024 * 1024 + 1)), "application/octet-stream"),
            "target": ("target.pdf", _pdf_bytes(), "application/pdf"),
        },
    )
    assert response.status_code == 413


def test_external_sync_returns_inline_result_without_callback_url(
    external_client, monkeypatch, tmp_path
):
    """sync=true:阻塞至完成,响应体内直接返回完整结果,且 callback_url 非必填。"""
    from document_comparison.api.app import task_manager

    report = _changed_report(page_count=1)
    pdf_path = tmp_path / "sync-target.pdf"
    pdf_path.write_bytes(_pdf_bytes().getvalue())

    async def _fake_run(task_id, *_args, **_kwargs):
        task = task_manager.get(task_id)
        assert task is not None
        task.report = report
        task.info.status = "done"
        # 同步模式下回调仅在 callback_url 非空时触发;这里未传 callback_url,
        # 模拟 run 内部 _fire_callback 短路返回的行为(不实际发请求)。
        return None

    monkeypatch.setattr(task_manager, "run", _fake_run)
    response = external_client.post(
        "/api/v1/external/contractCompare",
        headers={"X-API-Key": "external-test-key"},
        data={"document_no": "BILL-SYNC", "sync": "true"},
        files={
            "source": ("source.docx", _docx_bytes(), "application/octet-stream"),
            "target": ("target.pdf", _pdf_bytes(), "application/pdf"),
        },
    )
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["status"] == "done"
    assert body["document_no"] == "BILL-SYNC"
    assert body["change_status"] == "changed"
    assert "result_text" in body
    assert "highlight_images" in body
    assert "result_url" in body
    task = task_manager.get(body["task_id"])
    assert task is not None and task.callback_url is None


def test_external_sync_sets_task_sync_mode_for_background_callback(
    external_client, monkeypatch
):
    """sync=true 提交应把 task.sync_mode 置 True,使 run 内部 webhook 后台发送。

    不直接测 _fire_or_schedule_callback(那在 test_tasks.py 覆盖);
    此处只验证端点 → create(sync_mode=True) 的接线,防止回归。
    """
    from document_comparison.api.app import task_manager

    async def _fake_run(*_args, **_kwargs):
        return None

    monkeypatch.setattr(task_manager, "run", _fake_run)
    response = external_client.post(
        "/api/v1/external/contractCompare",
        headers={"X-API-Key": "external-test-key"},
        data={"document_no": "BILL-SYNC-MODE", "sync": "true"},
        files={
            "source": ("source.docx", _docx_bytes(), "application/octet-stream"),
            "target": ("target.pdf", _pdf_bytes(), "application/pdf"),
        },
    )
    assert response.status_code == 200, response.json()
    task = task_manager.get(response.json()["task_id"])
    assert task is not None
    assert task.sync_mode is True


def test_external_async_still_requires_callback_url_when_sync_omitted(external_client):
    """sync 缺省(异步)时 callback_url 仍必填。"""
    response = external_client.post(
        "/api/v1/external/contractCompare",
        headers={"X-API-Key": "external-test-key"},
        data={"document_no": "BILL-ASYNC"},
        files={
            "source": ("source.docx", _docx_bytes(), "application/octet-stream"),
            "target": ("target.pdf", _pdf_bytes(), "application/pdf"),
        },
    )
    assert response.status_code == 400
    assert "callback_url" in response.json()["message"]


def test_redeliver_callback_reposts_persisted_payload(external_client, monkeypatch):
    """重新回调端点:从 task_records 读 callback_url + payload 重投并回写状态。"""
    from fastapi.testclient import TestClient

    from document_comparison.api.app import create_app
    from document_comparison.db import repository as db_repo

    task_id = "rd1" + "0" * 12  # 16 hex-ish
    db_repo.create_task(
        task_id, "compare",
        source_name="s.docx", target_names=["t.pdf"],
        document_no="BILL-RD", external_request=True,
        callback_url="http://internal/hook",
    )
    db_repo.save_compare_report(task_id, _changed_report(page_count=1))
    db_repo.update_task_status(task_id, "done")
    db_repo.save_callback_payload(task_id, {"event_type": "contract.compare.completed", "document_no": "BILL-RD"})

    delivered: dict = {}

    async def _deliver(url, raw, event_id, **_kwargs):
        delivered.update(url=url, body=__import__("json").loads(raw), event_id=event_id)
        return {"success": True, "http_status": 200, "error": None}

    import document_comparison.webhook as webhook_mod

    monkeypatch.setattr(webhook_mod, "deliver", _deliver)
    client = TestClient(create_app())
    resp = client.post(f"/api/v1/tasks/{task_id}/redeliver-callback")
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["callback_status"] == "success"
    assert body["callback_http_status"] == 200
    # payload 还原:重投的事件体含首次交付的业务字段 + 新 event_id
    assert delivered["body"]["event_type"] == "contract.compare.completed"
    assert delivered["body"]["document_no"] == "BILL-RD"
    assert delivered["event_id"] == delivered["body"]["event_id"]
    # 回写落库
    rec = db_repo.get_task(task_id)
    assert rec is not None and rec.callback_status == "success"


def test_redeliver_callback_rejects_task_without_callback_url(external_client):
    """未配置回调地址的任务不能重新推送(400)。"""
    from document_comparison.db import repository as db_repo

    task_id = "rd2" + "0" * 12
    db_repo.create_task(task_id, "compare", target_names=["t.pdf"])
    db_repo.update_task_status(task_id, "done")
    resp = external_client.post(f"/api/v1/tasks/{task_id}/redeliver-callback")
    assert resp.status_code == 400


def test_external_sync_failure_returns_failed_status(external_client, monkeypatch):
    """同步模式下比对失败:响应仍 200,体内 status=failed + error。"""
    from document_comparison.api.app import task_manager

    async def _failing_run(task_id, *_args, **_kwargs):
        task = task_manager.get(task_id)
        assert task is not None
        task.info.status = "failed"
        task.info.error = "OCR 解析失败"
        return None

    monkeypatch.setattr(task_manager, "run", _failing_run)
    response = external_client.post(
        "/api/v1/external/contractCompare",
        headers={"X-API-Key": "external-test-key"},
        data={"document_no": "BILL-FAIL", "sync": "true"},
        files={
            "source": ("source.docx", _docx_bytes(), "application/octet-stream"),
            "target": ("target.pdf", _pdf_bytes(), "application/pdf"),
        },
    )
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"] == "OCR 解析失败"


def test_external_sync_accepts_callback_url_alongside(external_client, monkeypatch):
    """sync=true 同时传 callback_url 也应接受(回调照常触发,无害)。"""
    from document_comparison.api.app import task_manager

    async def _fake_run(*_args, **_kwargs):
        return None

    monkeypatch.setattr(task_manager, "run", _fake_run)
    response = external_client.post(
        "/api/v1/external/contractCompare",
        headers={"X-API-Key": "external-test-key"},
        data={
            "document_no": "BILL-SYNC-CB",
            "callback_url": "http://10.0.0.8/callback",
            "sync": "true",
        },
        files={
            "source": ("source.docx", _docx_bytes(), "application/octet-stream"),
            "target": ("target.pdf", _pdf_bytes(), "application/pdf"),
        },
    )
    assert response.status_code == 200, response.json()
    task = task_manager.get(response.json()["task_id"])
    assert task is not None
    assert task.callback_url == "http://10.0.0.8/callback"


def test_external_query_and_image_download(external_client, monkeypatch, tmp_path):
    from document_comparison.api.app import task_manager

    monkeypatch.setattr(settings, "external_image_dpi", 72)
    report = _changed_report(page_count=2)
    pdf_path = tmp_path / "query-target.pdf"
    pdf_path.write_bytes(_pdf_bytes(page_count=2).getvalue())
    task_id = task_manager.create(
        "compare",
        document_no="BILL-QUERY",
        external_request=True,
    )
    task = task_manager.get(task_id)
    assert task is not None
    task.report = report
    task.info.status = "done"
    render_external_highlight_images(task_id, pdf_path, report)

    headers = {"X-API-Key": "external-test-key"}
    response = external_client.get(
        f"/api/v1/external/contractCompare/{task_id}", headers=headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["document_no"] == "BILL-QUERY"
    assert body["change_status"] == "changed"
    assert len(body["highlight_images"]) == 2

    image = external_client.get(
        f"/api/v1/external/contractCompare/{task_id}/images/1", headers=headers
    )
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.content.startswith(b"\x89PNG")


def test_compare_page_api_test_query_and_image_preview(external_client, monkeypatch, tmp_path):
    from document_comparison.api.app import task_manager

    monkeypatch.setattr(settings, "external_image_dpi", 72)
    report = _changed_report(page_count=1)
    pdf_path = tmp_path / "admin-test-target.pdf"
    pdf_path.write_bytes(_pdf_bytes().getvalue())
    task_id = task_manager.create(
        "compare",
        document_no="API-TEST-QUERY",
        external_request=True,
    )
    task = task_manager.get(task_id)
    assert task is not None
    task.report = report
    task.info.status = "done"
    render_external_highlight_images(task_id, pdf_path, report)

    response = external_client.get(f"/api/v1/compare/api-test/{task_id}")
    assert response.status_code == 200
    assert response.json()["change_status"] == "changed"

    image = external_client.get(
        f"/api/v1/compare/api-test/{task_id}/images/1"
    )
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"


def test_result_text_and_all_page_images(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    monkeypatch.setattr(settings, "external_image_dpi", 72)
    monkeypatch.setattr(settings, "external_public_base_url", "https://dc.example.test/")
    report = _changed_report(page_count=2)
    pdf_path = tmp_path / "target.pdf"
    pdf_path.write_bytes(_pdf_bytes(page_count=2).getvalue())

    images = render_external_highlight_images("task-1", pdf_path, report)
    assert [path.name for path in images] == ["page-0001.png", "page-0002.png"]
    assert all(path.stat().st_size > 0 for path in images)

    text = build_result_text("BILL-1", report)
    assert "结论：发现确认内容变化" in text
    assert "原始合同：金额100元" in text
    assert "回收件：金额900元" in text

    result = build_external_result("task-1", "BILL-1", report)
    assert result["highlight_images"] == [
        "https://dc.example.test/api/v1/external/contractCompare/task-1/images/1",
        "https://dc.example.test/api/v1/external/contractCompare/task-1/images/2",
    ]


def test_external_renders_and_exposes_source_highlight_images(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    monkeypatch.setattr(settings, "external_image_dpi", 72)
    monkeypatch.setattr(settings, "external_public_base_url", "https://dc.example.test")
    report = _with_source_highlights(_changed_report(page_count=1))
    target_pdf = tmp_path / "target.pdf"
    source_pdf = tmp_path / "source.pdf"
    target_pdf.write_bytes(_pdf_bytes().getvalue())
    source_pdf.write_bytes(_pdf_bytes().getvalue())

    target_images = render_external_highlight_images(
        "task-source", target_pdf, report, source_pdf,
    )

    assert len(target_images) == 1
    from document_comparison.external_api import external_image_path
    assert external_image_path("task-source", 1, side="source").is_file()
    result = build_external_result("task-source", "BILL-SOURCE", report)
    assert result["source_highlight_images"] == [
        "https://dc.example.test/api/v1/external/contractCompare/task-source/source-images/1"
    ]


def test_render_replaces_stale_highlight_pngs(monkeypatch, tmp_path):
    """整侧重渲时旧的 page-*.png 被整体替换,不残留多余页。"""
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    monkeypatch.setattr(settings, "external_image_dpi", 72)
    monkeypatch.setattr(settings, "external_public_base_url", "https://dc.example.test")
    report = _changed_report(page_count=2)
    pdf_path = tmp_path / "target.pdf"
    pdf_path.write_bytes(_pdf_bytes(page_count=2).getvalue())

    img_dir = external_images_dir("task-stale")
    img_dir.mkdir(parents=True)
    (img_dir / "page-0001.png").write_bytes(b"stale-page1")
    (img_dir / "page-0009.png").write_bytes(b"stale-extra")

    images = render_external_highlight_images("task-stale", pdf_path, report)

    assert [path.name for path in images] == ["page-0001.png", "page-0002.png"]
    # 多余的旧页被清掉,正式目录只剩当前文档的页集
    assert sorted(p.name for p in img_dir.glob("page-*.png")) == [
        "page-0001.png", "page-0002.png",
    ]
    # 旧占位被真实渲染覆盖
    assert (img_dir / "page-0001.png").read_bytes() != b"stale-page1"
    assert (img_dir / "page-0002.png").stat().st_size > 0


def test_render_side_failure_leaves_no_partial_pngs(monkeypatch, tmp_path):
    """渲染中途失败时,正式目录不留残缺页集(报告不会嵌入不完整的一侧)。"""
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    monkeypatch.setattr(settings, "external_image_dpi", 72)
    monkeypatch.setattr(settings, "external_public_base_url", "https://dc.example.test")
    report = _changed_report(page_count=2)
    pdf_path = tmp_path / "target.pdf"
    pdf_path.write_bytes(_pdf_bytes(page_count=2).getvalue())

    original = pymupdf.Page.get_pixmap
    calls = {"count": 0}

    def flaky_get_pixmap(self, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:  # 第 2 页渲染时失败,第 1 页已画进临时目录
            raise RuntimeError("pixmap boom")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pymupdf.Page, "get_pixmap", flaky_get_pixmap)
    with pytest.raises(RuntimeError, match="pixmap boom"):
        render_external_highlight_images("task-partial", pdf_path, report)

    img_dir = external_images_dir("task-partial")
    assert list(img_dir.glob("page-*.png")) == []


def test_missing_location_does_not_claim_highlight(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    monkeypatch.setattr(settings, "external_image_dpi", 72)
    monkeypatch.setattr(settings, "external_public_base_url", "https://dc.example.test")
    report = _changed_report(page_count=1).model_copy(deep=True)
    report.location_status = "missing"
    report.diffs[0].page_regions = []
    pdf_path = tmp_path / "target.pdf"
    pdf_path.write_bytes(_pdf_bytes().getvalue())

    render_external_highlight_images("task-2", pdf_path, report)
    result = build_external_result("task-2", "BILL-2", report)
    assert "location_status" not in result
    assert "recognition_status" not in result
    assert "summary" not in result
    assert "高亮定位：缺失" in result["result_text"]


def test_callback_url_accepts_internal_http_and_rejects_credentials():
    assert validate_callback_url("http://10.0.0.8/hook") == "http://10.0.0.8/hook"
    with pytest.raises(ValueError, match="用户名"):
        validate_callback_url("http://user:pass@10.0.0.8/hook")


async def test_external_dependency_rejects_invalid_runtime_config(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(settings, "external_api_key", "key")
    monkeypatch.setattr(settings, "external_public_base_url", "not-a-url")
    with pytest.raises(HTTPException) as exc_info:
        await require_external_api_key("key")
    assert exc_info.value.status_code == 503


async def test_external_dependency_skips_auth_when_key_unconfigured(monkeypatch):
    # 未配置 Key:无论是否带 X-API-Key 都直接放行(返回 None,不抛)
    monkeypatch.setattr(settings, "external_api_key", "")
    monkeypatch.setattr(settings, "external_public_base_url", "https://dc.example.test")
    assert await require_external_api_key(None) is None
    assert await require_external_api_key("anything") is None


# ---------- source/target 兼容文件与 URL(source_url / target_url)----------


def _patch_download(monkeypatch, *, source_name="source.docx", target_name="target.pdf"):
    """让 app.download_to_upload 返回临时文件 + 推断文件名,避免真实网络。

    注意:`document_comparison.api.app` 这个名字会被 `api/__init__.py` 里的
    `from .app import app` 同名 FastAPI 实例遮蔽,所以必须从 sys.modules 取真正的模块对象,
    再 patch 其上的 `download_to_upload`(端点按模块级名字引用)。
    """
    import sys

    app_module = sys.modules["document_comparison.api.app"]
    calls: dict = {"count": 0, "urls": []}

    async def _fake(url, *, max_bytes, role):
        calls["count"] += 1
        calls["urls"].append(url)
        settings.ensure_dirs()
        name = source_name if role == "source" else target_name
        temp = settings.uploads_dir / f".test-pending-{role}-{calls['count']}-{name}"
        if role == "source":
            temp.write_bytes(_docx_bytes().getvalue())
        else:
            temp.write_bytes(_pdf_bytes().getvalue())
        return temp, name

    monkeypatch.setattr(app_module, "download_to_upload", _fake)
    return calls


def test_external_submit_accepts_source_and_target_urls(external_client, monkeypatch):
    from document_comparison.api.app import task_manager

    captured: list[dict] = []

    async def _capture_run(*args, **kwargs):
        captured.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(task_manager, "run", _capture_run)
    calls = _patch_download(monkeypatch)

    response = external_client.post(
        "/api/v1/external/contractCompare",
        headers={"X-API-Key": "external-test-key"},
        data={
            "document_no": "BILL-URL-1",
            "source_url": "http://files.example.test/source.docx",
            "target_url": "http://files.example.test/target.pdf",
            "callback_url": "http://internal/callback",
        },
    )
    assert response.status_code == 202, response.json()
    assert calls["count"] == 2
    # task_manager.run 接到的是 finalize 后的最终本地路径(非临时文件)
    word_path = captured[0]["args"][1]
    pdf_path = captured[0]["args"][2]
    assert word_path.endswith("source.docx")
    assert pdf_path.endswith("target.pdf")


def test_external_submit_rejects_source_and_source_url_both(external_client):
    response = external_client.post(
        "/api/v1/external/contractCompare",
        headers={"X-API-Key": "external-test-key"},
        data={
            "document_no": "BILL-URL-2",
            "source_url": "http://files.example.test/source.docx",
            "target_url": "http://files.example.test/target.pdf",
            "callback_url": "http://internal/callback",
        },
        files={
            "source": ("source.docx", _docx_bytes(), "application/octet-stream"),
            "target": ("target.pdf", _pdf_bytes(), "application/pdf"),
        },
    )
    assert response.status_code == 400
    assert "二选一" in response.json()["message"]


def test_external_submit_rejects_missing_source_and_source_url(external_client):
    response = external_client.post(
        "/api/v1/external/contractCompare",
        headers={"X-API-Key": "external-test-key"},
        data={
            "document_no": "BILL-URL-3",
            "target_url": "http://files.example.test/target.pdf",
            "callback_url": "http://internal/callback",
        },
    )
    assert response.status_code == 400
    assert "source" in response.json()["message"]


def test_external_submit_mixed_file_source_and_url_target(external_client, monkeypatch):
    from document_comparison.api.app import task_manager

    captured: list[dict] = []

    async def _capture_run(*args, **kwargs):
        captured.append({"args": args})

    monkeypatch.setattr(task_manager, "run", _capture_run)
    _patch_download(monkeypatch)  # 只 target 走下载

    response = external_client.post(
        "/api/v1/external/contractCompare",
        headers={"X-API-Key": "external-test-key"},
        data={
            "document_no": "BILL-URL-4",
            "target_url": "http://files.example.test/target.pdf",
            "callback_url": "http://internal/callback",
        },
        files={
            "source": ("source.docx", _docx_bytes(), "application/octet-stream"),
        },
    )
    assert response.status_code == 202, response.json()
    # source 走 save_upload(原命名),target 走 finalize(下载推断名)
    assert captured[0]["args"][1].endswith("source.docx")
    assert captured[0]["args"][2].endswith("target.pdf")


def test_external_url_suffix_mismatch_rejected(external_client, monkeypatch):
    _patch_download(monkeypatch, source_name="source.txt")

    response = external_client.post(
        "/api/v1/external/contractCompare",
        headers={"X-API-Key": "external-test-key"},
        data={
            "document_no": "BILL-URL-5",
            "source_url": "http://files.example.test/source.txt",
            "target_url": "http://files.example.test/target.pdf",
            "callback_url": "http://internal/callback",
        },
    )
    assert response.status_code == 400
    assert ".docx" in response.json()["message"]


def test_external_url_download_failure_returns_400(external_client, monkeypatch):
    import sys

    from document_comparison.api.app import task_manager

    app_module = sys.modules["document_comparison.api.app"]

    async def _boom(url, *, max_bytes, role):
        raise ValueError(f"下载 {role} 失败:HTTP 502 (files.example.test)")

    monkeypatch.setattr(app_module, "download_to_upload", _boom)

    before = len(task_manager._tasks)
    response = external_client.post(
        "/api/v1/external/contractCompare",
        headers={"X-API-Key": "external-test-key"},
        data={
            "document_no": "BILL-URL-6",
            "source_url": "http://files.example.test/source.docx",
            "target_url": "http://files.example.test/target.pdf",
            "callback_url": "http://internal/callback",
        },
    )
    assert response.status_code == 400
    assert "下载" in response.json()["message"]
    # 失败时不应创建任务
    assert len(task_manager._tasks) == before


def test_external_url_rejects_invalid_scheme(external_client):
    response = external_client.post(
        "/api/v1/external/contractCompare",
        headers={"X-API-Key": "external-test-key"},
        data={
            "document_no": "BILL-URL-7",
            "source_url": "ftp://files.example.test/source.docx",
            "target_url": "http://files.example.test/target.pdf",
            "callback_url": "http://internal/callback",
        },
    )
    assert response.status_code == 400
    assert "HTTP/HTTPS" in response.json()["message"]


# —— 金额统计对外 API(/api/v1/external/amountStat)——


def _statement_report(grand_total: float = 100000.0) -> StatementSummaryReport:
    """构造一个简单的金额统计报告,供 done 结果断言使用。"""
    return StatementSummaryReport(
        files=[
            StatementFileSummary(
                file_index=0,
                file_name="target.pdf",
                total_amount=grand_total,
            )
        ],
        grand_total=grand_total,
        grand_totals_by_column={"金额": grand_total},
        total_files=1,
        total_tables=1,
        total_items=2,
        verdict="clean",
        reasons=[],
        column_detection_summary={"heuristic": 1, "llm": 0, "none": 0},
    )


def test_build_external_statement_result_returns_each_file_total():
    report = _statement_report(60000.0)
    report.files.append(
        StatementFileSummary(
            file_index=1,
            file_name="failed.pdf",
            total_amount=0.0,
            error="OCR 解析失败",
        )
    )
    report.grand_total = 60000.0
    report.total_files = 2

    result = build_external_statement_result("task-1", "STMT-1", report)

    assert "grand_totals_by_column" not in result
    assert result["document_type"] is None
    assert result["file_totals"] == [
        {"file_index": 0, "file_name": "target.pdf", "total_amount": 60000.0, "error": None},
        {"file_index": 1, "file_name": "failed.pdf", "total_amount": 0.0, "error": "OCR 解析失败"},
    ]
    assert "column_detection_summary" not in result
    assert "report" not in result

    result_contract = build_external_statement_result(
        "task-2", "STMT-2", report, document_type="2"
    )
    assert result_contract["document_type"] == "2"


def test_external_statement_submit_async_echoes_document_no(external_client, monkeypatch):
    from document_comparison.api.app import task_manager

    async def _no_run(*_args, **_kwargs):
        return None

    monkeypatch.setattr(task_manager, "run_statement", _no_run)
    response = external_client.post(
        "/api/v1/external/amountStat",
        headers={"X-API-Key": "external-test-key"},
        data={
            "document_no": "STMT-2026-001",
            "callback_url": "http://internal/callback",
        },
        files=[
            ("target", ("a.pdf", _pdf_bytes(), "application/pdf")),
            ("target", ("b.pdf", _pdf_bytes(), "application/pdf")),
        ],
    )
    assert response.status_code == 202, response.json()
    body = response.json()
    assert body["document_no"] == "STMT-2026-001"
    # 缺省 document_type 归一为 "1"(发票)
    assert body["document_type"] == "1"
    assert body["status"] == "pending"
    task = task_manager.get(body["task_id"])
    assert task is not None
    assert task.external_request is True
    assert task.kind == "statement"
    assert task.document_type == "1"
    assert task.sync_mode is False


def test_external_statement_submit_document_type_statement(external_client, monkeypatch):
    """document_type=2(对帐单)入库并在受理响应回显,供对比记录区分发票/对帐单。"""
    from document_comparison.api.app import task_manager

    async def _no_run(*_args, **_kwargs):
        return None

    monkeypatch.setattr(task_manager, "run_statement", _no_run)
    response = external_client.post(
        "/api/v1/external/amountStat",
        headers={"X-API-Key": "external-test-key"},
        data={
            "document_no": "STMT-2026-TYPE2",
            "document_type": "2",
            "callback_url": "http://internal/callback",
        },
        files=[
            ("target", ("a.pdf", _pdf_bytes(), "application/pdf")),
        ],
    )
    assert response.status_code == 202, response.json()
    body = response.json()
    assert body["document_type"] == "2"
    task = task_manager.get(body["task_id"])
    assert task is not None
    assert task.document_type == "2"


def test_external_statement_document_type_accepts_chinese_alias(external_client, monkeypatch):
    """中文别名归一:发票→"1"、对帐单→"2";空串/缺省→"1"(发票)。"""
    from document_comparison.api.app import task_manager

    async def _no_run(*_args, **_kwargs):
        return None

    monkeypatch.setattr(task_manager, "run_statement", _no_run)

    def _submit(document_type):
        data = {
            "document_no": "STMT-ALIAS",
            "callback_url": "http://internal/callback",
        }
        if document_type is not None:
            data["document_type"] = document_type
        return external_client.post(
            "/api/v1/external/amountStat",
            headers={"X-API-Key": "external-test-key"},
            data=data,
            files=[("target", ("a.pdf", _pdf_bytes(), "application/pdf"))],
        )

    resp = _submit("发票")
    assert resp.status_code == 202
    assert resp.json()["document_type"] == "1"

    resp = _submit("对帐单")
    assert resp.status_code == 202
    assert resp.json()["document_type"] == "2"

    resp = _submit("")
    assert resp.status_code == 202
    assert resp.json()["document_type"] == "1"
    task = task_manager.get(resp.json()["task_id"])
    assert task.document_type == "1"


def test_external_statement_rejects_bad_document_type(external_client):
    response = external_client.post(
        "/api/v1/external/amountStat",
        headers={"X-API-Key": "external-test-key"},
        data={
            "document_no": "STMT-BADTYPE",
            "document_type": "3",
            "callback_url": "http://internal/callback",
        },
        files=[
            ("target", ("a.pdf", _pdf_bytes(), "application/pdf")),
        ],
    )
    assert response.status_code == 400
    assert "document_type" in response.json()["message"]


def test_external_statement_rejects_missing_files(external_client):
    response = external_client.post(
        "/api/v1/external/amountStat",
        headers={"X-API-Key": "external-test-key"},
        data={
            "document_no": "STMT-EMPTY",
            "callback_url": "http://internal/callback",
        },
    )
    assert response.status_code == 400
    assert "target" in response.json()["message"]


def test_external_statement_rejects_non_pdf(external_client):
    response = external_client.post(
        "/api/v1/external/amountStat",
        headers={"X-API-Key": "external-test-key"},
        data={
            "document_no": "STMT-NONPDF",
            "callback_url": "http://internal/callback",
        },
        files=[
            ("target", ("a.txt", io.BytesIO(b"not a pdf"), "text/plain")),
        ],
    )
    assert response.status_code == 400
    assert ".pdf" in response.json()["message"]


def test_external_statement_async_requires_callback_url(external_client):
    response = external_client.post(
        "/api/v1/external/amountStat",
        headers={"X-API-Key": "external-test-key"},
        data={"document_no": "STMT-NOCB"},
        files=[
            ("target", ("a.pdf", _pdf_bytes(), "application/pdf")),
        ],
    )
    assert response.status_code == 400
    assert "callback_url" in response.json()["message"]


def test_external_statement_sync_returns_full_result(external_client, monkeypatch):
    from document_comparison.api.app import task_manager

    report = _statement_report()

    async def _set_report(task_id, pdf_paths, file_names, **_kwargs):
        task = task_manager.get(task_id)
        assert task is not None
        task.statement_report = report
        task.info.status = "done"
        task.push_event("done", 1.0)

    monkeypatch.setattr(task_manager, "run_statement", _set_report)

    response = external_client.post(
        "/api/v1/external/amountStat",
        headers={"X-API-Key": "external-test-key"},
        data={"document_no": "STMT-SYNC", "sync": "true"},
        files=[
            ("target", ("a.pdf", _pdf_bytes(), "application/pdf")),
        ],
    )
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["status"] == "done"
    assert body["document_type"] == "1"
    assert body["grand_total"] == 100000.0
    assert body["verdict"] == "clean"
    assert "grand_totals_by_column" not in body
    assert body["file_totals"] == [
        {"file_index": 0, "file_name": "target.pdf", "total_amount": 100000.0, "error": None}
    ]
    assert body["total_files"] == 1
    assert body["total_items"] == 2
    assert "column_detection_summary" not in body
    assert "report" not in body
    assert body["result_url"].endswith(f"/api/v1/external/amountStat/{body['task_id']}")


def test_external_statement_query_done_returns_flat_result(external_client, monkeypatch):
    from document_comparison.api.app import task_manager

    report = _statement_report()

    async def _set_report(task_id, pdf_paths, file_names, **_kwargs):
        task = task_manager.get(task_id)
        assert task is not None
        task.statement_report = report
        task.info.status = "done"
        task.push_event("done", 1.0)

    monkeypatch.setattr(task_manager, "run_statement", _set_report)

    submit = external_client.post(
        "/api/v1/external/amountStat",
        headers={"X-API-Key": "external-test-key"},
        data={"document_no": "STMT-Q", "sync": "true"},
        files=[
            ("target", ("a.pdf", _pdf_bytes(), "application/pdf")),
        ],
    )
    task_id = submit.json()["task_id"]
    query = external_client.get(
        f"/api/v1/external/amountStat/{task_id}",
        headers={"X-API-Key": "external-test-key"},
    )
    assert query.status_code == 200, query.json()
    body = query.json()
    assert body["status"] == "done"
    assert body["grand_total"] == 100000.0
    assert body["verdict"] == "clean"
    assert "column_detection_summary" not in body
    assert "report" not in body


def test_external_statement_query_internal_statement_task_isolated(external_client, monkeypatch):
    """内部 statement 任务(external_request=False)不可被外部端点查询 → 404。"""
    from document_comparison.api.app import task_manager

    task_id = task_manager.create("statement", target_names=["a.pdf"])
    task = task_manager.get(task_id)
    assert task is not None
    assert task.external_request is False

    response = external_client.get(
        f"/api/v1/external/amountStat/{task_id}",
        headers={"X-API-Key": "external-test-key"},
    )
    assert response.status_code == 404


def test_external_statement_query_unknown_task_404(external_client):
    response = external_client.get(
        "/api/v1/external/amountStat/nonexistent",
        headers={"X-API-Key": "external-test-key"},
    )
    assert response.status_code == 404


def test_external_statement_auth_required(external_client, monkeypatch):
    # 配置了 Key:缺失/错误头 → 401
    assert external_client.post(
        "/api/v1/external/amountStat",
        data={"document_no": "X", "callback_url": "http://internal/callback"},
        files=[("target", ("a.pdf", _pdf_bytes(), "application/pdf"))],
    ).status_code == 401
    # 未配置 Key:鉴权跳过,放行到业务层
    monkeypatch.setattr(settings, "external_api_key", "")
    response = external_client.post(
        "/api/v1/external/amountStat",
        data={"document_no": "X", "callback_url": "http://internal/callback"},
        files=[("target", ("a.pdf", _pdf_bytes(), "application/pdf"))],
    )
    assert response.status_code == 202


# —— 管线测试端点 /api/v1/statement/api-test(免鉴权,仿 compare/api-test)——


def test_statement_api_test_submit_reuses_external_flow_without_callback(
    external_client, monkeypatch
):
    """管线测试:免鉴权(无需 X-API-Key)、强制 API-TEST- 前缀、不设回调、走 external 任务流。"""
    from document_comparison.api.app import task_manager

    captured: dict = {}

    async def _capture_run(task_id, pdf_paths, file_names, **kwargs):
        captured["task_id"] = task_id
        captured["pdf_paths"] = pdf_paths
        captured["file_names"] = file_names
        captured["kwargs"] = kwargs

    monkeypatch.setattr(task_manager, "run_statement", _capture_run)
    # 不带 X-API-Key(管线测试免鉴权)
    response = external_client.post(
        "/api/v1/statement/api-test",
        files=[
            ("target", ("a.pdf", _pdf_bytes(), "application/pdf")),
            ("target", ("b.pdf", _pdf_bytes(), "application/pdf")),
        ],
    )
    assert response.status_code == 202, response.json()
    body = response.json()
    assert body["document_no"].startswith("API-TEST-")
    assert body["status"] == "pending"
    task = task_manager.get(body["task_id"])
    assert task is not None
    assert task.external_request is True
    assert task.kind == "statement"
    assert task.callback_url is None  # 不发回调
    # 多文件按 index 落定,不互相覆盖
    assert len(captured["pdf_paths"]) == 2
    assert captured["file_names"] == ["a.pdf", "b.pdf"]
    assert captured["kwargs"]["ocr_backend"] == "paddleocr"  # 读 external_ocr_backend


def test_statement_api_test_accepts_file_and_url_targets(external_client, monkeypatch):
    """管线测试与正式 amountStat 一致，文件与 URL 可混合提交。"""
    from document_comparison.api.app import task_manager

    captured: dict = {}

    async def _capture_run(task_id, pdf_paths, file_names, **kwargs):
        captured["task_id"] = task_id
        captured["pdf_paths"] = pdf_paths
        captured["file_names"] = file_names
        captured["kwargs"] = kwargs

    monkeypatch.setattr(task_manager, "run_statement", _capture_run)
    calls = _patch_download(monkeypatch, target_name="from-url.pdf")
    response = external_client.post(
        "/api/v1/statement/api-test",
        data={"target_urls": "https://files.example.test/from-url.pdf"},
        files=[("target", ("upload.pdf", _pdf_bytes(), "application/pdf"))],
    )

    assert response.status_code == 202, response.json()
    assert calls["urls"] == ["https://files.example.test/from-url.pdf"]
    assert captured["file_names"] == ["upload.pdf", "from-url.pdf"]
    assert len(captured["pdf_paths"]) == 2
    assert captured["pdf_paths"][0].endswith("upload.pdf")
    assert captured["pdf_paths"][1].endswith("from-url.pdf")


def test_statement_api_test_accepts_json_url_array(external_client, monkeypatch):
    """multipart 的 target_urls 可用单个 JSON 数组字段提交。"""
    from document_comparison.api.app import task_manager

    captured: dict = {}

    async def _capture_run(_task_id, pdf_paths, file_names, **_kwargs):
        captured["pdf_paths"] = pdf_paths
        captured["file_names"] = file_names

    monkeypatch.setattr(task_manager, "run_statement", _capture_run)
    calls = _patch_download(monkeypatch, target_name="from-array.pdf")
    response = external_client.post(
        "/api/v1/statement/api-test",
        data={
            "target_urls": json.dumps([
                "https://files.example.test/first.pdf",
                "https://files.example.test/second.pdf",
            ])
        },
    )

    assert response.status_code == 202, response.json()
    assert calls["urls"] == [
        "https://files.example.test/first.pdf",
        "https://files.example.test/second.pdf",
    ]
    assert captured["file_names"] == ["from-array.pdf", "from-array.pdf"]
    assert len(captured["pdf_paths"]) == 2


def test_statement_api_test_requires_complete_external_config(external_client):
    """外部 API 未配置完整 → 400(与 compare/api-test 一致,即使免鉴权也拦截)。"""
    # external_client fixture 已配齐 external_*;临时置坏其中一个
    saved = settings.external_max_upload_mb
    settings.external_max_upload_mb = 0
    try:
        response = external_client.post(
            "/api/v1/statement/api-test",
            files=[("target", ("a.pdf", _pdf_bytes(), "application/pdf"))],
        )
        assert response.status_code == 400
        assert "配置" in response.json()["message"]
    finally:
        settings.external_max_upload_mb = saved


def test_statement_api_test_rejects_non_pdf_and_empty(external_client):
    # 无文件也无 URL → 业务校验 400；URL-only 是合法输入。
    empty = external_client.post("/api/v1/statement/api-test", files=[])
    assert empty.status_code == 400
    assert "target" in empty.json()["message"]
    # 非 pdf
    bad = external_client.post(
        "/api/v1/statement/api-test",
        files=[("target", ("a.txt", io.BytesIO(b"not a pdf"), "text/plain"))],
    )
    assert bad.status_code == 400
    assert ".pdf" in bad.json()["message"]


def test_statement_api_test_query_returns_done_result(external_client, monkeypatch):
    """管线测试查询:复用对外响应结构,done 时返回扁平汇总字段;非 API-TEST- 任务 404。"""
    from document_comparison.api.app import task_manager

    report = _statement_report()

    async def _set_report(task_id, pdf_paths, file_names, **_kwargs):
        task = task_manager.get(task_id)
        assert task is not None
        task.statement_report = report
        task.info.status = "done"
        task.push_event("done", 1.0)

    monkeypatch.setattr(task_manager, "run_statement", _set_report)
    submit = external_client.post(
        "/api/v1/statement/api-test",
        files=[("target", ("a.pdf", _pdf_bytes(), "application/pdf"))],
    )
    assert submit.status_code == 202
    task_id = submit.json()["task_id"]
    # 轮询直到 done(异步任务,事件循环在 run_statement 设好报告后推进)
    import time

    deadline = time.perf_counter() + 2.0
    while time.perf_counter() < deadline:
        query = external_client.get(f"/api/v1/statement/api-test/{task_id}")
        if query.json().get("status") == "done":
            break
        time.sleep(0.05)
    else:
        query = external_client.get(f"/api/v1/statement/api-test/{task_id}")
    assert query.status_code == 200, query.json()
    body = query.json()
    assert body["status"] == "done"
    assert body["grand_total"] == 100000.0
    assert body["verdict"] == "clean"
    assert body["document_no"].startswith("API-TEST-")


def test_statement_api_test_query_isolates_non_test_tasks(external_client, monkeypatch):
    """非 API-TEST- 前缀的(对外/内部)金额统计任务不可被管线测试端点查询 → 404。"""
    from document_comparison.api.app import task_manager

    # 一个真实对外金额统计任务(无 API-TEST- 前缀)
    task_id = task_manager.create(
        "statement",
        document_no="STMT-REAL",
        external_request=True,
    )
    task = task_manager.get(task_id)
    assert task is not None
    task.statement_report = _statement_report()
    task.info.status = "done"
    task.push_event("done", 1.0)

    response = external_client.get(f"/api/v1/statement/api-test/{task_id}")
    assert response.status_code == 404


# —— 报告下载端点 + result_url / html_url ——


def test_build_external_result_url_points_to_pdf_report():
    """result_url 指向 PDF 报告下载端点(/report.pdf);html_url 指向 HTML 版本。"""
    report = TamperReport(source="s.docx", target="t.pdf", change_status="clean", diffs=[])
    res = build_external_result("task-html-1", "BILL-HTML", report)
    assert res["result_url"].endswith("/api/v1/external/contractCompare/task-html-1/report.pdf")
    # 不再是裸 task_id 查询端点
    assert not res["result_url"].endswith("/contractCompare/task-html-1")


def test_build_external_result_includes_html_url():
    """结果结构新增 html_url,指向 HTML 报告下载端点(/report.html)。"""
    report = TamperReport(source="s.docx", target="t.pdf", change_status="clean", diffs=[])
    res = build_external_result("task-pdf-0", "BILL-PDF", report)
    assert res["html_url"].endswith("/api/v1/external/contractCompare/task-pdf-0/report.html")


def _make_external_done_task(task_manager, document_no="BILL-HTML-1"):
    """构造一个 done 状态的对外比对任务(不跑真实 pipeline)。"""
    task_id = task_manager.create(
        "compare", document_no=document_no, external_request=True
    )
    task = task_manager.get(task_id)
    assert task is not None
    task.report = TamperReport(source="s.docx", target="t.pdf", change_status="clean", diffs=[])
    task.info.status = "done"
    task.push_event("done", 1.0)
    return task_id


def test_html_report_download_requires_api_key(external_client):
    """report.html 端点复用外部 API Key 鉴权:缺失/错误 → 401。"""
    from document_comparison.api.app import task_manager
    from document_comparison.external_api import external_html_report_path

    task_id = _make_external_done_task(task_manager)
    # 预置 HTML 文件(模拟产物生成)
    external_html_report_path(task_id).parent.mkdir(parents=True, exist_ok=True)
    external_html_report_path(task_id).write_text("<html>ok</html>", encoding="utf-8")

    url = f"/api/v1/external/contractCompare/{task_id}/report.html"
    assert external_client.get(url).status_code == 401  # 无 Key
    assert external_client.get(url, headers={"X-API-Key": "wrong"}).status_code == 401


def test_html_report_download_returns_attachment_with_filename(external_client):
    """200 返回 HTML;Content-Disposition 为 attachment,文件名含【单据号】对比。"""
    from document_comparison.api.app import task_manager
    from document_comparison.external_api import external_html_report_path

    task_id = _make_external_done_task(task_manager, document_no="BILL-HTML-2")
    external_html_report_path(task_id).parent.mkdir(parents=True, exist_ok=True)
    external_html_report_path(task_id).write_text("<html>report body</html>", encoding="utf-8")

    r = external_client.get(
        f"/api/v1/external/contractCompare/{task_id}/report.html",
        headers={"X-API-Key": "external-test-key"},
    )
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    disp = r.headers.get("content-disposition", "")
    assert "attachment" in disp
    # RFC 5987 编码后的中文文件名(URL-encoded);断言核心片段
    assert "filename*=UTF-8''" in disp
    assert "report body" in r.text


def test_html_report_download_404_when_file_missing(external_client):
    """任务存在但 HTML 文件尚未生成/已清理 → 404。"""
    from document_comparison.api.app import task_manager

    task_id = _make_external_done_task(task_manager, document_no="BILL-HTML-3")
    # 故意不写 HTML 文件
    r = external_client.get(
        f"/api/v1/external/contractCompare/{task_id}/report.html",
        headers={"X-API-Key": "external-test-key"},
    )
    assert r.status_code == 404


def test_html_report_download_404_for_non_external_task(external_client):
    """非对外任务(internal)不可经外部端点下载报告 → 404。"""
    from document_comparison.api.app import task_manager

    task_id = task_manager.create("compare", external_request=False)  # 内部任务
    r = external_client.get(
        f"/api/v1/external/contractCompare/{task_id}/report.html",
        headers={"X-API-Key": "external-test-key"},
    )
    assert r.status_code == 404


# —— PDF 报告下载端点 + html_url ——


def test_pdf_report_download_requires_api_key(external_client):
    """report.pdf 端点复用外部 API Key 鉴权:缺失/错误 → 401。"""
    from document_comparison.api.app import task_manager
    from document_comparison.external_api import external_pdf_report_path

    task_id = _make_external_done_task(task_manager)
    external_pdf_report_path(task_id).parent.mkdir(parents=True, exist_ok=True)
    external_pdf_report_path(task_id).write_bytes(b"%PDF-1.4 stub")

    url = f"/api/v1/external/contractCompare/{task_id}/report.pdf"
    assert external_client.get(url).status_code == 401  # 无 Key
    assert external_client.get(url, headers={"X-API-Key": "wrong"}).status_code == 401


def test_pdf_report_download_returns_attachment_with_filename(external_client):
    """200 返回 PDF;Content-Disposition 为 attachment,文件名后缀 .pdf。"""
    from document_comparison.api.app import task_manager
    from document_comparison.external_api import external_pdf_report_path

    task_id = _make_external_done_task(task_manager, document_no="BILL-PDF-2")
    external_pdf_report_path(task_id).parent.mkdir(parents=True, exist_ok=True)
    external_pdf_report_path(task_id).write_bytes(b"%PDF-1.4 report body")

    r = external_client.get(
        f"/api/v1/external/contractCompare/{task_id}/report.pdf",
        headers={"X-API-Key": "external-test-key"},
    )
    assert r.status_code == 200
    assert "application/pdf" in r.headers.get("content-type", "")
    disp = r.headers.get("content-disposition", "")
    assert "attachment" in disp
    assert "filename*=UTF-8''" in disp
    assert disp.rstrip("'").endswith(".pdf")


def test_pdf_report_download_404_when_file_missing(external_client):
    """任务存在但 PDF 文件尚未生成/已清理 → 404。"""
    from document_comparison.api.app import task_manager

    task_id = _make_external_done_task(task_manager, document_no="BILL-PDF-3")
    r = external_client.get(
        f"/api/v1/external/contractCompare/{task_id}/report.pdf",
        headers={"X-API-Key": "external-test-key"},
    )
    assert r.status_code == 404


def test_pdf_report_download_404_for_non_external_task(external_client):
    """非对外任务(internal)不可经外部端点下载 PDF 报告 → 404。"""
    from document_comparison.api.app import task_manager

    task_id = task_manager.create("compare", external_request=False)
    r = external_client.get(
        f"/api/v1/external/contractCompare/{task_id}/report.pdf",
        headers={"X-API-Key": "external-test-key"},
    )
    assert r.status_code == 404


def test_result_payload_of_done_task_carries_result_and_html_url(external_client):
    """done 任务查询响应:result_url 指向 PDF 报告,html_url 指向 HTML 报告。"""
    from document_comparison.api.app import task_manager

    task_id = _make_external_done_task(task_manager, document_no="BILL-PDF-4")
    r = external_client.get(
        f"/api/v1/external/contractCompare/{task_id}",
        headers={"X-API-Key": "external-test-key"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["result_url"].endswith(f"/contractCompare/{task_id}/report.pdf")
    assert body["html_url"].endswith(f"/contractCompare/{task_id}/report.html")
