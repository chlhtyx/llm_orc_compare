"""流水线与模型调用的统一可观测性日志。"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from contextlib import contextmanager
from copy import deepcopy
from typing import Any, Iterator


def _safe_model_value(value: Any) -> Any:
    """保留模型参数语义，但避免把体积巨大的图片 Base64 写入日志。"""
    copied = deepcopy(value)

    def scrub(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: scrub(val) for key, val in item.items()}
        if isinstance(item, list):
            return [scrub(val) for val in item]
        if isinstance(item, str) and item.startswith("data:image/"):
            header, _, encoded = item.partition(",")
            return {
                "data_url": header,
                "base64_chars": len(encoded),
                "sha256": hashlib.sha256(encoded.encode("ascii")).hexdigest(),
            }
        return item

    return scrub(copied)


def log_model_request(
    logger: logging.Logger, kind: str, url: str, payload: dict[str, Any], attempt: int
) -> float:
    """记录不含鉴权头的模型请求，返回计时起点。"""
    logger.info(
        "model request kind=%s attempt=%s url=%s payload=%s",
        kind,
        attempt,
        url,
        json.dumps(_safe_model_value(payload), ensure_ascii=False, separators=(",", ":")),
    )
    return time.perf_counter()


def log_model_response(
    logger: logging.Logger,
    kind: str,
    status_code: int,
    value: Any,
    started_at: float,
) -> None:
    """记录模型原始返回值与单次 HTTP 调用耗时。"""
    logger.info(
        "model response kind=%s status=%s elapsed=%.3fs value=%s",
        kind,
        status_code,
        time.perf_counter() - started_at,
        json.dumps(_safe_model_value(value), ensure_ascii=False, separators=(",", ":")),
    )


@contextmanager
def timed_stage(logger: logging.Logger, stage: str, **fields: Any) -> Iterator[None]:
    """统一记录阶段开始、成功耗时与异常耗时。"""
    suffix = " ".join(f"{key}={value}" for key, value in fields.items())
    logger.info("stage start stage=%s%s", stage, f" {suffix}" if suffix else "")
    started_at = time.perf_counter()
    try:
        yield
    except Exception:
        logger.exception(
            "stage failed stage=%s elapsed=%.3fs%s",
            stage,
            time.perf_counter() - started_at,
            f" {suffix}" if suffix else "",
        )
        raise
    logger.info(
        "stage done stage=%s elapsed=%.3fs%s",
        stage,
        time.perf_counter() - started_at,
        f" {suffix}" if suffix else "",
    )
