"""对帐单金额统计 — LLM 兜底列指认单元测试。

mock LLMOCREngine._post_chat,验证 JSON 解析与失败降级路径。
不发起真实网络请求。
"""
from unittest.mock import MagicMock

from document_comparison.statement import llm_column_detect as lcd


def test_parse_valid_json(monkeypatch):
    """LLM 返回合法 JSON → 正确解析 {col_index: role}。"""
    fake_content = '{"columns": [{"index": 1, "role": "amount"}, {"index": 2, "role": "paid"}]}'
    monkeypatch.setattr(
        "document_comparison.ocr.llm.LLMOCREngine._post_chat",
        lambda self, payload, kind: fake_content,
    )
    result = lcd.llm_detect_amount_columns(b"fake-png", ["日期", "金额", "已付"])
    assert result == {1: "amount", 2: "paid"}


def test_request_includes_model_parameter(monkeypatch):
    """对帐单列定位请求必须显式带模型名，便于复现并兼容严格服务端。"""
    captured: dict = {}

    def fake_post(self, payload, kind):
        captured["payload"] = payload
        captured["kind"] = kind
        return '{"columns": [{"index": 0, "role": "amount"}]}'

    monkeypatch.setattr(
        "document_comparison.ocr.llm.LLMOCREngine._post_chat", fake_post,
    )
    result = lcd.llm_detect_amount_columns(b"fake-png", ["金额"])

    assert result == {0: "amount"}
    assert captured["kind"] == "statement-column"
    assert "model" in captured["payload"]


def test_parse_json_with_code_fence(monkeypatch):
    """LLM 返回带 ```json 包裹的 JSON → 仍能解析。"""
    fake_content = '```json\n{"columns": [{"index": 0, "role": "amount"}]}\n```'
    monkeypatch.setattr(
        "document_comparison.ocr.llm.LLMOCREngine._post_chat",
        lambda self, payload, kind: fake_content,
    )
    result = lcd.llm_detect_amount_columns(b"fake-png", ["金额", "日期"])
    assert result == {0: "amount"}


def test_parse_invalid_json_returns_none(monkeypatch):
    """LLM 返回非 JSON → None。"""
    monkeypatch.setattr(
        "document_comparison.ocr.llm.LLMOCREngine._post_chat",
        lambda self, payload, kind: "这不是JSON",
    )
    result = lcd.llm_detect_amount_columns(b"fake-png", ["金额"])
    assert result is None


def test_parse_empty_columns_returns_empty_dict(monkeypatch):
    """LLM 明确判定无金额列 → 空 dict(非 None)。"""
    fake_content = '{"columns": []}'
    monkeypatch.setattr(
        "document_comparison.ocr.llm.LLMOCREngine._post_chat",
        lambda self, payload, kind: fake_content,
    )
    result = lcd.llm_detect_amount_columns(b"fake-png", ["日期", "项目"])
    assert result == {}


def test_invalid_role_filtered(monkeypatch):
    """role 不在白名单 → 该条目被过滤。"""
    fake_content = '{"columns": [{"index": 0, "role": "money"}, {"index": 1, "role": "amount"}]}'
    monkeypatch.setattr(
        "document_comparison.ocr.llm.LLMOCREngine._post_chat",
        lambda self, payload, kind: fake_content,
    )
    result = lcd.llm_detect_amount_columns(b"fake-png", ["日期", "金额"])
    assert result == {1: "amount"}


def test_index_out_of_range_filtered(monkeypatch):
    """col_index 越界 → 该条目被过滤。"""
    fake_content = '{"columns": [{"index": 99, "role": "amount"}]}'
    monkeypatch.setattr(
        "document_comparison.ocr.llm.LLMOCREngine._post_chat",
        lambda self, payload, kind: fake_content,
    )
    result = lcd.llm_detect_amount_columns(b"fake-png", ["金额"])
    assert result == {}


def test_post_chat_exception_returns_none(monkeypatch):
    """_post_chat 抛异常(超时/网络)→ 返回 None(调用方标 needs_review)。"""
    def raise_exc(self, payload, kind):
        raise RuntimeError("timeout")
    monkeypatch.setattr(
        "document_comparison.ocr.llm.LLMOCREngine._post_chat", raise_exc
    )
    result = lcd.llm_detect_amount_columns(b"fake-png", ["金额"])
    assert result is None


def test_empty_png_returns_none():
    """无 PNG 字节 → 直接返回 None,不调 LLM。"""
    assert lcd.llm_detect_amount_columns(b"", ["金额"]) is None


def test_empty_headers_returns_none():
    """无表头 → 直接返回 None。"""
    assert lcd.llm_detect_amount_columns(b"fake-png", []) is None
