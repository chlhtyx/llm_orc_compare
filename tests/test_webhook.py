"""Webhook 签名与验证测试。"""
import json

from document_comparison.webhook import build_event, sign, verify


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
