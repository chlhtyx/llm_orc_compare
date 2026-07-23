"""Webhook 回调交付测试。"""
import json

import pytest

from document_comparison.webhook import build_event, deliver


def test_build_event_includes_event_id():
    event, raw = build_event("t1", "done", {"overall_risk": "high"})
    assert "event_id" in event
    assert event["task_id"] == "t1"
    # raw 与 event 内容一致
    parsed = json.loads(raw.decode())
    assert parsed["event_id"] == event["event_id"]
    assert parsed["status"] == "done"


async def test_deliver_sends_headers_and_body(monkeypatch):
    """deliver 应 POST 原始 body 并带上 Content-Type / X-Event-Id(不带签名头)。"""
    captured: dict = {}

    class _Resp:
        status_code = 200

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content=None, headers=None):
            captured["headers"] = headers
            captured["body"] = content
            return _Resp()

    monkeypatch.setattr("document_comparison.webhook.httpx.AsyncClient", _Client)

    result = await deliver(
        "http://hook", b'{"x":1}', "evt-1", max_retries=1, timeout=1.0
    )
    assert result["success"] is True
    headers = captured["headers"]
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Event-Id"] == "evt-1"
    assert "X-Signature" not in headers  # 签名头已移除
    assert captured["body"] == b'{"x":1}'
