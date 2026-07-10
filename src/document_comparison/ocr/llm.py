"""多模态 LLM OCR 引擎:通过 OpenAI 兼容的 Chat Completions API 识别 PDF 版面。

设计目标:
- 无需本地 GPU / PaddlePaddle,任意可访问多模态 LLM 的环境即可部署。
- 兼容 OpenAI 协议(base_url 可指向 OpenAI / Azure / 本地 vLLM / SGLang 等)。
- 每页 PDF 渲染为 PNG,以 image_url 发多模态请求,要求 LLM 返回 JSON 版面块。
- 输出统一为 Block(bbox 为 PDF 点坐标),与 mock 引擎一致。

配置(统一持久化于 .dc_data/llm_config.json,在 UI 设置页维护):
- llm_api_base           API 根地址(兼容 OpenAI 协议)
- llm_api_key            API Key
- llm_model              多模态模型名
- llm_timeout            单次请求读取超时秒(默认 120;连接超时固定 10s)
- llm_max_concurrency    逐页并发数(默认 4)
- llm_max_retries        单页瞬态失败(超时 / 429 / 5xx)重试次数(默认 2)
"""
from __future__ import annotations

import base64
import json
import random
import re
from collections import deque
from pathlib import Path
import threading
import time
from typing import Any

import httpx

from ..config import settings
from ..models import Block, PageMeta
from ..parsing.pdf import render_pages
from .base import ProgressCb

# 要求 LLM 返回的 JSON 结构:
# {"blocks": [{"label": ..., "content": ..., "bbox": [x1,y1,x2,y2]}, ...]}
# bbox 用归一化 [0,1] 坐标(相对页面),再由本引擎换算回 PDF 点坐标。
_SYSTEM_PROMPT = (
    "你是一个文档版面分析助手。给定一页文档图片,识别其中所有可读的版面块,"
    "包括标题、段落、表格、列表、印章等。"
    "严格只输出 JSON,不要解释、不要 markdown 代码块。"
    "JSON 结构为:{\"blocks\": [{\"label\": str, \"content\": str, \"bbox\": [x1,y1,x2,y2]}]}。"
    "label 取值:text / doc_title / paragraph_title / table / list / seal。"
    "bbox 为该块在页面中的归一化边界框,坐标范围 [0,1],原点左上角。"
    "content 为该块的完整文字内容(表格用 markdown 或 TSV 表达)。"
)


class LLMOCREngine:
    """通过多模态 LLM API 识别 PDF 版面。

    坐标系约定:LLM 返回归一化 [0,1] bbox,本引擎按页面尺寸换算为
    PDF 点坐标(pt,72 DPI),与 pdf.js viewport 对齐(同 mock 引擎)。
    """

    def __init__(
        self,
        api_base: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        max_concurrency: int | None = None,
        max_retries: int | None = None,
    ) -> None:
        self.api_base = api_base or settings.llm_api_base
        self.api_key = api_key if api_key is not None else settings.llm_api_key
        self.model = model or settings.llm_model
        self.timeout = timeout if timeout is not None else settings.llm_timeout
        self.max_concurrency = (
            max_concurrency if max_concurrency is not None else settings.llm_max_concurrency
        )
        self.max_retries = (
            max_retries if max_retries is not None else settings.llm_max_retries
        )
        self._dpi = settings.pdf_render_dpi

    # —— 公共接口 ——
    def recognize(
        self,
        pdf_path: Path,
        page_metas: list[PageMeta],
        *,
        on_progress: ProgressCb | None = None,
    ) -> list[list[Block]]:
        images = render_pages(pdf_path, dpi=self._dpi)
        if not images:
            return []

        # Pre-warm certifi CA bundle before spawning threads — certifi.where()
        # uses an unlocked global guard that races under concurrent access.
        import certifi
        certifi.where()

        # 逐页识别(限并发)
        results: list[list[Block]] = [None] * len(images)  # type: ignore[list-item]
        total = len(images)
        done_count = [0]  # mutable counter for threads

        with _BoundedConcurrency(self.max_concurrency) as pool:
            for i, img in enumerate(images):
                pool.submit(
                    self._recognize_page,
                    i, img, page_metas[i], results,
                    on_progress, total, done_count,
                )

        return results

    # —— 单页识别 ——
    def _recognize_page(
        self,
        page_index: int,
        png_bytes: bytes,
        meta: PageMeta,
        out: list[list[Block]],
        on_progress: ProgressCb | None = None,
        total_pages: int = 1,
        done_count: list[int] | None = None,
    ) -> None:
        data_url = _to_data_url(png_bytes)
        content = self._chat(data_url)
        blocks = _parse_blocks(content, page_index, meta)
        out[page_index] = blocks
        if on_progress:
            done_count[0] += 1
            frac = 0.10 + (done_count[0] / total_pages) * 0.60
            on_progress("ocr", frac)

    def _chat(self, data_url: str) -> str:
        url = self.api_base.rstrip("/") + "/chat/completions"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "请识别这页文档的全部版面块,按要求的 JSON 格式输出。",
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url, "detail": "high"},
                        },
                    ],
                },
            ],
            # 结构化输出:降低温度,要求 JSON
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        # 多模态 OCR 单页推理可能很慢:连接快速失败,读取给予充分时间。
        timeout = httpx.Timeout(self.timeout, connect=10.0)
        # 可重试的瞬态故障:超时 / 网络传输错误 / 429 / 5xx。
        # (httpx.TransportError 覆盖 ConnectError / ReadTimeout / NetworkError 等)
        # 4xx(鉴权、参数错误等)不可重试,立即抛出。
        last_exc: Exception | None = None
        with httpx.Client(timeout=timeout) as client:
            for attempt in range(self.max_retries + 1):
                try:
                    resp = client.post(url, json=payload, headers=headers)
                except httpx.TransportError as exc:
                    last_exc = exc
                else:
                    if resp.status_code == 429 or resp.status_code >= 500:
                        last_exc = httpx.HTTPStatusError(
                            f"服务端瞬态错误:HTTP {resp.status_code}",
                            request=resp.request,
                            response=resp,
                        )
                    else:
                        resp.raise_for_status()  # 4xx:不可重试,直接抛
                        data = resp.json()
                        return data["choices"][0]["message"]["content"]
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 8) + random.random())
        assert last_exc is not None
        raise last_exc


# —— 工具函数 ——
def _to_data_url(png_bytes: bytes) -> str:
    b64 = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _parse_blocks(
    content: str, page_index: int, meta: PageMeta
) -> list[Block]:
    raw = _extract_json(content)
    items = []
    if isinstance(raw, dict):
        items = raw.get("blocks") or raw.get("items") or []
    elif isinstance(raw, list):
        items = raw

    blocks: list[Block] = []
    # 页面尺寸:优先用 PDF 点坐标;若为 0 退回像素(并按 DPI 反算)
    w_pt = meta.pdf_width_pt or (meta.width_px * 72.0 / settings.pdf_render_dpi)
    h_pt = meta.pdf_height_pt or (meta.height_px * 72.0 / settings.pdf_render_dpi)

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        text = (item.get("content") or item.get("text") or "").strip()
        if not text:
            continue
        label = item.get("label") or "text"
        # bbox 可能是归一化 [0,1],也可能是绝对像素;做一次宽松归一化
        bbox_norm = item.get("bbox") or item.get("box") or []
        bbox_pt = _to_pt_bbox(bbox_norm, w_pt, h_pt)
        blocks.append(
            Block(
                block_id=f"p{page_index}-b{idx}",
                page_index=page_index,
                label=label,
                bbox=bbox_pt,
                content=text,
            )
        )
    return blocks


def _to_pt_bbox(bbox: list[float], w_pt: float, h_pt: float) -> list[float]:
    if len(bbox) >= 4:
        x1, y1, x2, y2 = bbox[0], bbox[1], bbox[2], bbox[3]
        # 任一维度超过 1.5 视为绝对坐标,否则视为归一化
        if max(abs(x1), abs(y1), abs(x2), abs(y2)) > 1.5:
            return [float(x1), float(y1), float(x2), float(y2)]
        return [x1 * w_pt, y1 * h_pt, x2 * w_pt, y2 * h_pt]
    return []


def _extract_json(text: str) -> Any:
    """从可能混杂文本/代码块的回复中提取首个 JSON 对象或数组。"""
    text = text.strip()
    # 去 markdown 代码围栏
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 兜底:取首个 {...} 或 [...]
    for pat in (r"\{[\s\S]*\}", r"\[[\s\S]*\]"):
        m = re.search(pat, text)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                continue
    return {}


class _BoundedConcurrency:
    """极简线程池:submit 阻塞至有空闲槽位,退出时 join 全部。

    工作线程抛出的异常会被收集,并在 __exit__ 重新抛出,避免被 daemon
    线程默认 excepthook 静默吞掉(否则失败页会留下 None,污染下游结果)。
    """

    def __init__(self, limit: int) -> None:
        self._limit = max(1, limit)
        self._sem = threading.Semaphore(self._limit)
        self._threads: deque[threading.Thread] = deque()
        self._exc: list[BaseException] = []

    def submit(self, fn, *args) -> None:
        self._sem.acquire()
        t = threading.Thread(target=self._run, args=(fn, args), daemon=True)
        t.start()
        self._threads.append(t)

    def _run(self, fn, args) -> None:
        try:
            fn(*args)
        except BaseException as exc:  # noqa: BLE001 — 收集后统一重抛
            self._exc.append(exc)
        finally:
            self._sem.release()

    def __enter__(self) -> "_BoundedConcurrency":
        return self

    def __exit__(self, *exc) -> None:
        for t in self._threads:
            t.join()
        if self._exc:
            raise self._exc[0]
