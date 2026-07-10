"""Webhook 回调交付:HMAC-SHA256 签名 + 指数退避重试(§14.3)。"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid

import httpx


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def build_event(task_id: str, status: str, payload: dict) -> tuple[dict, bytes]:
    """构造事件:返回 (event_dict, 用于签名的 raw body bytes)。"""
    event = {
        "event_id": str(uuid.uuid4()),
        "task_id": task_id,
        "status": status,
        **payload,
    }
    raw = json.dumps(event, ensure_ascii=False).encode("utf-8")
    return event, raw


async def deliver(
    url: str,
    raw_body: bytes,
    secret: str,
    event_id: str,
    *,
    max_retries: int = 3,
    timeout: float = 10.0,
) -> bool:
    """POST 事件到 url,带 X-Signature;失败按指数退避重试。返回是否成功。"""
    signature = sign(raw_body, secret)
    headers = {
        "Content-Type": "application/json",
        "X-Signature": signature,
        "X-Event-Id": event_id,
    }
    backoff = (1, 4, 16)  # 秒
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, content=raw_body, headers=headers)
            if resp.status_code < 300:
                return True
        except Exception:
            pass
        if attempt < max_retries - 1:
            await asyncio.sleep(backoff[min(attempt, len(backoff) - 1)])
    return False


def verify(raw_body: bytes, secret: str, signature: str) -> bool:
    """调用方可用于校验签名。"""
    expected = sign(raw_body, secret)
    return hmac.compare_digest(expected, signature)
