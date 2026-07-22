"""Webhook 签名与验证测试。"""
import json

import pytest

from document_comparison.webhook import build_event, deliver, sign, verify


def test_sign_deterministic():
    body = b'{"x":1}'
    assert sign(body, "secret") == sign(body, "secret")


def test_sign_differs_by_secret():
    body = b'{"x":1}'
    assert sign(body, "a") != sign(body, "b")


def test_verify_roundtrip():
    body = b'{"task_id":"t1","status":"done"}'
    sig = sign(body, "secret")
    assert verify(body, "secret", sig) is True
    assert verify(body, "wrong", sig) is False


def test_build_event_includes_event_id():
    event, raw = build_event("t1", "done", {"overall_risk": "high"})
    assert "event_id" in event
    assert event["task_id"] == "t1"
    # raw 与 event 一致(可验签)
    parsed = json.loads(raw.decode())
    assert parsed["event_id"] == event["event_id"]
    assert verify(raw, "secret", sign(raw, "secret"))


@pytest.mark.parametrize("secret", ["s", None])
async def test_deliver_signature_header_only_when_secret_present(secret, monkeypatch):
    """有 secret 时带 X-Signature;secret 为 None 时省略签名头,但仍正常投递。"""
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
        "http://hook", b'{"x":1}', secret, "evt-1", max_retries=1, timeout=1.0
    )
    assert result["success"] is True
    headers = captured["headers"]
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Event-Id"] == "evt-1"
    if secret:
        assert "X-Signature" in headers
        assert headers["X-Signature"] == sign(b'{"x":1}', secret)
    else:
        assert "X-Signature" not in headers
