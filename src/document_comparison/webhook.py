"""Webhook 回调交付:HMAC-SHA256 签名 + 指数退避重试(§14.3)。"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from typing import TypedDict

import httpx

logger = logging.getLogger(__name__)


class DeliveryResult(TypedDict):
    """deliver 的结构化返回值,供调用方回写 callback 状态。"""

    success: bool
    http_status: int | None
    error: str | None


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
) -> DeliveryResult:
    """POST 事件到 url,带 X-Signature;失败按指数退避重试。

    返回 DeliveryResult(成功与否 / 最终 HTTP 状态码 / 错误原因),
    供调用方回写 task_records 的 callback 交付状态。
    """
    signature = sign(raw_body, secret)
    headers = {
        "Content-Type": "application/json",
        "X-Signature": signature,
        "X-Event-Id": event_id,
    }
    backoff = (1, 4, 16)  # 秒
    logger.info("webhook deliver start event_id=%s url=%s", event_id, url)
    last_status: int | None = None
    last_error: str | None = None
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, content=raw_body, headers=headers)
            last_status = resp.status_code
            last_error = None
            if resp.status_code < 300:
                logger.info("webhook delivered event_id=%s status=%s", event_id, resp.status_code)
                return DeliveryResult(success=True, http_status=resp.status_code, error=None)
            logger.warning(
                "webhook non-2xx event_id=%s status=%s attempt=%s/%s",
                event_id, resp.status_code, attempt + 1, max_retries,
            )
            last_error = f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001 — 网络/超时等可重试故障,记录后退避
            last_status = None
            last_error = str(exc) or exc.__class__.__name__
            logger.warning(
                "webhook request error event_id=%s attempt=%s/%s reason=%s",
                event_id, attempt + 1, max_retries, exc,
            )
        if attempt < max_retries - 1:
            wait = backoff[min(attempt, len(backoff) - 1)]
            logger.info("webhook retry event_id=%s after %ss", event_id, wait)
            await asyncio.sleep(wait)
    logger.error("webhook give up event_id=%s url=%s after %s attempts", event_id, url, max_retries)
    return DeliveryResult(success=False, http_status=last_status, error=last_error)


def verify(raw_body: bytes, secret: str, signature: str) -> bool:
    """调用方可用于校验签名。"""
    expected = sign(raw_body, secret)
    return hmac.compare_digest(expected, signature)
