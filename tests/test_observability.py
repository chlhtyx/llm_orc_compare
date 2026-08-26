"""observability 模块的 LLM 调用收集器单测(不依赖 DB)。"""
from __future__ import annotations

import logging

import pytest

from document_comparison.models import (
    Block,
    PageRecognitionDiagnostic,
    TableStructure,
)
from document_comparison.observability import (
    LlmCallRecord,
    _MAX_RESPONSE_CHARS,
    _model_log_summary,
    llm_call_collector,
    log_context,
    log_model_failure,
    log_model_request,
    log_model_response,
    model_call_context,
    log_value_summary,
    record_ocr_result,
    record_ocr_text_result,
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
        assert recs[0].payload["model"] == "m"
        assert recs[0].payload["_request"] == {"url": "http://x/v1"}
        assert t > 0


def test_collector_keeps_request_url_and_model_trace_context(quiet_logger):
    """任务调用明细应带 URL 和业务定位信息，但不改变实际 HTTP payload。"""
    with llm_call_collector() as recs:
        with model_call_context(statement_column={"file_index": 0, "table_index": 2}):
            started = log_model_request(
                quiet_logger, "statement-column", "http://x/v1/chat/completions",
                {"model": "vision-model"}, 1,
            )
        log_model_response(quiet_logger, "statement-column", 200, {"ok": True}, started)

    assert recs[0].payload["_request"] == {"url": "http://x/v1/chat/completions"}
    assert recs[0].payload["_trace"] == {
        "statement_column": {"file_index": 0, "table_index": 2}
    }
    assert recs[0].payload["model"] == "vision-model"


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


def test_failure_keeps_provider_response_for_task_audit(quiet_logger):
    """HTTP 失败也保留受限响应体，方便定位供应商的参数校验错误。"""
    with llm_call_collector() as recs:
        t = log_model_request(quiet_logger, "statement-column", "http://x/v1", {"model": "m"}, 1)
        log_model_failure(
            quiet_logger, "statement-column", t, "HTTP 400", status_code=400,
            response={"error": {"message": "model is required"}},
        )
    assert recs[0].error == "HTTP 400"
    assert recs[0].response == {"error": {"message": "model is required"}}


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


def test_anthropic_base64_image_source_scrubbed(quiet_logger):
    """Anthropic 协议的裸 base64(source.data,不以 data: 开头)同样脱敏。"""
    big_b64 = "Q" * 5000
    payload = {
        "model": "claude-sonnet",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": big_b64,
                        },
                    }
                ],
            }
        ],
    }
    with llm_call_collector() as recs:
        t = log_model_request(quiet_logger, "ocr", "http://x/v1/messages", payload, 1)
        log_model_response(quiet_logger, "ocr", 200, {"ok": True}, t)
    source = recs[0].payload["messages"][0]["content"][0]["source"]
    assert source["media_type"] == "image/png"
    assert isinstance(source["data"], dict)
    assert source["data"]["base64_chars"] == 5000
    assert len(source["data"]["sha256"]) == 64
    assert "QQQQ" not in str(recs[0].payload), "原始 base64 不应出现在 payload"


def test_model_log_summary_does_not_include_contract_text():
    """文件日志只保留可关联摘要，合同正文仍仅存在受控的调用审计记录中。"""
    secret_clause = "甲方应于2026年8月1日前支付987654元"
    summary = _model_log_summary({"model": "vision", "messages": [secret_clause]})
    assert summary["model"] == "vision"
    assert summary["chars"] > 0
    assert len(summary["sha256"]) == 64
    assert secret_clause not in str(summary)
    assert log_value_summary(secret_clause) == _model_log_summary(secret_clause)


# —— 最终 OCR 解析结果 ——

def test_structured_ocr_result_records_page_bbox_and_bounded_preview(
    quiet_logger, caplog,
):
    """最终 blocks 可按页排障，但普通日志不泄露合同文本或表格单元格。"""
    secret_clause = "10.4 测试条款1232456767"
    table = TableStructure(
        headers=["项目", "金额"],
        rows=[["保密项目", "987654.32"]],
    )
    pages = [
        [
            Block(
                block_id="p0-b0",
                page_index=0,
                label="paragraph_title",
                bbox=[10, 20, 300, 48],
                content=secret_clause,
            )
        ],
        [
            Block(
                block_id="p1-b0",
                page_index=1,
                label="table",
                bbox=[12, 50, 400, 500],
                content="项目 金额\n保密项目 987654.32",
                table=table,
            )
        ],
    ]
    diagnostics = [
        PageRecognitionDiagnostic(
            page_index=0,
            source="fallback",
            reliable=True,
            char_count=len(secret_clause),
            bbox_coverage=1.0,
        )
    ]

    with caplog.at_level(logging.INFO, logger=quiet_logger.name):
        with llm_call_collector() as recs:
            record_ocr_result(
                quiet_logger,
                pages,
                diagnostics=diagnostics,
                stage="compare",
                metadata={"ocr_backend": "official_sdk"},
            )

    assert len(recs) == 1
    record = recs[0]
    assert record.kind == "ocr-result"
    assert record.status_code == 200
    assert record.payload["operation"] == "final_ocr_blocks"
    assert record.payload["stage"] == "compare"
    assert isinstance(record.response, dict)
    assert record.response["summary"]["page_count"] == 2
    assert record.response["summary"]["block_count"] == 2
    assert record.response["blocks"][0]["outer_page_index"] == 0
    assert record.response["blocks"][0]["block_page_index"] == 0
    assert record.response["blocks"][0]["bbox"] == [10.0, 20.0, 300.0, 48.0]
    assert record.response["blocks"][0]["content_preview"] == secret_clause
    assert len(record.response["blocks"][0]["content_sha256"]) == 64
    assert record.response["blocks"][1]["table_shape"] == {
        "header_count": 2,
        "row_count": 1,
        "max_columns": 2,
    }
    assert "保密项目" not in str(record.response["blocks"][1]["table_shape"])
    assert record.response["diagnostics"][0]["source"] == "fallback"
    assert secret_clause not in caplog.text
    assert "987654.32" not in caplog.text


def test_ocr_result_limits_blocks_and_each_content_preview(quiet_logger):
    """大文档按块数和单块字符数限流，并明确报告省略数量。"""
    pages = [
        [
            Block(
                block_id=f"b-{index}",
                page_index=0,
                label="text",
                content="长" * 1000,
            )
            for index in range(170)
        ]
    ]
    with llm_call_collector() as recs:
        record_ocr_result(quiet_logger, pages, stage="compare")

    response = recs[0].response
    assert isinstance(response, dict)
    assert response["summary"]["block_count"] == 170
    assert response["summary"]["recorded_block_count"] == 100
    assert response["summary"]["omitted_block_count"] == 70
    assert len(response["blocks"][0]["content_preview"]) == 300
    assert response["blocks"][0]["content_truncated"] is True


def test_raw_ocr_text_result_records_bounded_preview_without_plaintext_log(
    quiet_logger, caplog,
):
    """无标注管线也保存最终文本，超长内容只留受限预览。"""
    secret_text = "机密合同正文" * 7000
    diagnostic = PageRecognitionDiagnostic(
        page_index=0,
        source="native",
        reliable=True,
        char_count=len(secret_text),
    )
    with caplog.at_level(logging.INFO, logger=quiet_logger.name):
        with llm_call_collector() as recs:
            record_ocr_text_result(
                quiet_logger,
                secret_text,
                diagnostic=diagnostic,
                stage="raw",
            )

    response = recs[0].response
    assert isinstance(response, dict)
    assert recs[0].kind == "ocr-result"
    assert response["summary"]["content_chars"] == len(secret_text)
    assert response["summary"]["content_truncated"] is True
    assert len(response["content_preview"]) == 32768
    assert response["diagnostic"]["source"] == "native"
    assert "机密合同正文" not in caplog.text


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


def test_set_log_fields_merges_and_removes():
    """set_log_fields 即时合并字段,None 移除;仅影响当前执行链。"""
    from document_comparison.observability import get_log_context, set_log_fields

    assert get_log_context() == {}
    with log_context(task_id="task-a", task_kind="compare"):
        set_log_fields(step="ocr")
        assert get_log_context() == {"task_id": "task-a", "task_kind": "compare", "step": "ocr"}
        set_log_fields(step="align")  # 覆盖同名
        assert get_log_context()["step"] == "align"
        set_log_fields(step=None)  # None 移除
        assert "step" not in get_log_context()
    assert get_log_context() == {}


def test_log_format_renders_biz_step_tag(caplog):
    """日志行带【业务-步骤】标注:任务阶段日志 biz-step,任务级仅业务,其它为 -。"""
    import logging as _logging

    from document_comparison.logging_config import _ContextFilter, _FMT

    formatter = _logging.Formatter(_FMT)
    record = _logging.LogRecord(
        "doc.test", _logging.INFO, __file__, 1, "hello", None, None
    )
    _ContextFilter().filter(record)
    assert formatter.format(record).count("【-】") == 1  # 无上下文

    with log_context(task_id="t1", task_kind="compare"):
        with log_context(step="ocr"):
            record = _logging.LogRecord(
                "doc.test", _logging.INFO, __file__, 1, "hello", None, None
            )
            _ContextFilter().filter(record)
            assert "【compare-ocr】" in formatter.format(record)
        record = _logging.LogRecord(
            "doc.test", _logging.INFO, __file__, 1, "hello", None, None
        )
        _ContextFilter().filter(record)
        assert "【compare】" in formatter.format(record)


def test_step_label_maps_pipeline_stages():
    """进度 stage 名 → 步骤标签:主映射沿用计时桶,对帐单细分,任务级不切换。"""
    from document_comparison.tasks import _step_label

    assert _step_label("word_parsing") == "word"
    assert _step_label("ocr") == "ocr"
    assert _step_label("structure_done") == "structure"
    assert _step_label("align") == "align"
    assert _step_label("compare") == "compare"
    assert _step_label("normalize") == "normalize"
    assert _step_label("diff") == "diff"
    assert _step_label("statement_start") == "start"
    assert _step_label("statement_aggregate") == "aggregate"
    assert _step_label("statement_file_2") == "file-2"
    assert _step_label("statement_file_3_done") == "file-3"
    # 任务级事件不切换步骤
    assert _step_label("start") is None
    assert _step_label("done") is None
    assert _step_label("failed") is None


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


# —— DEBUG 级完整请求/响应体日志 ——


def _msg_text(records):
    return " ".join(r.getMessage() for r in records)


def test_debug_logs_full_request_body(quiet_logger, caplog):
    """DEBUG 级别下,log_model_request 额外输出完整脱敏请求体(含提示词文本)。"""
    with caplog.at_level(logging.DEBUG, logger="test_observability"):
        log_model_request(
            quiet_logger, "ocr", "http://x/v1/chat/completions",
            {"model": "m", "messages": [{"content": "原始合同条款原文"}]}, 1,
        )
    body_lines = [r for r in caplog.records if r.getMessage().startswith("model request body")]
    assert body_lines, "DEBUG 级应输出 model request body 行"
    assert "原始合同条款原文" in body_lines[0].getMessage()


def test_info_level_omits_full_request_body(quiet_logger, caplog):
    """INFO 级别下不输出完整请求体(只输出摘要),避免合同原文进入默认日志。"""
    with caplog.at_level(logging.INFO, logger="test_observability"):
        log_model_request(
            quiet_logger, "ocr", "http://x/v1", {"messages": [{"content": "SECRET_TEXT"}]}, 1,
        )
    text = _msg_text(caplog.records)
    assert "model request kind=ocr" in text  # 摘要行仍在
    assert "SECRET_TEXT" not in text          # 完整 body 不进 INFO 日志


def test_debug_logs_full_response_body(quiet_logger, caplog):
    """DEBUG 级别下,log_model_response 输出完整响应体。"""
    with caplog.at_level(logging.DEBUG, logger="test_observability"):
        t = log_model_request(quiet_logger, "judge", "http://x/v1", {"model": "m"}, 1)
        log_model_response(
            quiet_logger, "judge", 200,
            {"choices": [{"message": {"content": "风险结论: high"}}]}, t,
        )
    body_lines = [r for r in caplog.records if r.getMessage().startswith("model response body")]
    assert body_lines
    assert "风险结论: high" in body_lines[0].getMessage()


def test_debug_skips_embedding_response_body(quiet_logger, caplog):
    """embedding 响应是纯向量数组,DEBUG 级也不输出完整 body(体积大、无可读性)。"""
    with caplog.at_level(logging.DEBUG, logger="test_observability"):
        t = log_model_request(quiet_logger, "embedding", "http://x/v1", {"model": "m"}, 1)
        log_model_response(
            quiet_logger, "embedding", 200,
            {"data": [{"embedding": [0.1, 0.2, 0.3]}]}, t,
        )
    text = _msg_text(caplog.records)
    assert "model response body kind=embedding" not in text
    assert "[0.1" not in text


def test_debug_logs_failure_response_body(quiet_logger, caplog):
    """DEBUG 级别下,log_model_failure 也输出完整响应体(embedding 仍跳过)。"""
    with caplog.at_level(logging.DEBUG, logger="test_observability"):
        t = log_model_request(quiet_logger, "statement-column", "http://x/v1", {"model": "m"}, 1)
        log_model_failure(
            quiet_logger, "statement-column", t, "HTTP 400", status_code=400,
            response={"error": {"message": "invalid column"}},
        )
    body_lines = [r for r in caplog.records if r.getMessage().startswith("model failure body")]
    assert body_lines
    assert "invalid column" in body_lines[0].getMessage()
