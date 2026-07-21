"""API 端点冒烟测试(使用 FastAPI TestClient)。"""
import pytest
from fastapi.testclient import TestClient

from document_comparison.api.app import create_app
from document_comparison.config import settings


@pytest.fixture
def client(db_isolated):
    """所有 API 测试在隔离的测试库中跑(避免污染开发库 doc_compare)。

    db_isolated fixture 已 monkeypatch 了 settings.database_url 与引擎工厂,
    所以 create_app() 内的 db_pkg.init_engine() 会连到测试库。
    """
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


def test_compare_accepts_enable_risk_assessment_option(client, monkeypatch):
    """options 携带 enable_risk_assessment 应被正确解析并透传到 task_manager.run。"""
    import io
    from docx import Document  # type: ignore[import-untyped]

    from document_comparison.api.app import task_manager

    # 捕获 run 调用参数,避免真实 OCR
    captured: dict = {}
    async def _spy_run(*args, **kwargs):
        captured.update(kwargs)
    monkeypatch.setattr(task_manager, "run", _spy_run)

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
    pdf.new_page(width=595, height=842)
    pdf.save(pdf_buf)
    pdf_buf.seek(0)

    r = client.post(
        "/api/v1/compare",
        files={
            "source": ("contract.docx", doc_buf,
                       "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            "target": ("scan.pdf", pdf_buf, "application/pdf"),
        },
        data={"options": '{"enable_risk_assessment": true, "enable_llm_judge": true}'},
    )
    assert r.status_code == 200, r.json()
    assert captured.get("enable_risk_assessment") is True
    assert captured.get("enable_llm_judge") is True


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


def test_llm_config_persists_max_pdf_pages(client, monkeypatch):
    """PUT /api/v1/config/llm 应把 max_pdf_pages 写入 PG llm_config 表并应用到 settings。"""
    from document_comparison.db import repository as db_repo

    # save_llm_overrides 内部合并已有 PG 记录,这里先清空避免被前序用例污染
    db_repo.save_llm_config({})

    r = client.put("/api/v1/config/llm", json={"max_pdf_pages": 12})
    assert r.status_code == 200
    assert r.json()["config"]["max_pdf_pages"] == 12

    # 应用到运行时单例(app.py 复用同一个 settings 对象)
    assert settings.max_pdf_pages == 12

    # 持久化到 PG
    persisted = db_repo.get_llm_config()
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


# —— 比对记录历史端点 + 进程重启兜底测试(/api/v1/tasks)——

def test_tasks_listing_returns_items(client):
    """POST 提交后会写入 task_records,GET /api/v1/tasks 应能列出。"""
    import io

    from document_comparison.api.app import task_manager

    # mock run 避免真实 OCR
    async def _noop(*args, **kwargs):
        return None
    orig_run = task_manager.run
    task_manager.run = _noop  # type: ignore[assignment]

    try:
        import pymupdf as fitz
        pdf_buf = io.BytesIO()
        pdf = fitz.open()
        pdf.new_page(width=595, height=842)
        pdf.save(pdf_buf)
        pdf.close()
        pdf_buf.seek(0)

        from docx import Document  # type: ignore[import-untyped]
        doc = Document()
        doc.add_paragraph("第一条 测试条款")
        doc_buf = io.BytesIO()
        doc.save(doc_buf)
        doc_buf.seek(0)

        r = client.post(
            "/api/v1/compare",
            files={
                "source": ("contract.docx", doc_buf,
                           "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
                "target": ("scan.pdf", pdf_buf, "application/pdf"),
            },
        )
        assert r.status_code == 200
    finally:
        task_manager.run = orig_run  # type: ignore[assignment]

    # 列表端点
    r = client.get("/api/v1/tasks")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body and "total" in body
    assert body["total"] >= 1
    assert any(it["kind"] == "compare" for it in body["items"])


def test_tasks_listing_filter_by_kind(client):
    """kind 过滤生效:仅 compare。"""
    from document_comparison.db import repository as db_repo

    db_repo.create_task("list-c1", "compare", target_names=["t.pdf"])
    db_repo.create_task("list-r1", "raw", target_names=["t.pdf"])
    db_repo.create_task("list-s1", "statement", target_names=["t.pdf"])

    r = client.get("/api/v1/tasks?kind=compare")
    assert r.status_code == 200
    body = r.json()
    assert all(it["kind"] == "compare" for it in body["items"])
    assert any(it["task_id"] == "list-c1" for it in body["items"])
    assert all(it["task_id"] != "list-r1" for it in body["items"])


def test_tasks_listing_rejects_bad_kind(client):
    r = client.get("/api/v1/tasks?kind=unknown")
    assert r.status_code == 400


def test_get_compare_result_survives_no_inmemory_task(client):
    """进程重启模拟:清空内存 task_manager._tasks 后,GET 应从 PG 回兜返回 done 状态。"""
    from document_comparison.db import repository as db_repo
    from document_comparison.models import TamperReport
    from document_comparison.api.app import task_manager

    task_manager._tasks.clear()

    db_repo.create_task("restart-1", "compare", target_names=["t.pdf"])
    db_repo.update_task_status(
        "restart-1", "done",
        overall_risk="clean",
        change_status="clean",
        elapsed=1.23,
        finished=True,
    )
    db_repo.save_compare_report(
        "restart-1", TamperReport(source="a.docx", target="b.pdf", overall_risk="clean"),
    )

    r = client.get("/api/v1/compare/restart-1")
    assert r.status_code == 200
    body = r.json()
    assert body["task_id"] == "restart-1"
    assert body["status"] == "done"
    assert body["overall_risk"] == "clean"
    assert body["elapsed"] == pytest.approx(1.23)
    assert body["report"]["overall_risk"] == "clean"


def test_download_report_from_pg_after_inmemory_cleared(client):
    """下载端点在内存 Task 不存在时应从 PG JSONB 还原报告(原文件兜底已移除)。

    回归保护:此前 /report 端点三级兜底「内存→文件→PG」,本次改造去掉文件层,
    仅靠 PG 必须仍能返回完整 JSON。
    """
    from document_comparison.db import repository as db_repo
    from document_comparison.models import TamperReport
    from document_comparison.api.app import task_manager

    task_manager._tasks.clear()

    db_repo.create_task("dl-1", "compare", target_names=["t.pdf"])
    db_repo.update_task_status("dl-1", "done", finished=True)
    db_repo.save_compare_report(
        "dl-1",
        TamperReport(source="a.docx", target="b.pdf", overall_risk="clean"),
    )

    r = client.get("/api/v1/compare/dl-1/report?format=json")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "a.docx"
    assert body["target"] == "b.pdf"
    assert body["overall_risk"] == "clean"


def test_get_task_events_endpoint(client):
    """/api/v1/tasks/{id}/events 返回持久化的里程碑事件。"""
    from document_comparison.db import repository as db_repo

    db_repo.create_task("ev-1", "compare")
    db_repo.save_milestone_event("ev-1", "start", 0.0, {})
    db_repo.save_milestone_event("ev-1", "done", 1.0, {"word": 0.3})

    r = client.get("/api/v1/tasks/ev-1/events")
    assert r.status_code == 200
    body = r.json()
    assert body["task_id"] == "ev-1"
    assert [it["stage"] for it in body["items"]] == ["start", "done"]


def test_get_task_events_unknown_404(client):
    r = client.get("/api/v1/tasks/unknown-task/events")
    assert r.status_code == 404
