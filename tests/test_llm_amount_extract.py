"""金额统计 — LLM 兜底金额抽取单元测试。

mock LLMOCREngine._post_chat,验证:
  - 发票纯数字金额抽取 + grounding 校验通过/拒绝
  - 非 JSON / 字段非法 / 调用异常 → None/空
不发起真实网络请求。
"""
from unittest.mock import MagicMock

from document_comparison.statement import llm_amount_extract as lae


def _patch_post_chat(monkeypatch, fake_content):
    """让 _post_chat 返回固定文本。"""
    monkeypatch.setattr(
        "document_comparison.ocr.llm.LLMOCREngine._post_chat",
        lambda self, payload, kind: fake_content,
    )


# —— grounding 通过 / 拒绝 ——

def test_extract_invoice_amounts_grounding_pass(monkeypatch):
    """发票纯数字金额,在 OCR 文本里能逐字溯源 → 抽取成功。"""
    fake = '{"items": [{"amount": "1680.00"}, {"amount": "640.00"}]}'
    _patch_post_chat(monkeypatch, fake)
    grounding = "货物名称 金额\nA 1680.00\nB 640.00"  # 两个值都在原文

    items = lae.llm_extract_amounts(
        b"fake-png", ["货物名称", "金额"],
        [["A", "1680.00"], ["B", "640.00"]],
        grounding,
    )
    assert items is not None
    assert len(items) == 2
    values = sorted(it.value for it in items)
    assert values == [640.0, 1680.0]
    # canonical 归一化(去尾零)
    assert items[0].canonical.startswith("CNY:")
    # 列名应取表头里的"金额"
    assert all(it.column == "金额" for it in items)


def test_thousand_separator_grounding(monkeypatch):
    """千分位金额:LLM 返回 "1,680.00",OCR 文本含 "1,680.00" → 归一化后匹配。"""
    fake = '{"items": [{"amount": "1,680.00"}]}'
    _patch_post_chat(monkeypatch, fake)
    grounding = "金额\n1,680.00"
    items = lae.llm_extract_amounts(
        b"fake-png", ["金额"], [["1,680.00"]], grounding,
    )
    assert items and items[0].value == 1680.0


def test_grounding_reject_hallucinated(monkeypatch):
    """LLM 编造 OCR 文本里没有的金额 → grounding 拒绝 → 空 list。"""
    fake = '{"items": [{"amount": "99999.00"}]}'  # OCR 文本里没有 99999
    _patch_post_chat(monkeypatch, fake)
    grounding = "金额\n1680.00"  # 只有 1680
    items = lae.llm_extract_amounts(
        b"fake-png", ["金额"], [["1680.00"]], grounding,
    )
    assert items == []


def test_grounding_reject_unrelated_number(monkeypatch):
    """grounding 通过但模型误抽(数量/单价不应被抽)——本测试只验 grounding 逻辑:
    OCR 文本里含的数会通过 grounding,但 prompt 已要求排除非金额列;
    这里确认一个确实不在原文的值会被拒。"""
    fake = '{"items": [{"amount": "85.00"}, {"amount": "12345.00"}]}'
    _patch_post_chat(monkeypatch, fake)
    grounding = "数量 单价 金额\n20 85.00 1680.00"  # 85 在,12345 不在
    items = lae.llm_extract_amounts(
        b"fake-png", ["数量", "单价", "金额"],
        [["20", "85.00", "1680.00"]], grounding,
    )
    # 只有 85.00 通过 grounding,12345.00 被拒
    assert items is not None
    assert len(items) == 1
    assert items[0].value == 85.0


# —— 容错 / 降级 ——

def test_invalid_json_returns_none(monkeypatch):
    _patch_post_chat(monkeypatch, "这不是JSON")
    items = lae.llm_extract_amounts(
        b"fake-png", ["金额"], [["1680.00"]], "1680.00",
    )
    assert items is None


def test_empty_items_returns_empty_list(monkeypatch):
    """LLM 明确判定无金额 → 空 list(非 None)。"""
    _patch_post_chat(monkeypatch, '{"items": []}')
    items = lae.llm_extract_amounts(
        b"fake-png", ["项目"], [["说明"]], "说明",
    )
    assert items == []


def test_non_string_amount_filtered(monkeypatch):
    """amount 非字符串/不可解析 → 该条丢弃。"""
    fake = '{"items": [{"amount": 1680}, {"amount": "abc"}, {"amount": "640.00"}]}'
    _patch_post_chat(monkeypatch, fake)
    grounding = "1680 640.00"
    items = lae.llm_extract_amounts(
        b"fake-png", ["金额"], [["640.00"]], grounding,
    )
    assert items is not None
    assert len(items) == 1
    assert items[0].value == 640.0


def test_negative_amount_filtered(monkeypatch):
    """负数金额被拒。"""
    fake = '{"items": [{"amount": "-1680.00"}]}'
    _patch_post_chat(monkeypatch, fake)
    grounding = "-1680.00"
    items = lae.llm_extract_amounts(
        b"fake-png", ["金额"], [["-1680.00"]], grounding,
    )
    assert items == []


def test_call_exception_returns_none(monkeypatch):
    """_post_chat 抛异常 → None(调用方标 needs_review)。"""
    def boom(self, payload, kind):
        raise RuntimeError("network down")
    monkeypatch.setattr(
        "document_comparison.ocr.llm.LLMOCREngine._post_chat", boom,
    )
    items = lae.llm_extract_amounts(
        b"fake-png", ["金额"], [["1680.00"]], "1680.00",
    )
    assert items is None


def test_no_png_returns_none(monkeypatch):
    """无 PNG 字节 → None(不调 LLM)。"""
    items = lae.llm_extract_amounts(
        b"", ["金额"], [["1680.00"]], "1680.00",
    )
    assert items is None


def test_no_rows_returns_none(monkeypatch):
    """无数据行 → None。"""
    items = lae.llm_extract_amounts(
        b"fake-png", ["金额"], [], "1680.00",
    )
    assert items is None


# —— 请求体 ——

def test_request_kind_and_temperature(monkeypatch):
    """请求 kind=statement-amount, temperature=0。"""
    captured: dict = {}

    def fake_post(self, payload, kind):
        captured["payload"] = payload
        captured["kind"] = kind
        return '{"items": [{"amount": "1680.00"}]}'

    monkeypatch.setattr(
        "document_comparison.ocr.llm.LLMOCREngine._post_chat", fake_post,
    )
    lae.llm_extract_amounts(
        b"fake-png", ["金额"], [["1680.00"]], "1680.00",
    )
    assert captured["kind"] == "statement-amount"
    assert captured["payload"]["temperature"] == 0
    assert captured["payload"]["chat_template_kwargs"]["enable_thinking"] is False
    assert "model" in captured["payload"]
    system_prompt = captured["payload"]["messages"][0]["content"]
    assert "两列的数据行数值都要抽出" in system_prompt
    assert "只抽这一列" in system_prompt


# —— 列名选择 ——

def test_column_name_fallback_when_no_amount_header(monkeypatch):
    """表头无金额列名时,column 兜底为 "金额(llm)"。"""
    _patch_post_chat(monkeypatch, '{"items": [{"amount": "1680.00"}]}')
    grounding = "1680.00"
    items = lae.llm_extract_amounts(
        b"fake-png", ["日期", "备注"], [["2024", "1680.00"]], grounding,
    )
    assert items and items[0].column == "金额(llm)"
