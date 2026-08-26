"""llm_protocol 协议适配单测 + 各调用点新协议接入冒烟(不访问真实网络)。

覆盖:build_chat_request 三协议的 URL/认证头/payload 转换、parse_chat_content
三协议的响应提取,以及 LLMOCREngine 与 llm_diff 通道按 anthropic / responses
协议发送的端到端断言(MockTransport)。judge / alignment / statement 通道与
llm_diff 共用同一组 helper,请求构造逻辑等价,不重复覆盖。
"""
from __future__ import annotations

import json

import httpx

from document_comparison.config import settings
from document_comparison.llm_protocol import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    build_chat_request,
    parse_chat_content,
)
from document_comparison.ocr.llm import LLMOCREngine


def _openai_style_payload(data_url: str = "data:image/png;base64,eA==") -> dict:
    """调用点现有构造方式的 OpenAI Chat Completions 规范形态。"""
    return {
        "model": "m1",
        "messages": [
            {"role": "system", "content": "SYS"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "请识别"},
                    {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}},
                ],
            },
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "chat_template_kwargs": {"enable_thinking": False},
    }


# —— build_chat_request ——

def test_openai_protocol_keeps_existing_wire_format():
    payload = _openai_style_payload()
    url, headers, wire = build_chat_request(
        api_base="http://x/v1/", api_key="k", payload=payload, protocol="openai"
    )
    assert url == "http://x/v1/chat/completions"
    assert headers == {"Authorization": "Bearer k"}
    assert wire is payload  # 原样透传,历史行为不变


def test_openai_empty_key_omits_bearer_header():
    url, headers, _ = build_chat_request(
        api_base="http://x/v1", api_key="", payload=_openai_style_payload()
    )
    assert url == "http://x/v1/chat/completions"
    assert headers == {}


def test_anthropic_build_request():
    url, headers, wire = build_chat_request(
        api_base="https://api.anthropic.com/v1/",
        api_key="sk-ant",
        payload=_openai_style_payload(),
        protocol="anthropic",
    )
    assert url == "https://api.anthropic.com/v1/messages"
    assert headers["x-api-key"] == "sk-ant"
    assert headers["anthropic-version"] == "2023-06-01"

    assert wire["system"] == "SYS"
    msg = wire["messages"][0]
    assert msg["role"] == "user"
    text_block, image_block = msg["content"]
    assert text_block == {"type": "text", "text": "请识别"}
    assert image_block == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "eA=="},
    }
    assert wire["max_tokens"] == DEFAULT_MAX_OUTPUT_TOKENS
    assert wire["temperature"] == 0
    assert "response_format" not in wire
    assert "chat_template_kwargs" not in wire


def test_anthropic_empty_key_omits_x_api_key():
    _, headers, _ = build_chat_request(
        api_base="http://gw/v1", api_key="", payload=_openai_style_payload(),
        protocol="anthropic",
    )
    assert "x-api-key" not in headers
    assert headers["anthropic-version"] == "2023-06-01"


def test_anthropic_http_image_url_becomes_url_source():
    payload = _openai_style_payload(data_url="https://cdn.test/a.png")
    _, _, wire = build_chat_request(
        api_base="http://x/v1", api_key="k", payload=payload, protocol="anthropic"
    )
    image_block = wire["messages"][0]["content"][1]
    assert image_block == {
        "type": "image",
        "source": {"type": "url", "url": "https://cdn.test/a.png"},
    }


def test_responses_build_request():
    url, headers, wire = build_chat_request(
        api_base="http://x/v1/",
        api_key="k",
        payload=_openai_style_payload(),
        protocol="openai_responses",
    )
    assert url == "http://x/v1/responses"
    assert headers == {"Authorization": "Bearer k"}

    assert wire["instructions"] == "SYS"
    item = wire["input"][0]
    assert item["role"] == "user"
    text_part, image_part = item["content"]
    assert text_part == {"type": "input_text", "text": "请识别"}
    assert image_part == {
        "type": "input_image",
        "image_url": "data:image/png;base64,eA==",
        "detail": "high",
    }
    assert wire["text"] == {"format": {"type": "json_object"}}
    assert wire["max_output_tokens"] == DEFAULT_MAX_OUTPUT_TOKENS
    assert "chat_template_kwargs" not in wire


# —— parse_chat_content ——

def test_parse_openai_choices():
    data = {"choices": [{"message": {"content": "hi"}}]}
    assert parse_chat_content(data) == "hi"


def test_parse_anthropic_joins_text_blocks_and_skips_thinking():
    data = {
        "content": [
            {"type": "thinking", "thinking": "internal"},
            {"type": "text", "text": "部分1"},
            {"type": "text", "text": "部分2"},
        ],
        "stop_reason": "end_turn",
    }
    assert parse_chat_content(data, protocol="anthropic") == "部分1部分2"


def test_parse_anthropic_strips_think_blocks():
    data = {
        "content": [{"type": "text", "text": '<think>推理…</think>{"blocks": []}'}],
        "stop_reason": "end_turn",
    }
    assert parse_chat_content(data, protocol="anthropic") == '{"blocks": []}'


def test_parse_anthropic_strips_unclosed_think_block():
    data = {
        "content": [{"type": "text", "text": '<think>未闭合思考\n{"blocks": []}'}],
        "stop_reason": "end_turn",
    }
    assert parse_chat_content(data, protocol="anthropic") == '{"blocks": []}'


def test_parse_openai_keeps_think_blocks_untouched():
    """openai 路径保持逐字节不变(现有行为,由 vLLM 开关控制思考链)。"""
    data = {"choices": [{"message": {"content": "<think>x</think>y"}}]}
    assert parse_chat_content(data) == "<think>x</think>y"


def test_parse_responses_joins_message_output_text_only():
    data = {
        "status": "completed",
        "output": [
            {"type": "reasoning", "summary": []},
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "a"},
                    {"type": "output_text", "text": "b"},
                ],
            },
        ],
    }
    assert parse_chat_content(data, protocol="openai_responses") == "ab"


def test_parse_anthropic_and_responses_return_content_when_truncated():
    """截断只记 warning,内容照常返回,由消费端 JSON 容错解析兜底。"""
    anth = {
        "content": [{"type": "text", "text": '{"blocks": [1,'}],
        "stop_reason": "max_tokens",
    }
    assert parse_chat_content(anth, protocol="anthropic") == '{"blocks": [1,'
    resp = {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "部分"}]}
        ],
    }
    assert parse_chat_content(resp, protocol="openai_responses") == "部分"


# —— 调用点接入(MockTransport 端到端)——

def test_ocr_engine_anthropic_end_to_end():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            request=request,
            json={
                "content": [{"type": "text", "text": '{"blocks": []}'}],
                "stop_reason": "end_turn",
            },
        )

    engine = LLMOCREngine(
        api_base="https://claude.test/v1",
        api_key="sk-ant-1",
        model="claude-sonnet",
        max_retries=0,
        api_protocol="anthropic",
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        content = engine._chat("data:image/png;base64,eA==", client=client)

    assert content == '{"blocks": []}'
    assert captured["url"] == "https://claude.test/v1/messages"
    assert captured["headers"]["x-api-key"] == "sk-ant-1"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert "authorization" not in captured["headers"]
    body = captured["body"]
    assert body["system"]
    image_block = body["messages"][0]["content"][1]
    assert image_block["source"] == {
        "type": "base64", "media_type": "image/png", "data": "eA==",
    }


def test_ocr_engine_responses_end_to_end():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            request=request,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": '{"blocks": []}'}],
                    }
                ],
            },
        )

    engine = LLMOCREngine(
        api_base="http://newgw.test/v1",
        api_key="gk",
        model="gpt-test",
        max_retries=0,
        api_protocol="openai_responses",
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        content = engine._chat("data:image/png;base64,eA==", client=client)

    assert content == '{"blocks": []}'
    assert captured["url"] == "http://newgw.test/v1/responses"
    assert captured["headers"]["authorization"] == "Bearer gk"
    body = captured["body"]
    assert body["instructions"]
    image_part = body["input"][0]["content"][1]
    assert image_part == {
        "type": "input_image",
        "image_url": "data:image/png;base64,eA==",
        "detail": "high",
    }


def test_llm_diff_post_judge_chat_anthropic(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            request=request,
            json={
                "content": [{"type": "text", "text": '{"hunks": []}'}],
                "stop_reason": "end_turn",
            },
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        "document_comparison.compare.llm_diff.httpx.Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr(settings, "judge_api_base", "https://judge.test/v1")
    monkeypatch.setattr(settings, "judge_api_key", "jk")
    monkeypatch.setattr(settings, "judge_model", "txt")
    monkeypatch.setattr(settings, "judge_api_protocol", "anthropic")
    monkeypatch.setattr(settings, "judge_timeout", 5)
    monkeypatch.setattr(settings, "llm_max_retries", 0)

    from document_comparison.compare.llm_diff import _post_judge_chat

    assert _post_judge_chat("SYS", "USER") == '{"hunks": []}'
    assert captured["url"] == "https://judge.test/v1/messages"
    assert captured["body"]["system"] == "SYS"
    assert captured["body"]["max_tokens"] == DEFAULT_MAX_OUTPUT_TOKENS
