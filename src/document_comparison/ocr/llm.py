"""多模态 LLM OCR 引擎:通过 OpenAI 兼容的 Chat Completions API 识别 PDF 版面。

设计目标:
- 无需本地 GPU / PaddlePaddle,任意可访问多模态 LLM 的环境即可部署。
- 兼容 OpenAI 协议(base_url 可指向 OpenAI / Azure / 本地 vLLM / SGLang 等)。
- 每页 PDF 渲染为 PNG,以 image_url 发多模态请求,要求 LLM 返回 JSON 版面块。
- 输出统一为 Block(bbox 为 PDF 点坐标),与 mock 引擎一致。

配置(统一持久化于 Postgres llm_config 表,在 UI 设置页维护):
- llm_api_protocol       接口协议(openai | openai_responses | anthropic,默认 openai)
- llm_api_base           API 根地址(按所选协议拼 /chat/completions、/responses 或 /messages)
- llm_api_key            API Key
- llm_model              多模态模型名
- llm_timeout            单次请求读取超时秒(默认 120;连接超时固定 10s)
- llm_max_concurrency    逐页并发数(默认 4)
- llm_max_retries        单页瞬态失败(超时 / 429 / 5xx)重试次数(默认 2)
"""
from __future__ import annotations

import base64
import json
import logging
import random
import re
from collections import deque
from pathlib import Path
import contextvars
import threading
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

from .._llm_json import extract_llm_json
from ..config import settings
from ..llm_protocol import build_chat_request, parse_chat_content
from ..models import Block, PageMeta, TableStructure
from ..observability import (
    log_model_failure,
    log_model_request,
    log_model_response,
)
from ..parsing.pdf import render_page, render_pages
from .base import ProgressCb
from .seal import (
    PreparedSealVariant,
    SealRecoveryDiagnostic,
    merge_seal_ocr_blocks,
    prepare_seal_variant,
)

# 要求 LLM 返回的 JSON 结构:
# {"blocks": [{"label": ..., "content": ..., "bbox": [x1,y1,x2,y2], "table": {"headers":[...], "rows":[...]}}]}
# bbox 用归一化 [0,1] 坐标(相对页面),再由本引擎换算回 PDF 点坐标。
# table 字段仅 label=table 时出现,提供结构化表头/行供单元格级比对;
# content 仍保留为表格纯文本(向后兼容)。
_SYSTEM_PROMPT = (
    "你是一个文档版面分析助手。给定一页文档图片,识别其中所有可读的版面块,"
    "包括标题、段落、表格、列表、印章等。"
    "严格只输出 JSON,不要解释、不要 markdown 代码块。"
    "JSON 结构为:{\"blocks\": [{\"label\": str, \"content\": str, \"bbox\": [x1,y1,x2,y2], \"table\": {\"headers\": [str,...], \"rows\": [[str,...],...]}]}。"
    "label 取值:text / doc_title / paragraph_title / table / list / seal。"
    "bbox 为该块在页面中的归一化边界框,坐标范围 [0,1],原点左上角。"
    "content 为该块的完整文字内容。\n"
    "表格格式约定(重要,用于后续逐行比对):\n"
    "- 表格块必须同时提供 content(纯文本)和 table(结构化)两个字段。\n"
    "- content 中每行单元格用「 | 」(竖线两侧各一个空格)分隔,首行是表头,"
    "不要输出 Markdown 表格语法,禁止分隔行(如 |---|---|),禁止行首/行尾额外 |。\n"
    "- table.headers 是表头各列名称数组;table.rows 是数据行数组,每行是单元格数组,"
    "每行单元格数应与 headers 对齐。\n"
    "- 示例:content=\"阶段 | 比例 | 金额\\n预付款 | 30% | 30000\\n尾款 | 70% | 70000\","
    "table={\"headers\":[\"阶段\",\"比例\",\"金额\"],\"rows\":[[\"预付款\",\"30%\",\"30000\"],[\"尾款\",\"70%\",\"70000\"]]}。\n"
    "- 非表格块(text/doc_title/paragraph_title/list/seal)不要输出 table 字段。\n"
    "切分粒度约定(重要):\n"
    "- 同一编号条款(第X条 / X.X / (X) / 一、等)的全部内容合并为单个 block 输出,"
    "不要按视觉换行或段落把一条编号条款拆成多个 block。\n"
    "- 合同首部、签字页的甲乙方信息、联系方式等键值字段,每一行(每个字段名)作为独立 block 输出,"
    '字段名带冒号,如 content="甲方(甲方主体):XX公司"、content="联系电话:138..."。'
)

# 整图(长图)整体 OCR 用的提示词:只要纯文本,不要求版面块 JSON。
# 用于无标注版管线的扫描件兜底——所有页拼成一张长图,单次调用取全文。
_WHOLE_DOC_OCR_PROMPT = (
    "你是一个文档文字识别助手。给定一张可能包含多页的完整文档图片,"
    "请按人类阅读顺序(从上到下、从左到右)识别其中全部文字内容,直接输出纯文本。"
    "不要输出 JSON,不要解释,不要加页码或分隔标记。"
    "要求:\n"
    "- 保留原文的段落与换行结构,段落之间空一行。\n"
    "- 表格内容每行用「 | 」(竖线两侧各一个空格)分隔单元格,首行是表头,"
    "不要输出 Markdown 表格语法,不要分隔行(如 |---|---|)。\n"
    "- 只输出识别到的文字,不要添加标题、说明或格式包裹。"
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
        api_protocol: str | None = None,
    ) -> None:
        self.api_base = api_base if api_base is not None else settings.llm_api_base
        self.api_key = api_key if api_key is not None else settings.llm_api_key
        self.model = model if model is not None else settings.llm_model
        self.timeout = timeout if timeout is not None else settings.llm_timeout
        self.max_concurrency = (
            max_concurrency if max_concurrency is not None else settings.llm_max_concurrency
        )
        self.max_retries = (
            max_retries if max_retries is not None else settings.llm_max_retries
        )
        self.api_protocol = (
            api_protocol if api_protocol is not None else settings.llm_api_protocol
        )
        self._dpi = settings.pdf_render_dpi
        self.last_seal_diagnostics: dict[int, SealRecoveryDiagnostic] = {}

    # —— 公共接口 ——
    def recognize(
        self,
        pdf_path: Path,
        page_metas: list[PageMeta],
        *,
        on_progress: ProgressCb | None = None,
    ) -> list[list[Block]]:
        self._validate_config()
        self.last_seal_diagnostics = {}
        images = render_pages(pdf_path, dpi=self._dpi)
        if not images:
            logger.info("ocr recognize pages=0 (empty pdf)")
            return []
        logger.info(
            "ocr recognize pages=%s dpi=%s concurrency=%s model=%s",
            len(images), self._dpi, self.max_concurrency, self.model,
        )

        seal_inputs: list[tuple[PreparedSealVariant, PageMeta] | None] = [
            None
        ] * len(images)
        if getattr(settings, "seal_recovery_enabled", False):
            recovery_dpi = max(
                self._dpi, int(getattr(settings, "seal_recovery_dpi", 300))
            )
            for page_index, image in enumerate(images):
                prepared = prepare_seal_variant(image)
                if prepared is None:
                    continue
                if recovery_dpi != self._dpi:
                    high_res = render_page(
                        pdf_path, page_index, dpi=recovery_dpi
                    )
                    prepared = prepare_seal_variant(high_res) or prepared
                seal_inputs[page_index] = (
                    prepared,
                    PageMeta(
                        page_index=page_index,
                        width_px=prepared.width_px,
                        height_px=prepared.height_px,
                        pdf_width_pt=page_metas[page_index].pdf_width_pt,
                        pdf_height_pt=page_metas[page_index].pdf_height_pt,
                    ),
                )

        # Pre-warm certifi CA bundle before spawning threads — certifi.where()
        # uses an unlocked global guard that races under concurrent access.
        import certifi
        certifi.where()

        # 逐页识别(限并发)
        results: list[list[Block]] = [None] * len(images)  # type: ignore[list-item]
        total = len(images)
        done_count = [0]  # mutable counter for threads

        # 一个任务内的所有页面共享连接池。httpx.Client 可跨线程使用，能够
        # 复用 TCP/TLS 连接，避免每页重新握手；任务结束后统一关闭。
        timeout = httpx.Timeout(self.timeout, connect=10.0)
        limits = httpx.Limits(
            max_connections=max(1, self.max_concurrency),
            max_keepalive_connections=max(1, self.max_concurrency),
        )
        with httpx.Client(timeout=timeout, limits=limits) as client:
            with _BoundedConcurrency(self.max_concurrency) as pool:
                for i, img in enumerate(images):
                    seal_input = seal_inputs[i]
                    pool.submit(
                        self._recognize_page,
                        i, img, page_metas[i], results,
                        on_progress, total, done_count, client,
                        seal_input[0] if seal_input else None,
                        seal_input[1] if seal_input else None,
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
        client: httpx.Client | None = None,
        prepared_seal: PreparedSealVariant | None = None,
        recovery_meta: PageMeta | None = None,
    ) -> None:
        data_url = _to_data_url(png_bytes)
        content = self._chat(data_url, client=client)
        blocks = _parse_blocks(content, page_index, meta)
        if getattr(settings, "seal_recovery_enabled", False):
            prepared = prepared_seal or prepare_seal_variant(png_bytes)
            if prepared is not None:
                variant_meta = recovery_meta or meta
                try:
                    recovered_content = self._chat(
                        _to_data_url(prepared.png_bytes), client=client
                    )
                    recovered_blocks = _parse_blocks(
                        recovered_content, page_index, variant_meta
                    )
                except Exception as exc:  # noqa: BLE001 -- 原图 OCR 已成功
                    logger.warning(
                        "seal recovery ocr failed page=%s error_type=%s",
                        page_index,
                        type(exc).__name__,
                    )
                    blocks = [b for b in blocks if b.label != "seal"]
                    self.last_seal_diagnostics[page_index] = (
                        SealRecoveryDiagnostic(
                            reliable=False,
                            reasons=("检测到印章，但二次 OCR 失败",),
                            region_count=len(prepared.regions_px),
                        )
                    )
                else:
                    merged = merge_seal_ocr_blocks(
                        blocks, recovered_blocks, prepared, variant_meta
                    )
                    blocks = list(merged.blocks)
                    self.last_seal_diagnostics[page_index] = merged.diagnostic
        out[page_index] = blocks
        if on_progress:
            done_count[0] += 1
            frac = 0.10 + (done_count[0] / total_pages) * 0.60
            on_progress("ocr", frac)

    def _validate_config(self) -> None:
        """在渲染和重试前识别永久配置错误，避免无意义等待。"""
        if not self.api_base or not self.api_base.strip():
            raise ValueError("OCR 未配置 llm_api_base")
        if not self.model or not self.model.strip():
            raise ValueError("OCR 未配置 llm_model")
        if self.max_concurrency < 1:
            raise ValueError("llm_max_concurrency 必须大于 0")
        if self.max_retries < 0:
            raise ValueError("llm_max_retries 不能小于 0")

    def _chat(self, data_url: str, *, client: httpx.Client | None = None) -> str:
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
            # chat_template_kwargs.enable_thinking=False:关闭 Qwen3 系列默认输出的
            # <think> 思考链 token(OCR 识别是确定性任务,这些 token 不进结果但严重拖慢
            # 生成)。必须嵌进 chat_template_kwargs 才会被 vLLM 应用到 chat template;
            # 顶层 enable_thinking 字段在多数 vLLM 版本被忽略(见 vllm#35574)。
            # 非 Qwen3 模型按 OpenAI 兼容约定忽略未知参数,不报错。
            "chat_template_kwargs": {"enable_thinking": False},
        }
        return self._post_chat(payload, kind="ocr", client=client)

    def recognize_text(
        self, image_bytes: bytes, *, client: httpx.Client | None = None
    ) -> str:
        """单次 OCR 取纯文本(无标注版扫描件识别用)。

        与逐页结构化 `recognize` 的区别:不要求 JSON 版面块,直接让模型按阅读顺序
        输出该图片的纯文本,表格用「cell | cell」格式。既可处理整张长图,也可处理
        单页图片;无标注版扫描件采用逐页并发调用本方法、最后按序拼接成整篇纯文本。

        `client` 可传入共享的 httpx.Client 以复用 TCP/TLS 连接池(并发场景)。
        """
        self._validate_config()
        data_url = _to_data_url(image_bytes)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _WHOLE_DOC_OCR_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "请识别这张文档图片的全部文字,按阅读顺序输出。"},
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url, "detail": "high"},
                        },
                    ],
                },
            ],
            "temperature": 0,
            # chat_template_kwargs.enable_thinking=False:关闭 Qwen3 系列默认输出的
            # <think> 思考链 token(整篇纯文本 OCR 是确定性任务,这些 token 不进结果但
            # 严重拖慢生成)。必须嵌进 chat_template_kwargs 才会被 vLLM 应用到 chat
            # template;顶层 enable_thinking 字段在多数 vLLM 版本被忽略(见 vllm#35574)。
            # 非 Qwen3 模型按 OpenAI 兼容约定忽略未知参数,不报错。
            "chat_template_kwargs": {"enable_thinking": False},
        }
        return self._post_chat(payload, kind="ocr-whole", client=client)

    def _post_chat(
        self,
        payload: dict[str, Any],
        *,
        kind: str,
        client: httpx.Client | None = None,
    ) -> str:
        """统一的对话 LLM POST + 重试骨架,返回 assistant 文本。

        payload 按 OpenAI Chat Completions 规范形态构造,发送前经
        llm_protocol.build_chat_request 按所选协议(api_protocol)转换为
        /chat/completions、/responses 或 /messages 的请求体。

        可重试瞬态故障:超时 / 网络传输错误 / 429 / 5xx。
        (httpx.TransportError 覆盖 ConnectError / ReadTimeout / NetworkError 等)
        4xx(鉴权、参数错误等)不可重试,立即抛出。
        """
        url, headers, payload = build_chat_request(
            api_base=self.api_base,
            api_key=self.api_key,
            payload=payload,
            protocol=self.api_protocol,
        )
        last_exc: Exception | None = None
        owns_client = client is None
        if client is None:
            client = httpx.Client(timeout=httpx.Timeout(self.timeout, connect=10.0))
        try:
            for attempt in range(self.max_retries + 1):
                request_started = log_model_request(logger, kind, url, payload, attempt + 1)
                try:
                    resp = client.post(url, json=payload, headers=headers)
                except httpx.TransportError as exc:
                    last_exc = exc
                    log_model_failure(logger, kind, request_started, str(exc))
                    logger.warning(
                        "%s transport error attempt=%s/%s reason=%s",
                        kind, attempt + 1, self.max_retries + 1, exc,
                    )
                else:
                    if resp.status_code == 429 or resp.status_code >= 500:
                        last_exc = httpx.HTTPStatusError(
                            f"服务端瞬态错误:HTTP {resp.status_code}",
                            request=resp.request,
                            response=resp,
                        )
                        log_model_failure(
                            logger, kind, request_started,
                            f"transient HTTP {resp.status_code}",
                            status_code=resp.status_code,
                            response=resp.text,
                        )
                        logger.warning(
                            "%s transient http status=%s attempt=%s/%s",
                            kind, resp.status_code, attempt + 1, self.max_retries + 1,
                        )
                    else:
                        try:
                            resp.raise_for_status()  # 4xx:不可重试,直接抛
                        except httpx.HTTPStatusError as exc:
                            log_model_failure(
                                logger, kind, request_started, str(exc),
                                status_code=resp.status_code,
                                response=resp.text,
                            )
                            raise
                        data = resp.json()
                        log_model_response(logger, kind, resp.status_code, data, request_started)
                        return parse_chat_content(data, protocol=self.api_protocol)
                if attempt < self.max_retries:
                    backoff = min(2 ** attempt, 8) + random.random()
                    logger.info("%s retry after %.1fs", kind, backoff)
                    time.sleep(backoff)
            assert last_exc is not None
            logger.error("%s give up after %s attempts: %s", kind, self.max_retries + 1, last_exc)
            raise last_exc
        finally:
            if owns_client:
                client.close()


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
        # bbox 可能是归一化 [0,1],也可能是渲染图片像素坐标;统一转为 PDF pt
        bbox_norm = item.get("bbox") or item.get("box") or []
        bbox_pt = _to_pt_bbox(bbox_norm, w_pt, h_pt, meta.width_px, meta.height_px)
        if idx == 0:
            logger.debug(
                "ocr bbox space probe page=%s raw=%s pt=%s meta_wxh_px=%s,%s pt=%s,%s",
                page_index, bbox_norm, bbox_pt,
                meta.width_px, meta.height_px, w_pt, h_pt,
            )
        # 结构化表格:label=table 时解析 table 字段(headers/rows)
        table_obj: TableStructure | None = None
        if label == "table":
            tbl = item.get("table")
            if isinstance(tbl, dict):
                headers = tbl.get("headers") or []
                rows = tbl.get("rows") or []
                if isinstance(headers, list) and isinstance(rows, list):
                    # 拍平单元格内换行(与 Word 侧 _table_rows 对称):VL 模型偶尔
                    # 会在表头/单元格内返回换行,如「数\\n量」,会破坏列对齐。
                    headers_str = [
                        str(h).replace("\n", " ").replace("\r", " ").strip()
                        for h in headers
                        if str(h).replace("\n", " ").strip()
                    ]
                    rows_str = [
                        [str(c).replace("\n", " ").replace("\r", " ").strip() for c in row]
                        for row in rows
                        if isinstance(row, list)
                    ]
                    if headers_str or rows_str:
                        table_obj = TableStructure(headers=headers_str, rows=rows_str)
        blocks.append(
            Block(
                block_id=f"p{page_index}-b{idx}",
                page_index=page_index,
                label=label,
                bbox=bbox_pt,
                content=text,
                table=table_obj,
            )
        )
    return blocks


def _to_pt_bbox(
    bbox: list[float],
    w_pt: float,
    h_pt: float,
    width_px: float,
    height_px: float,
) -> list[float]:
    """把 LLM 返回的 bbox 换算为 PDF 点坐标(pt)。

    支持三种坐标空间(按 max(|v|) 分段):
    - <= 1.5      : 归一化 [0,1]
    - <= 1000     : Qwen-VL 原生 [0,1000) 区间(prompt 要求 [0,1] 但模型训练约定如此)
    - > 1000      : 渲染图绝对像素(300dpi),按 width_px/height_px 缩放
    """
    if len(bbox) >= 4:
        x1, y1, x2, y2 = bbox[0], bbox[1], bbox[2], bbox[3]
        vmax = max(abs(x1), abs(y1), abs(x2), abs(y2))
        # 渲染图绝对像素坐标:按 DPI 比例换算
        if vmax > 1000:
            if width_px <= 0 or height_px <= 0:
                return [float(x1), float(y1), float(x2), float(y2)]
            return [
                float(x1) * w_pt / width_px,
                float(y1) * h_pt / height_px,
                float(x2) * w_pt / width_px,
                float(y2) * h_pt / height_px,
            ]
        # Qwen-VL 原生 [0,1000) 归一化:除以 1000
        if vmax > 1.5:
            return [
                float(x1) * w_pt / 1000.0,
                float(y1) * h_pt / 1000.0,
                float(x2) * w_pt / 1000.0,
                float(y2) * h_pt / 1000.0,
            ]
        # 归一化 [0,1]
        return [x1 * w_pt, y1 * h_pt, x2 * w_pt, y2 * h_pt]
    return []


def _extract_json(text: str) -> Any:
    """从可能混杂文本/代码块的回复中提取首个 JSON 对象或数组。

    委托给共享 helper ``_llm_json.extract_llm_json``,保留薄封装以维持本模块内的
    既有调用点签名(允许返回 dict 或 list,供 ``_parse_blocks`` 分支处理)。
    """
    return extract_llm_json(text, expect=(dict, list))


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
        # 捕获当前 context(含 observability.current_llm_collector),让子线程能
        # 继续把 LLM 调用记录 append 到任务收集器。threading.Thread 不会自动继承。
        ctx = contextvars.copy_context()
        t = threading.Thread(target=self._run, args=(ctx, fn, args), daemon=True)
        t.start()
        self._threads.append(t)

    def _run(self, ctx, fn, args) -> None:
        try:
            ctx.run(fn, *args)
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
