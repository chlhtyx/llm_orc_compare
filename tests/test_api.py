"""API 端点冒烟测试(使用 FastAPI TestClient)。"""
import pytest
from fastapi.testclient import TestClient

from document_comparison.api.app import create_app
from document_comparison.config import settings


@pytest.fixture
def client():
    return TestClient(create_app())


@pytest.fixture(autouse=True)
def _reset_task_manager():
    """每个测试前后清空 task_manager,避免后台任务跨测试残留导致并发上限误触发。"""
    from document_comparison.api.app import task_manager
    task_manager._tasks.clear()
    yield
    task_manager._tasks.clear()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_compare_rejects_non_docx_source(client):
    import io

    r = client.post(
        "/api/v1/compare",
        files={
            "source": ("bad.txt", io.BytesIO(b"text"), "text/plain"),
            "target": ("ok.pdf", io.BytesIO(b"%PDF"), "application/pdf"),
        },
    )
    assert r.status_code == 400
    assert "docx" in r.json()["message"]


def test_compare_accepts_valid_files(client):
    import io
    from docx import Document  # type: ignore[import-untyped]

    doc = Document()
    doc.add_paragraph("第一条 测试条款")
    doc_buf = io.BytesIO()
    doc.save(doc_buf)
    doc_buf.seek(0)

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
    )
    assert r.status_code == 200
    assert "task_id" in r.json()


def test_compare_rejects_bad_options(client):
    """options 非法 JSON / 类型应返回 400。"""
    import io

    r = client.post(
        "/api/v1/compare",
        files={
            "source": ("bad.docx", io.BytesIO(b"x"), "application/octet-stream"),
            "target": ("ok.pdf", io.BytesIO(b"%PDF"), "application/pdf"),
        },
        data={"options": "not-json"},
    )
    assert r.status_code == 400


def test_raw_compare_accepts_valid_files(client):
    """无标注版 POST /api/v1/raw-compare 应接受 docx+pdf 并返回 task_id。"""
    import io
    from docx import Document  # type: ignore[import-untyped]

    doc = Document()
    doc.add_paragraph("第一条 测试条款")
    doc_buf = io.BytesIO()
    doc.save(doc_buf)
    doc_buf.seek(0)

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
        "/api/v1/raw-compare",
        files={
            "source": ("contract.docx", doc_buf, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            "target": ("scan.pdf", pdf_buf, "application/pdf"),
        },
    )
    assert r.status_code == 200
    assert "task_id" in r.json()


def test_raw_compare_rejects_non_docx(client):
    """无标注版 source 非 docx 应返回 400。"""
    import io

    r = client.post(
        "/api/v1/raw-compare",
        files={
            "source": ("bad.txt", io.BytesIO(b"text"), "text/plain"),
            "target": ("ok.pdf", io.BytesIO(b"%PDF"), "application/pdf"),
        },
    )
    assert r.status_code == 400
    assert "docx" in r.json()["message"]


def test_raw_task_not_found_404(client):
    r = client.get("/api/v1/raw-compare/nonexistent")
    assert r.status_code == 404


def test_task_not_found_404(client):
    r = client.get("/api/v1/compare/nonexistent")
    assert r.status_code == 404


def test_uniform_error_has_request_id(client):
    r = client.post("/api/v1/compare", files={})
    body = r.json()
    assert "code" in body
    assert "message" in body
    assert "request_id" in body


# —— PDF 页数上限预检(max_pdf_pages)——

def _make_pdf_bytes(num_pages: int) -> bytes:
    """构造一个 num_pages 页的空白 PDF(供页数上限测试)。"""
    import io

    try:
        import pymupdf as fitz
    except ImportError:
        import fitz  # type: ignore

    buf = io.BytesIO()
    pdf = fitz.open()
    for _ in range(num_pages):
        pdf.new_page(width=595, height=842)
    pdf.save(buf)
    pdf.close()
    buf.seek(0)
    return buf.read()


def _make_docx_bytes() -> bytes:
    import io
    from docx import Document  # type: ignore[import-untyped]

    doc = Document()
    doc.add_paragraph("第一条 测试条款")
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf.read()


def test_compare_rejects_oversized_pdf(client, monkeypatch):
    """max_pdf_pages 配置后,超过页数的 PDF 直接返回 400 暂不支持。"""
    import io

    monkeypatch.setattr(settings, "max_pdf_pages", 2)

    r = client.post(
        "/api/v1/compare",
        files={
            "source": ("contract.docx", io.BytesIO(_make_docx_bytes()),
                       "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            "target": ("scan.pdf", io.BytesIO(_make_pdf_bytes(3)), "application/pdf"),
        },
    )
    assert r.status_code == 400
    assert "暂不支持" in r.json()["message"]


def test_compare_allows_pdf_within_limit(client, monkeypatch):
    """max_pdf_pages 配置后,页数 <= 上限的 PDF 正常接受。"""
    import io

    monkeypatch.setattr(settings, "max_pdf_pages", 5)

    r = client.post(
        "/api/v1/compare",
        files={
            "source": ("contract.docx", io.BytesIO(_make_docx_bytes()),
                       "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            "target": ("scan.pdf", io.BytesIO(_make_pdf_bytes(3)), "application/pdf"),
        },
    )
    assert r.status_code == 200
    assert "task_id" in r.json()


def test_raw_compare_rejects_oversized_pdf(client, monkeypatch):
    """无标注版同样受 max_pdf_pages 约束。"""
    import io

    monkeypatch.setattr(settings, "max_pdf_pages", 1)

    r = client.post(
        "/api/v1/raw-compare",
        files={
            "source": ("contract.docx", io.BytesIO(_make_docx_bytes()),
                       "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            "target": ("scan.pdf", io.BytesIO(_make_pdf_bytes(2)), "application/pdf"),
        },
    )
    assert r.status_code == 400
    assert "暂不支持" in r.json()["message"]


def test_max_pdf_pages_zero_means_unlimited(client, monkeypatch):
    """max_pdf_pages = 0 时不限制,大 PDF 也能通过(默认行为)。"""
    import io

    monkeypatch.setattr(settings, "max_pdf_pages", 0)

    r = client.post(
        "/api/v1/compare",
        files={
            "source": ("contract.docx", io.BytesIO(_make_docx_bytes()),
                       "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            "target": ("scan.pdf", io.BytesIO(_make_pdf_bytes(20)), "application/pdf"),
        },
    )
    assert r.status_code == 200


def test_llm_config_persists_max_pdf_pages(client, monkeypatch, tmp_path):
    """PUT /api/v1/config/llm 应把 max_pdf_pages 写入持久化文件并应用到 settings。"""
    import json

    cfg_path = tmp_path / "llm_config.json"
    # save_llm_overrides 内部调用 config 模块命名空间下的 llm_config_path
    monkeypatch.setattr(
        "document_comparison.config.llm_config_path", lambda: cfg_path
    )

    r = client.put("/api/v1/config/llm", json={"max_pdf_pages": 12})
    assert r.status_code == 200
    assert r.json()["config"]["max_pdf_pages"] == 12

    # 应用到运行时单例(app.py 复用同一个 settings 对象)
    assert settings.max_pdf_pages == 12

    # 持久化文件也写入
    persisted = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert persisted["max_pdf_pages"] == 12


def test_llm_config_rejects_negative_max_pdf_pages(client):
    """max_pdf_pages 不能为负数。"""
    r = client.put("/api/v1/config/llm", json={"max_pdf_pages": -1})
    assert r.status_code == 400


# —— 对帐单金额统计端点冒烟(/api/v1/statement)——

def test_statement_accepts_multiple_pdfs(client, monkeypatch):
    """POST /api/v1/statement 接受多个 PDF,返回 task_id + file_count。

    mock 掉 run_statement 避免触发真实 OCR;TestClient 多文件需用 list-of-tuple 形式
    (同字段名多次出现),不能用 dict 单键 list 形式(后者会触发 multipart 解析错误)。
    """
    import io

    from document_comparison.api.app import task_manager

    async def _noop(*args, **kwargs):
        return None
    monkeypatch.setattr(task_manager, "run_statement", _noop)

    try:
        import pymupdf as fitz
    except ImportError:
        import fitz  # type: ignore

    def _pdf_bytes():
        buf = io.BytesIO()
        pdf = fitz.open()
        pdf.new_page(width=595, height=842)
        pdf.save(buf)
        pdf.close()
        buf.seek(0)
        return buf

    r = client.post(
        "/api/v1/statement",
        files=[
            ("target", ("a.pdf", _pdf_bytes(), "application/pdf")),
            ("target", ("b.pdf", _pdf_bytes(), "application/pdf")),
        ],
    )
    assert r.status_code == 200
    body = r.json()
    assert "task_id" in body
    assert body["file_count"] == 2


def test_statement_rejects_non_pdf(client):
    """混入非 PDF → 400。"""
    import io

    try:
        import pymupdf as fitz
    except ImportError:
        import fitz  # type: ignore

    pdf_buf = io.BytesIO()
    pdf = fitz.open()
    pdf.new_page(width=595, height=842)
    pdf.save(pdf_buf)
    pdf.close()
    pdf_buf.seek(0)

    r = client.post(
        "/api/v1/statement",
        files=[
            ("target", ("ok.pdf", pdf_buf, "application/pdf")),
            ("target", ("bad.txt", io.BytesIO(b"text"), "text/plain")),
        ],
    )
    assert r.status_code == 400
    assert "pdf" in r.json()["message"].lower()


def test_statement_rejects_oversized_pdf(client, monkeypatch):
    """多文件中任一超 max_pdf_pages → 400 暂不支持。"""
    import io

    try:
        import pymupdf as fitz
    except ImportError:
        import fitz  # type: ignore

    monkeypatch.setattr(settings, "max_pdf_pages", 1)
    # 构造 3 页 PDF
    buf = io.BytesIO()
    pdf = fitz.open()
    for _ in range(3):
        pdf.new_page(width=595, height=842)
    pdf.save(buf)
    pdf.close()
    buf.seek(0)

    r = client.post(
        "/api/v1/statement",
        files=[
            ("target", ("big.pdf", buf, "application/pdf")),
        ],
    )
    assert r.status_code == 400
    assert "暂不支持" in r.json()["message"]


def test_statement_task_not_found_404(client):
    r = client.get("/api/v1/statement/nonexistent")
    assert r.status_code == 404


def test_statement_report_not_found_404(client):
    r = client.get("/api/v1/statement/nonexistent/report")
    assert r.status_code == 404
