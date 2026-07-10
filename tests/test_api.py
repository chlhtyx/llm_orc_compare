"""API 端点冒烟测试(使用 FastAPI TestClient)。"""
import pytest
from fastapi.testclient import TestClient

from document_comparison.api.app import create_app
from document_comparison.config import settings


@pytest.fixture
def client():
    return TestClient(create_app())


_AUTH = {"headers": {"X-API-Key": settings.api_keys[0]}}


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_missing_api_key_401(client):
    r = client.post("/api/v1/compare", files={})
    assert r.status_code == 401


def test_invalid_api_key_403(client):
    r = client.post(
        "/api/v1/compare", files={}, headers={"X-API-Key": "wrong"}
    )
    assert r.status_code == 403


def test_compare_rejects_non_docx_source(client):
    import io

    r = client.post(
        "/api/v1/compare",
        files={
            "source": ("bad.txt", io.BytesIO(b"text"), "text/plain"),
            "target": ("ok.pdf", io.BytesIO(b"%PDF"), "application/pdf"),
        },
        **_AUTH,
    )
    assert r.status_code == 400
    assert "docx" in r.json()["message"]


def test_compare_accepts_valid_files(client):
    import io
    from docx import Document  # type: ignore[import-untyped]

    # 生成一个最小 docx + pdf(PyMuPDF 文本层可读)
    doc = Document()
    doc.add_paragraph("第一条 测试条款")
    doc_buf = io.BytesIO()
    doc.save(doc_buf)
    doc_buf.seek(0)

    # 构造一个最小 pdf(PyMuPDF 可读文本层,有 pdf/pdf 标记)
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz  # type: ignore

    pdf_buf = io.BytesIO()
    pdf = fitz.open()
    page = pdf.new_page(width=595, height=842)
    page.insert_text((72, 72), "第一条 测试条款", fontsize=12)
    pdf.save(pdf_buf)
    pdf_buf.seek(0)

    r = client.post(
        "/api/v1/compare",
        files={
            "source": ("contract.docx", doc_buf, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            "target": ("scan.pdf", pdf_buf, "application/pdf"),
        },
        **_AUTH,
    )
    assert r.status_code == 200
    assert "task_id" in r.json()


def test_task_not_found_404(client):
    r = client.get("/api/v1/compare/nonexistent", **_AUTH)
    assert r.status_code == 404


def test_uniform_error_has_request_id(client):
    r = client.post(
        "/api/v1/compare", files={}, headers={"X-API-Key": "wrong"}
    )
    body = r.json()
    assert "code" in body
    assert "message" in body
    assert "request_id" in body
