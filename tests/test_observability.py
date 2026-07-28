"""observability 模块的 LLM 调用收集器单测(不依赖 DB)。"""
from __future__ import annotations

import logging

import pytest

from document_comparison.observability import (
    LlmCallRecord,
    _MAX_RESPONSE_CHARS,
    _model_log_summary,
    llm_call_collector,
    log_context,
    log_model_failure,
    log_model_request,
    log_model_response,
    log_value_summary,
)


@pytest.fixture
def quiet_logger():
    """不输出到 stderr 的 logger,避免测试日志噪音。"""
    return logging.getLogger("test_observability")


# —— 收集开关 ——

def test_no_collector_set_does_not_error(quiet_logger):
    """无收集器时 log_model_request/response 静默,不抛错(独立脚本场景)。"""
    t = log_model_request(quiet_logger, "ocr", "http://x/v1", {"model": "m"}, 1)
    log_model_response(quiet_logger, "ocr", 200, {"choices": [{"message": {"content": "x"}}]}, t)
    # 无 assert,不抛即通过


def test_collector_appends_on_request(quiet_logger):
    """collector set 时 log_model_request 立即 append 一条未补全记录。"""
    with llm_call_collector() as recs:
        t = log_model_request(quiet_logger, "ocr", "http://x/v1", {"model": "m"}, 1)
        assert len(recs) == 1
        assert recs[0].kind == "ocr"
        assert recs[0].attempt == 1
        assert recs[0].status_code is None
        assert recs[0].payload == {"model": "m"}
        assert t > 0


def test_response_finalizes_record(quiet_logger):
    """log_model_response 补全 status_code/elapsed_ms/response。"""
    with llm_call_collector() as recs:
        t = log_model_request(quiet_logger, "judge", "http://x/v1", {"model": "j"}, 1)
        log_model_response(
            quiet_logger, "judge", 200,
            {"choices": [{"message": {"content": "hi"}}]}, t,
        )
    assert len(recs) == 1
    assert recs[0].status_code == 200
    assert recs[0].elapsed_ms is not None and recs[0].elapsed_ms >= 0
    assert recs[0].response == {"choices": [{"message": {"content": "hi"}}]}
    assert recs[0].error is None


def test_failure_finalizes_record(quiet_logger):
    """log_model_failure 补全 error/status_code,response 留 None。"""
    with llm_call_collector() as recs:
        t = log_model_request(quiet_logger, "ocr", "http://x/v1", {"model": "m"}, 1)
        log_model_failure(quiet_logger, "ocr", t, "connection timeout")
    assert len(recs) == 1
    assert recs[0].status_code is None
    assert recs[0].error == "connection timeout"
    assert recs[0].response is None


def test_failure_with_status_code(quiet_logger):
    """4xx/5xx 失败传 status_code。"""
    with llm_call_collector() as recs:
        t = log_model_request(quiet_logger, "ocr", "http://x/v1", {"model": "m"}, 1)
        log_model_failure(
            quiet_logger, "ocr", t, "HTTP 503",
            status_code=503,
        )
    assert recs[0].status_code == 503
    assert recs[0].error == "HTTP 503"


# —— embedding 排除 ——

def test_embedding_not_collected(quiet_logger):
    """embedding kind 不收集(文本→向量,非对话型,量太大)。"""
    with llm_call_collector() as recs:
        t = log_model_request(quiet_logger, "embedding", "http://x/v1/embeddings", {"model": "e"}, 1)
        log_model_response(quiet_logger, "embedding", 200, {"data": [[]]}, t)
    assert len(recs) == 0, "embedding 不应被收集"


def test_alignment_llm_call_is_collected(quiet_logger):
    """歧义条款 LLM 裁决属于对话型调用，必须进入任务调用明细。"""
    with llm_call_collector() as recs:
        started = log_model_request(
            quiet_logger,
            "alignment",
            "http://x/v1/chat/completions",
            {"model": "judge-model"},
            1,
        )
        log_model_response(
            quiet_logger,
            "alignment",
            200,
            {"selected": []},
            started,
        )

    assert len(recs) == 1
    assert recs[0].kind == "alignment"
    assert recs[0].status_code == 200


# —— 图片脱敏 ——

def test_image_base64_scrubbed(quiet_logger):
    """payload 中的 data:image base64 被替换为 sha256 摘要,不存原始 base64。"""
    big_b64 = "A" * 5000
    payload = {
        "model": "m",
        "messages": [{"image": {"url": f"data:image/png;base64,{big_b64}"}}],
    }
    with llm_call_collector() as recs:
        t = log_model_request(quiet_logger, "ocr", "http://x/v1", payload, 1)
        log_model_response(quiet_logger, "ocr", 200, {"ok": True}, t)
    img = recs[0].payload["messages"][0]["image"]["url"]
    assert isinstance(img, dict)
    assert img["data_url"] == "data:image/png;base64"
    assert img["base64_chars"] == 5000
    assert len(img["sha256"]) == 64
    assert "AAAA" not in str(recs[0].payload), "原始 base64 不应出现在 payload"


def test_model_log_summary_does_not_include_contract_text():
    """文件日志只保留可关联摘要，合同正文仍仅存在受控的调用审计记录中。"""
    secret_clause = "甲方应于2026年8月1日前支付987654元"
    summary = _model_log_summary({"model": "vision", "messages": [secret_clause]})
    assert summary["model"] == "vision"
    assert summary["chars"] > 0
    assert len(summary["sha256"]) == 64
    assert secret_clause not in str(summary)
    assert log_value_summary(secret_clause) == _model_log_summary(secret_clause)


def test_log_context_is_scoped_and_restored():
    """嵌套上下文可以附加 request_id，并在退出时恢复。"""
    from document_comparison.observability import get_log_context

    assert get_log_context() == {}
    with log_context(task_id="task-a"):
        assert get_log_context() == {"task_id": "task-a"}
        with log_context(request_id="req-b"):
            assert get_log_context() == {"task_id": "task-a", "request_id": "req-b"}
        assert get_log_context() == {"task_id": "task-a"}
    assert get_log_context() == {}


# —— response 截断 ——

def test_response_truncated_to_max_chars(quiet_logger):
    """超大 response 截断到 _MAX_RESPONSE_CHARS 并加尾标记。"""
    big_content = "x" * (_MAX_RESPONSE_CHARS + 5000)
    with llm_call_collector() as recs:
        t = log_model_request(quiet_logger, "ocr", "http://x/v1", {"model": "m"}, 1)
        log_model_response(quiet_logger, "ocr", 200, {"content": big_content}, t)
    # 截断后存为 JSON 字符串(dict 序列化超长)
    resp = recs[0].response
    assert isinstance(resp, str)
    assert len(resp) <= _MAX_RESPONSE_CHARS + 100  # 尾标记
    assert resp.endswith("…(truncated)")


def test_small_response_not_truncated(quiet_logger):
    """正常大小的 response 保持 dict 结构。"""
    with llm_call_collector() as recs:
        t = log_model_request(quiet_logger, "ocr", "http://x/v1", {"model": "m"}, 1)
        log_model_response(quiet_logger, "ocr", 200, {"choices": [{"message": {"content": "小响应"}}]}, t)
    assert isinstance(recs[0].response, dict)
    assert recs[0].response["choices"][0]["message"]["content"] == "小响应"


# —— 并发:同 kind 多条未补全记录的 finalize ——

def test_finalize_picks_last_unfinalized_same_kind(quiet_logger):
    """同 kind 多条未补全记录时,finalize 补全最后一条(模拟并发逐页 OCR)。"""
    with llm_call_collector() as recs:
        t1 = log_model_request(quiet_logger, "ocr", "http://x/v1", {"page": 1}, 1)
        t2 = log_model_request(quiet_logger, "ocr", "http://x/v1", {"page": 2}, 1)
        # 先补 page 2(后入),再补 page 1
        log_model_response(quiet_logger, "ocr", 200, {"page": 2, "ok": True}, t2)
        log_model_response(quiet_logger, "ocr", 200, {"page": 1, "ok": True}, t1)
    assert len(recs) == 2
    # 两条都被补全
    assert all(r.status_code == 200 for r in recs)
    assert all(r.response is not None for r in recs)
    pages = sorted(r.payload["page"] for r in recs)
    assert pages == [1, 2]


# —— collector 退出后隔离 ——

def test_collector_resets_after_context_exit(quiet_logger):
    """llm_call_collector 退出后 contextvar 复位,后续调用不再 append。"""
    with llm_call_collector() as recs:
        t = log_model_request(quiet_logger, "ocr", "http://x/v1", {"model": "m"}, 1)
        log_model_response(quiet_logger, "ocr", 200, {"ok": True}, t)
    assert len(recs) == 1
    # 退出后再调用,不抛也不 append(无收集器)
    t = log_model_request(quiet_logger, "ocr", "http://x/v1", {"model": "m"}, 1)
    log_model_response(quiet_logger, "ocr", 200, {"ok": True}, t)
    assert len(recs) == 1, "退出后不应再 append"
