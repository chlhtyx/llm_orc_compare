"""外部合同比对 API、结果文本与高亮图片测试。"""
from __future__ import annotations

import io

import pymupdf
import pytest
from docx import Document

from document_comparison.config import settings
from document_comparison.external_api import (
    build_external_result,
    build_result_text,
    render_external_highlight_images,
    require_external_api_key,
    validate_callback_url,
)
from document_comparison.models import (
    Diff,
    DiffSegment,
    PageMeta,
    PageRegion,
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
    monkeypatch.setattr(settings, "external_enable_risk_assessment", False)
    yield client
    task_manager._tasks.clear()


def test_external_auth_missing_wrong_and_unconfigured(external_client, monkeypatch):
    assert external_client.get("/api/v1/external/contractCompare/missing").status_code == 401
    assert external_client.get(
        "/api/v1/external/contractCompare/missing", headers={"X-API-Key": "wrong"}
    ).status_code == 401
    monkeypatch.setattr(settings, "external_api_key", "")
    assert external_client.get(
        "/api/v1/external/contractCompare/missing", headers={"X-API-Key": "external-test-key"}
    ).status_code == 503


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
    monkeypatch.setattr(settings, "external_enable_risk_assessment", True)

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
    assert all(item["enable_risk_assessment"] is True for item in captured)


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
