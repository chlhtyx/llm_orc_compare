"""PaddleOCR-VL 引擎:可切换 vLLM Chat Completions 与 PaddleOCR 官方 SDK。

PaddleOCR-VL 是专用 OCR 模型,**不遵循 system prompt 的 JSON 格式指令**。
其实际输出取决于模型版本与调用方式,本引擎按返回内容自适应解析:

1. **Markdown 纯文本**(PaddleOCR-VL-1.5 在 SiliconFlow 等平台上的实际行为):
   返回带行结构的纯文本,表格以 Markdown `|` 分隔(含 `---` 分隔行),段落间空行分隔。
   → 由 `_parse_plain_content` 解析:连续 `|` 行归为一个 `label=table` 的 Block
     (构造 TableStructure),其余行归为 `label=text` 的 Block。

2. **文字行 + `<|LOC_|>` 坐标标记**:
   每行文字后跟偶数个 `<|LOC_N|>` token,构成矩形/四边形/多边形坐标:
       [x1, y1, x2, y1, x2, y2, x1, y2](0-1000 归一化空间)
   → 由 `_parse_loc_content` 解析,每行一个带 bbox 的 Block。

本引擎在拿到响应后检测是否含 `<|LOC_` 标记:有则走 LOC 分支,否则走 plain 分支,
对 PaddleOCR-VL 各版本兼容。

关键调用约定:
- 内容调用使用官方「OCR:」任务；标准合同比对在内容缺少坐标时追加一次
  「Spotting:」调用，并在本地按阅读顺序挂载 bbox。Spotting 失败不阻断正文比对。
- **必须限制 `max_tokens`**:PaddleOCR-VL 在表格行上不自我停止,会重复 hallucination
  直到 token 上限,导致合计行/后续内容丢失(实测 200DPI 大图 385 行死循环)。
- 不带 `response_format`(强制 JSON 会让该模型陷入构造死循环)

配置:独立使用 paddleocr_*。原 vLLM 配置与
paddleocr_official_* 官方 SDK 配置分开保存,
与 llm 后端的配置完全隔离。当 ocr_backend=paddleocr 时由 get_ocr_engine() 路由到
本引擎。vllm 模式要求 API Base;官方 SDK 模式可留空 Base 使用官方默认地址。
"""
from __future__ import annotations

import base64
import logging
import random
import re
import contextvars
import threading
import time
import tempfile
from collections import deque
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import requests

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz  # type: ignore[no-redef]

from ..config import settings
from ..models import Block, PageMeta, TableStructure
from ..observability import log_model_failure, log_model_request, log_model_response
from ..parsing.pdf import render_pages
from ..structure.normalize import normalize_text
from .base import ProgressCb

logger = logging.getLogger(__name__)

# <|LOC_数字|> token 正则
_LOC_RE = re.compile(r"<\|LOC_(\d+)\|>")

# LOC 坐标空间:PaddleOCR-VL 使用 0-1000 归一化(与 Qwen-VL 一致)
_LOC_SPACE = 1000.0

# 限制生成长度:PaddleOCR-VL 在表格行上可能重复生成直到 token 上限，
# 导致合计行/后续内容丢失。2000 作为单页安全上限；内容超出时由下方
# 有界分片恢复，而不是继续放大单次输出。
_MAX_TOKENS = 2000
_SILICONFLOW_PADDLEOCR_MAX_TOKENS = 2000

# 整页触发输出上限时，只做一轮上下分片恢复。分片保留少量重叠区，避免裁切
# 穿过一行文字；禁止递归继续分片，保证模型调用次数有硬上限。
_TILE_OVERLAP_RATIO = 0.08

# 整图(长图)整体 OCR 的生成长度上限:无标注版把整篇拼成一张长图,
# 单次调用需容纳全文,按整篇合同量级放宽到 8000。
_WHOLE_DOC_MAX_TOKENS = 8000

# 表格行:含至少一个 |(Markdown 表格的单元格分隔)。用于识别连续表格行。
_TABLE_LINE_RE = re.compile(r"\||\t")
_HTML_TABLE_RE = re.compile(r"<table\b[\s\S]*?</table\s*>", re.IGNORECASE)
_MD_SEPARATOR_RE = re.compile(r"^[\s:|\-]+$")
_REPEATED_ROW_THRESHOLD = 4
_COMPLETE_BBOX_COVERAGE = 0.95
_OFFICIAL_DEFAULT_MODEL = "PaddleOCR-VL-1.6"
# 官方文档解析是异步任务。排队或复杂 PDF 可能超过通用 OCR 的 300 秒请求
# 超时；不能用该较短默认值覆盖 SDK 的总轮询时间。较大的用户配置仍然生效。
_OFFICIAL_MIN_POLL_TIMEOUT = 900.0

# AI Studio 官方 ``layout-parsing`` 示例参数。该同步服务与
# PaddleOCRClient 的异步 ``/api/v2/ocr/jobs`` 队列是两条独立调用链路。
_LAYOUT_PARSING_OPTIONS: dict[str, Any] = {
    "markdownIgnoreLabels": [
        "header",
        "header_image",
        "footer",
        "footer_image",
        "number",
        "footnote",
        "aside_text",
    ],
    "useChartRecognition": False,
    "useRegionDetection": True,
    "useDocOrientationClassify": False,
    "useDocUnwarping": False,
    "useTextlineOrientation": False,
    "useSealRecognition": True,
    "useFormulaRecognition": True,
    "useTableRecognition": True,
    "layoutThreshold": 0.5,
    "layoutNms": True,
    "layoutUnclipRatio": 1,
    "textDetLimitType": "min",
    "textDetLimitSideLen": 64,
    "textDetThresh": 0.3,
    "textDetBoxThresh": 0.6,
    "textDetUnclipRatio": 1.5,
    "textRecScoreThresh": 0,
    "sealDetLimitType": "min",
    "sealDetLimitSideLen": 736,
    "sealDetThresh": 0.2,
    "sealDetBoxThresh": 0.6,
    "sealDetUnclipRatio": 0.5,
    "sealRecScoreThresh": 0,
    "useTableOrientationClassify": True,
    "useOcrResultsWithTableCells": True,
    "useE2eWiredTableRecModel": False,
    "useE2eWirelessTableRecModel": False,
    "useWiredTableCellsTransToHtml": False,
    "useWirelessTableCellsTransToHtml": False,
    "parseLanguage": "default",
}


class _OCRChatContent(str):
    """保持原有 str 接口，同时携带模型的停止原因。"""

    finish_reason: str
    truncated: bool

    def __new__(cls, value: str, finish_reason: str = ""):
        instance = super().__new__(cls, value)
        instance.finish_reason = finish_reason
        instance.truncated = finish_reason == "length"
        return instance


class PaddleOCREngine:
    """调用 PaddleOCR-VL,运行时可选 vLLM 或 PaddleOCR 官方 SDK。

    与 LLMOCREngine 的区别:
    - 请求:user 消息只发图片 + 简单指令(不发 system prompt 要求 JSON 格式)
    - 内容:解析 OCR 纯文本/Markdown；可选 Spotting 响应只用于补充坐标
    - 坐标:LOC 坐标 0-1000 → 换算为 PDF pt(复用 _to_pt_bbox 逻辑)
    """

    def __init__(
        self,
        api_mode: str | None = None,
        api_base: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        official_api_base: str | None = None,
        official_access_token: str | None = None,
        official_model: str | None = None,
        timeout: float | None = None,
        max_concurrency: int | None = None,
        max_retries: int | None = None,
        enable_spotting: bool = False,
    ) -> None:
        # paddleocr 后端使用独立配置,不复用 llm_*。
        self.api_mode = api_mode or settings.paddleocr_api_mode
        if self.api_mode not in {"vllm", "official_sdk"}:
            raise ValueError(
                f"未知 paddleocr_api_mode: {self.api_mode};"
                "仅支持 vllm | official_sdk"
            )
        self.api_base = api_base or settings.paddleocr_api_base
        self.api_key = api_key if api_key is not None else settings.paddleocr_api_key
        self.model = model or settings.paddleocr_model
        self.official_api_base = (
            official_api_base
            if official_api_base is not None
            else settings.paddleocr_official_api_base
        )
        self.official_access_token = (
            official_access_token
            if official_access_token is not None
            else settings.paddleocr_official_access_token
        )
        self.official_model = (
            official_model
            if official_model is not None
            else settings.paddleocr_official_model
        )
        self.timeout = timeout if timeout is not None else settings.paddleocr_timeout
        self.max_concurrency = (
            max_concurrency if max_concurrency is not None else settings.paddleocr_max_concurrency
        )
        self.max_retries = (
            max_retries if max_retries is not None else settings.paddleocr_max_retries
        )
        self.enable_spotting = enable_spotting
        self._dpi = settings.pdf_render_dpi
        # 页号使用本次 recognize 输入中的局部索引；TrustedPDFReader 会再映射
        # 回原 PDF 页号。每次识别开始时都会重置，避免任务间串数据。
        self.last_truncated_pages: set[int] = set()

    # —— 公共接口(与 LLMOCREngine 对称)——
    def recognize(
        self,
        pdf_path: Path,
        page_metas: list[PageMeta],
        *,
        on_progress: ProgressCb | None = None,
    ) -> list[list[Block]]:
        self.last_truncated_pages = set()
        if self.api_mode == "official_sdk":
            return self._recognize_official_document(
                pdf_path, page_metas, on_progress=on_progress
            )
        if not self.api_base:
            raise RuntimeError(
                "paddleocr_api_base 未配置,请在设置页填写专用 OCR 模型的推理服务地址"
            )

        images = render_pages(pdf_path, dpi=self._dpi)
        if not images:
            logger.info("paddleocr recognize pages=0 (empty pdf)")
            return []
        logger.info(
            "paddleocr recognize pages=%s dpi=%s concurrency=%s model=%s",
            len(images), self._dpi, self.max_concurrency, self.model,
        )

        # Pre-warm certifi CA bundle(避免并发竞争)
        import certifi
        certifi.where()

        results: list[list[Block]] = [None] * len(images)  # type: ignore[list-item]
        truncated_flags = [False] * len(images)
        total = len(images)
        done_count = [0]

        with _BoundedConcurrency(self.max_concurrency) as pool:
            for i, img in enumerate(images):
                pool.submit(
                    self._recognize_page,
                    i, img, page_metas[i], results,
                    on_progress, total, done_count, truncated_flags,
                )

        self.last_truncated_pages = {
            page_index
            for page_index, truncated in enumerate(truncated_flags)
            if truncated
        }
        return results

    def _recognize_official_document(
        self,
        pdf_path: Path,
        page_metas: list[PageMeta],
        *,
        on_progress: ProgressCb | None = None,
    ) -> list[list[Block]]:
        """通过官方 SDK 一次提交 PDF,按返回页映射为 Block。"""
        if on_progress:
            on_progress("ocr", 0.10)
        result = self._official_parse_document(pdf_path)
        pages = list(getattr(result, "pages", []) or [])
        if len(pages) != len(page_metas):
            logger.warning(
                "paddleocr official sdk page count mismatch expected=%s actual=%s",
                len(page_metas),
                len(pages),
            )
        blocks_by_page: list[list[Block]] = []
        for page_index, meta in enumerate(page_metas):
            if page_index < len(pages):
                blocks = _parse_official_page(pages[page_index], page_index, meta)
            else:
                blocks = []
            blocks_by_page.append(blocks)
            if on_progress:
                on_progress(
                    "ocr", 0.10 + ((page_index + 1) / max(1, len(page_metas))) * 0.60
                )
        return blocks_by_page

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
        truncated_flags: list[bool] | None = None,
    ) -> None:
        data_url = _to_data_url(png_bytes)
        content = self._chat(data_url, prompt="OCR:")
        if getattr(content, "truncated", False):
            blocks, still_truncated = self._recover_truncated_page(
                page_index, png_bytes, meta, content
            )
        else:
            blocks = _parse_content(content, page_index, meta)
            still_truncated = False
        if truncated_flags is not None:
            truncated_flags[page_index] = still_truncated
        coverage = _bbox_coverage(blocks)
        if self.enable_spotting and coverage < _COMPLETE_BBOX_COVERAGE:
            try:
                spotting_content = self._chat(data_url, prompt="Spotting:")
            except Exception as exc:  # noqa: BLE001
                # 坐标补全属于报告增强；失败不能让已成功的 OCR 内容比对失败。
                logger.warning(
                    "paddleocr spotting failed page=%s error_type=%s",
                    page_index,
                    type(exc).__name__,
                )
            else:
                if "<|LOC_" in spotting_content:
                    spotting_blocks = _parse_loc_content(
                        spotting_content, page_index, meta
                    )
                    blocks = _attach_spotting_bboxes(blocks, spotting_blocks)
                else:
                    logger.warning(
                        "paddleocr spotting returned no LOC tokens page=%s chars=%s",
                        page_index,
                        len(spotting_content),
                    )
        logger.info(
            "paddleocr page location page=%s bbox_coverage=%.3f",
            page_index,
            _bbox_coverage(blocks),
        )
        out[page_index] = blocks
        if on_progress:
            done_count[0] += 1
            frac = 0.10 + (done_count[0] / total_pages) * 0.60
            on_progress("ocr", frac)

    def _recover_truncated_page(
        self,
        page_index: int,
        png_bytes: bytes,
        meta: PageMeta,
        original_content: str,
    ) -> tuple[list[Block], bool]:
        """整页被截断后做固定一轮上下分片，不递归。"""
        try:
            tiles = _split_png_vertical(png_bytes, meta)
            tile_blocks: list[list[Block]] = []
            tile_truncated = False
            for tile_bytes, tile_meta, y_offset_pt in tiles:
                tile_content = self._chat(_to_data_url(tile_bytes), prompt="OCR:")
                tile_truncated = tile_truncated or bool(
                    getattr(tile_content, "truncated", False)
                )
                parsed = _parse_content(tile_content, page_index, tile_meta)
                tile_blocks.append(
                    _shift_blocks_y(parsed, page_index, y_offset_pt)
                )
            blocks = _merge_vertical_tile_blocks(
                tile_blocks[0], tile_blocks[1], page_index
            )
        except Exception as exc:  # noqa: BLE001 - 恢复失败保留整页已有内容
            logger.warning(
                "paddleocr truncated page tile recovery failed page=%s "
                "error_type=%s",
                page_index,
                type(exc).__name__,
            )
            return _parse_content(original_content, page_index, meta), True

        if tile_truncated:
            logger.warning(
                "paddleocr page still truncated after one tile round page=%s; "
                "stop recovery and mark page for review",
                page_index,
            )
        else:
            logger.info(
                "paddleocr truncated page recovered by one tile round page=%s "
                "blocks=%s",
                page_index,
                len(blocks),
            )
        return blocks, tile_truncated

    def recognize_text(
        self, image_bytes: bytes, *, client: httpx.Client | None = None
    ) -> str:
        """单次 OCR 取纯文本(无标注版扫描件识别用)。

        与逐页结构化 `recognize` 的区别:只要纯文本(剥除 <|LOC_|> 坐标标记),
        用更大的 max_tokens 容纳内容。既可处理整张长图,也可处理单页;无标注版
        扫描件逐页并发调用本方法、最后按序拼接成整篇纯文本。

        `client` 可传入共享的 httpx.Client 以复用连接池(并发场景)。
        """
        if self.api_mode == "official_sdk":
            # 官方 SDK 只接受 file_path/file_url;使用短生命临时文件,
            # 不复用上层为 vLLM 准备的 httpx.Client。
            with tempfile.TemporaryDirectory(prefix="dc-paddle-sdk-") as temp_dir:
                image_path = Path(temp_dir) / "page.png"
                image_path.write_bytes(image_bytes)
                result = self._official_parse_document(image_path)
            return "\n".join(
                str(getattr(page, "markdown_text", "") or "")
                for page in (getattr(result, "pages", []) or [])
            )
        if not self.api_base:
            raise RuntimeError(
                "paddleocr_api_base 未配置,请在设置页填写专用 OCR 模型的推理服务地址"
            )
        data_url = _to_data_url(image_bytes)
        content = self._chat(
            data_url, prompt="OCR:", max_tokens=_MAX_TOKENS, client=client
        )
        # 剥除可能的 <|LOC_N|> 坐标标记,只留文字
        return _LOC_RE.sub("", content)

    def _official_parse_document(self, file_path: Path):
        """调用 PaddleOCR 官方同步 API 或 SDK,并记录脱敏的任务级信息。"""
        if not self.official_access_token:
            raise RuntimeError(
                "PaddleOCR 官方 API/SDK 模式未配置 Access Token;"
                "请在设置页填写 AI Studio Access Token"
            )
        if _is_layout_parsing_endpoint(self.official_api_base):
            return self._official_layout_parse(file_path)

        return self._official_sdk_parse(file_path)

    def _official_layout_parse(self, file_path: Path):
        """调用 AI Studio 示例中的同步 ``/layout-parsing`` API。"""
        endpoint = self.official_api_base.rstrip("/")
        file_bytes = file_path.read_bytes()
        payload = {
            "file": base64.b64encode(file_bytes).decode("ascii"),
            "fileType": 0 if file_path.suffix.lower() == ".pdf" else 1,
            **_LAYOUT_PARSING_OPTIONS,
        }
        safe_payload = {
            "transport": "official_layout_api",
            "file": {
                "name": file_path.name,
                "size_bytes": len(file_bytes),
                "file_type": payload["fileType"],
            },
            "options": _LAYOUT_PARSING_OPTIONS,
        }
        request_started = log_model_request(
            logger, "paddleocr", endpoint, safe_payload, 1
        )
        try:
            response = requests.post(
                endpoint,
                json=payload,
                headers={
                    "Authorization": f"token {self.official_access_token}",
                    "Content-Type": "application/json",
                },
                timeout=float(self.timeout),
            )
            response.raise_for_status()
            response_data = response.json()
            pages = _layout_api_pages(response_data)
        except Exception as exc:  # noqa: BLE001 -- 官方 API 统一错误边界
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            log_model_failure(
                logger,
                "paddleocr",
                request_started,
                str(exc),
                status_code=status_code,
            )
            raise

        log_model_response(
            logger,
            "paddleocr",
            response.status_code,
            {
                "transport": "official_layout_api",
                "pages": len(pages),
                "markdown_chars": sum(
                    len(str(getattr(page, "markdown_text", "") or ""))
                    for page in pages
                ),
            },
            request_started,
        )
        return SimpleNamespace(job_id="", pages=pages)

    def _official_sdk_parse(self, file_path: Path):
        """调用异步任务型 PaddleOCRClient.parse_document。"""
        PaddleOCRClient, PaddleOCRVLOptions = _load_official_sdk()
        model = self.official_model or _OFFICIAL_DEFAULT_MODEL
        client_kwargs: dict[str, Any] = {
            "token": self.official_access_token,
            "request_timeout": float(self.timeout),
            "poll_timeout": max(
                float(self.timeout), _OFFICIAL_MIN_POLL_TIMEOUT
            ),
        }
        if self.official_api_base:
            client_kwargs["base_url"] = self.official_api_base
        options = PaddleOCRVLOptions(
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_layout_detection=True,
            format_block_content=True,
            temperature=0,
            max_new_tokens=_MAX_TOKENS,
            prettify_markdown=False,
            return_markdown_images=False,
            visualize=False,
        )
        payload = {
            "transport": "official_sdk",
            "model": model,
            "file": {
                "name": file_path.name,
                "size_bytes": file_path.stat().st_size,
            },
        }
        request_started = log_model_request(
            logger,
            "paddleocr",
            self.official_api_base or "paddleocr://official-api",
            payload,
            1,
        )
        try:
            with PaddleOCRClient(**client_kwargs) as sdk_client:
                result = sdk_client.parse_document(
                    file_path=str(file_path), model=model, options=options
                )
        except Exception as exc:  # noqa: BLE001 -- SDK 统一错误边界
            log_model_failure(logger, "paddleocr", request_started, str(exc))
            raise
        pages = list(getattr(result, "pages", []) or [])
        log_model_response(
            logger,
            "paddleocr",
            200,
            {
                "job_id": getattr(result, "job_id", ""),
                "pages": len(pages),
                "markdown_chars": sum(
                    len(str(getattr(page, "markdown_text", "") or ""))
                    for page in pages
                ),
            },
            request_started,
        )
        return result

    def _chat(
        self,
        data_url: str,
        *,
        prompt: str = "OCR:",
        max_tokens: int = _MAX_TOKENS,
        client: httpx.Client | None = None,
    ) -> str:
        """发送多模态请求,返回模型原始文本(含 <|LOC_|> 标记)。

        `client` 可传入共享的 httpx.Client 以复用连接池(并发场景);不传则自建并关闭。
        """
        url = self.api_base.rstrip("/") + "/chat/completions"
        effective_max_tokens = _bounded_paddleocr_max_tokens(
            self.api_base, self.model, max_tokens
        )
        if effective_max_tokens != max_tokens:
            logger.warning(
                "paddleocr max_tokens capped for provider requested=%s effective=%s",
                max_tokens,
                effective_max_tokens,
            )
        # PaddleOCR-VL 不遵循复杂 system prompt,用最简指令
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}},
                    ],
                }
            ],
            "temperature": 0,
            # 限制生成长度:见模块 docstring,PaddleOCR-VL 在表格行上不自我停止,
            # 会重复 hallucination 刷到默认上限,导致后续内容(合计行/签字页)丢失。
            "max_tokens": effective_max_tokens,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        timeout = httpx.Timeout(self.timeout, connect=10.0)

        last_exc: Exception | None = None
        owns_client = client is None
        if client is None:
            client = httpx.Client(timeout=timeout)
        try:
            for attempt in range(self.max_retries + 1):
                request_started = log_model_request(logger, "paddleocr", url, payload, attempt + 1)
                try:
                    resp = client.post(url, json=payload, headers=headers)
                except httpx.TransportError as exc:
                    last_exc = exc
                    log_model_failure(logger, "paddleocr", request_started, str(exc))
                    logger.warning(
                        "paddleocr transport error attempt=%s/%s reason=%s",
                        attempt + 1, self.max_retries + 1, exc,
                    )
                else:
                    if resp.status_code == 429 or resp.status_code >= 500:
                        last_exc = httpx.HTTPStatusError(
                            f"服务端瞬态错误:HTTP {resp.status_code}",
                            request=resp.request,
                            response=resp,
                        )
                        log_model_failure(
                            logger, "paddleocr", request_started,
                            f"transient HTTP {resp.status_code}",
                            status_code=resp.status_code,
                        )
                        logger.warning(
                            "paddleocr transient http status=%s attempt=%s/%s",
                            resp.status_code, attempt + 1, self.max_retries + 1,
                        )
                    else:
                        try:
                            resp.raise_for_status()
                        except httpx.HTTPStatusError as exc:
                            provider_detail = _provider_error_detail(resp)
                            error_message = str(exc)
                            if provider_detail:
                                error_message = (
                                    f"{error_message}; {provider_detail}"
                                )
                            log_model_failure(
                                logger, "paddleocr", request_started, error_message,
                                status_code=resp.status_code,
                            )
                            raise
                        data = resp.json()
                        log_model_response(logger, "paddleocr", resp.status_code, data, request_started)
                        choice = data["choices"][0]
                        content = choice["message"]["content"]
                        if choice.get("finish_reason") == "length":
                            logger.warning(
                                "paddleocr response truncated at max_tokens=%s chars=%s; "
                                "caller will apply bounded recovery policy because this "
                                "response may omit source content",
                                effective_max_tokens, len(content),
                            )
                        return _OCRChatContent(
                            content,
                            str(choice.get("finish_reason") or ""),
                        )
                if attempt < self.max_retries:
                    backoff = min(2 ** attempt, 8) + random.random()
                    logger.info("paddleocr retry after %.1fs", backoff)
                    time.sleep(backoff)
        finally:
            if owns_client:
                client.close()
        assert last_exc is not None
        logger.error(
            "paddleocr give up after %s attempts: %s", self.max_retries + 1, last_exc
        )
        raise last_exc


# —— 解析函数 ——


def _bounded_paddleocr_max_tokens(
    api_base: str, model: str, requested: int
) -> int:
    """限制 SiliconFlow PaddleOCR-VL 输出，避免非法参数和退化长生成。"""
    requested = max(1, int(requested))
    try:
        host = str(httpx.URL(api_base).host or "").lower()
    except (TypeError, ValueError):
        host = ""
    if (
        host == "api.siliconflow.cn"
        and model.startswith("PaddlePaddle/PaddleOCR-VL")
    ):
        return min(requested, _SILICONFLOW_PADDLEOCR_MAX_TOKENS)
    return requested


def _provider_error_detail(response: httpx.Response) -> str:
    """只提取供应商错误码和短消息，避免把任意响应体写入日志。"""
    try:
        payload = response.json()
    except (ValueError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""

    error = payload.get("error")
    error_payload = error if isinstance(error, dict) else payload
    code = error_payload.get("code")
    message = error_payload.get("message")
    if code is None and error_payload is not payload:
        code = payload.get("code")
    if message is None and error_payload is not payload:
        message = payload.get("message")

    parts: list[str] = []
    if isinstance(code, (str, int)):
        safe_code = re.sub(r"[^A-Za-z0-9_.-]", "", str(code))[:64]
        if safe_code:
            parts.append(f"provider_code={safe_code}")
    if isinstance(message, str):
        safe_message = re.sub(r"\s+", " ", message).strip()[:500]
        if safe_message:
            parts.append(f"provider_message={safe_message}")
    return " ".join(parts)


def _is_layout_parsing_endpoint(api_base: str) -> bool:
    """完整 ``/layout-parsing`` 地址启用官方同步 API。"""
    return api_base.rstrip("/").endswith("/layout-parsing")


def _layout_api_pages(response_data: Any) -> list[SimpleNamespace]:
    """把官方同步 API 响应适配为 SDK page 的最小公共接口。"""
    if not isinstance(response_data, dict):
        raise RuntimeError("PaddleOCR 官方同步 API 返回格式错误:响应不是 JSON 对象")
    result = response_data.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("PaddleOCR 官方同步 API 返回格式错误:缺少 result")
    raw_pages = result.get("layoutParsingResults")
    if not isinstance(raw_pages, list):
        raise RuntimeError(
            "PaddleOCR 官方同步 API 返回格式错误:缺少 layoutParsingResults"
        )

    pages: list[SimpleNamespace] = []
    for raw_page in raw_pages:
        if not isinstance(raw_page, dict):
            continue
        markdown = raw_page.get("markdown")
        markdown_text = (
            str(markdown.get("text") or "") if isinstance(markdown, dict) else ""
        )
        pruned_result = raw_page.get("prunedResult")
        if not isinstance(pruned_result, dict):
            pruned_result = raw_page.get("pruned_result")
        if not isinstance(pruned_result, dict):
            pruned_result = {}
        pages.append(
            SimpleNamespace(
                markdown_text=markdown_text,
                pruned_result=pruned_result,
            )
        )
    return pages


def _load_official_sdk():
    """延迟导入官方 SDK,使 vLLM 模式不依赖 PaddleOCR 导入路径。"""
    try:
        from paddleocr import PaddleOCRClient, PaddleOCRVLOptions
    except ImportError as exc:  # pragma: no cover - 生产依赖缺失才触发
        raise RuntimeError(
            "PaddleOCR 官方 SDK 未安装;"
            "请安装 paddleocr>=3.7,<3.8 或重建项目镜像"
        ) from exc
    return PaddleOCRClient, PaddleOCRVLOptions


def _parse_official_page(page: Any, page_index: int, meta: PageMeta) -> list[Block]:
    """把官方 SDK DocParsingPage 转为项目 Block,优先保留版面坐标。"""
    pruned = getattr(page, "pruned_result", None)
    if not isinstance(pruned, dict):
        pruned = {}
    parsing_items = pruned.get("parsing_res_list")
    if not isinstance(parsing_items, list):
        parsing_items = []

    width = _positive_float(pruned.get("width")) or float(meta.width_px or 0)
    height = _positive_float(pruned.get("height")) or float(meta.height_px or 0)
    w_pt = meta.pdf_width_pt or (meta.width_px * 72.0 / settings.pdf_render_dpi)
    h_pt = meta.pdf_height_pt or (meta.height_px * 72.0 / settings.pdf_render_dpi)

    blocks: list[Block] = []
    for item_index, item in enumerate(parsing_items):
        if not isinstance(item, dict):
            continue
        content = str(item.get("block_content") or "").strip()
        if not content:
            continue
        label = str(item.get("block_label") or "text")
        table = _plain_text_to_table(content) if label == "table" else None
        blocks.append(
            Block(
                block_id=f"p{page_index}-paddle-sdk-{item_index}",
                page_index=page_index,
                label=label,
                bbox=_official_bbox_to_pt(
                    item.get("block_bbox"), width, height, w_pt, h_pt
                ),
                content=content,
                table=table,
            )
        )
    if blocks:
        logger.info(
            "paddleocr official sdk parsed page=%s blocks=%s located=%s",
            page_index,
            len(blocks),
            sum(len(block.bbox) >= 4 for block in blocks),
        )
        return blocks

    # SDK 保证 markdown_text,但某些模型/版本可能不返回精简结构。
    markdown = str(getattr(page, "markdown_text", "") or "")
    return _parse_plain_content(markdown, page_index, meta)


def _positive_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


def _official_bbox_to_pt(
    raw_bbox: Any,
    width: float,
    height: float,
    w_pt: float,
    h_pt: float,
) -> list[float]:
    """把 SDK 像素 rect/quad/poly 转为 PDF pt 外接矩形。"""
    if width <= 0 or height <= 0 or w_pt <= 0 or h_pt <= 0:
        return []

    numbers: list[float] = []

    def collect(value: Any) -> None:
        if isinstance(value, (list, tuple)):
            for child in value:
                collect(child)
        elif isinstance(value, (int, float)):
            numbers.append(float(value))

    collect(raw_bbox)
    if len(numbers) < 4:
        return []
    if len(numbers) == 4:
        x1, y1, x2, y2 = numbers
    elif len(numbers) % 2 == 0:
        xs = numbers[0::2]
        ys = numbers[1::2]
        x1, x2 = min(xs), max(xs)
        y1, y2 = min(ys), max(ys)
    else:
        return []
    x1, x2 = sorted((max(0.0, x1), min(width, x2)))
    y1, y2 = sorted((max(0.0, y1), min(height, y2)))
    if x2 <= x1 or y2 <= y1:
        return []
    return [
        x1 * w_pt / width,
        y1 * h_pt / height,
        x2 * w_pt / width,
        y2 * h_pt / height,
    ]


def _to_data_url(png_bytes: bytes) -> str:
    b64 = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _parse_content(
    content: str, page_index: int, meta: PageMeta
) -> list[Block]:
    """按 PaddleOCR 实际返回格式选择 LOC 或纯文本解析器。"""
    if "<|LOC_" in content:
        return _parse_loc_content(content, page_index, meta)
    return _parse_plain_content(content, page_index, meta)


def _split_png_vertical(
    png_bytes: bytes, meta: PageMeta
) -> list[tuple[bytes, PageMeta, float]]:
    """把 PNG 切成两个带重叠区的纵向分片，并返回各片的 PDF y 偏移。"""
    pixmap = fitz.Pixmap(png_bytes)
    width_px = int(pixmap.width)
    height_px = int(pixmap.height)
    if width_px <= 0 or height_px < 2:
        raise ValueError("page image is too small for vertical tile recovery")

    overlap_px = max(1, round(height_px * _TILE_OVERLAP_RATIO))
    midpoint = height_px // 2
    spans = [
        (0, min(height_px, midpoint + overlap_px)),
        (max(0, midpoint - overlap_px), height_px),
    ]
    full_width_pt = meta.pdf_width_pt or (
        meta.width_px * 72.0 / settings.pdf_render_dpi
    )
    full_height_pt = meta.pdf_height_pt or (
        meta.height_px * 72.0 / settings.pdf_render_dpi
    )

    tiles: list[tuple[bytes, PageMeta, float]] = []
    with fitz.open(stream=png_bytes, filetype="png") as image_document:
        image_page = image_document[0]
        scale_x = width_px / image_page.rect.width
        scale_y = height_px / image_page.rect.height
        matrix = fitz.Matrix(scale_x, scale_y)
        for y0_px, y1_px in spans:
            clip = fitz.Rect(
                image_page.rect.x0,
                image_page.rect.y0 + y0_px / scale_y,
                image_page.rect.x1,
                image_page.rect.y0 + y1_px / scale_y,
            )
            tile_pixmap = image_page.get_pixmap(
                matrix=matrix, clip=clip, alpha=False
            )
            height_ratio = (y1_px - y0_px) / height_px
            tile_meta = PageMeta(
                page_index=meta.page_index,
                width_px=tile_pixmap.width,
                height_px=tile_pixmap.height,
                pdf_width_pt=full_width_pt,
                pdf_height_pt=full_height_pt * height_ratio,
            )
            tiles.append(
                (
                    tile_pixmap.tobytes("png"),
                    tile_meta,
                    full_height_pt * y0_px / height_px,
                )
            )
    return tiles


def _shift_blocks_y(
    blocks: list[Block], page_index: int, y_offset_pt: float
) -> list[Block]:
    shifted: list[Block] = []
    for block in blocks:
        bbox = list(block.bbox)
        if len(bbox) >= 4:
            bbox[1] += y_offset_pt
            bbox[3] += y_offset_pt
        shifted.append(
            block.model_copy(
                update={
                    "page_index": page_index,
                    "bbox": bbox,
                }
            )
        )
    return shifted


def _merge_vertical_tile_blocks(
    upper: list[Block], lower: list[Block], page_index: int
) -> list[Block]:
    """按上下片阅读顺序合并，并清除重叠区共同识别出的连续块。"""
    upper_keys = [_location_key(block.content) for block in upper]
    lower_keys = [_location_key(block.content) for block in lower]
    overlap = 0
    for size in range(min(len(upper), len(lower), 24), 0, -1):
        if (
            all(upper_keys[-size:])
            and upper_keys[-size:] == lower_keys[:size]
        ):
            overlap = size
            break

    merged = [*upper, *lower[overlap:]]
    return [
        block.model_copy(
            update={
                "page_index": page_index,
                "block_id": f"p{page_index}-paddle-tile-{block_index}",
            }
        )
        for block_index, block in enumerate(merged)
    ]


def _parse_loc_content(
    content: str, page_index: int, meta: PageMeta
) -> list[Block]:
    """解析 PaddleOCR-VL 的「文字行 + <|LOC_|> 标记」格式为 Block 列表。

    格式:每行 = 文字 + 偶数个 <|LOC_N|> token(rect/quad/poly,0-1000 空间)
    示例:合同标题<|LOC_146|><|LOC_89|><|LOC_499|><|LOC_89|><|LOC_499|><|LOC_121|><|LOC_146|><|LOC_121|>
    """
    # 页面尺寸:优先用 PDF 点坐标;若为 0 退回像素(并按 DPI 反算)
    w_pt = meta.pdf_width_pt or (meta.width_px * 72.0 / settings.pdf_render_dpi)
    h_pt = meta.pdf_height_pt or (meta.height_px * 72.0 / settings.pdf_render_dpi)

    lines: list[tuple[str, list[float]]] = []
    for line in content.splitlines():
        locs = _LOC_RE.findall(line)
        text = _LOC_RE.sub("", line).strip()
        bbox_pt: list[float] = []
        if len(locs) >= 4 and len(locs) % 2 == 0:
            # 兼容 rect/quad/poly：把全部顶点保守转换成轴对齐外接矩形。
            nums = [int(v) for v in locs]
            points = list(zip(nums[0::2], nums[1::2]))
            x1 = min(point[0] for point in points)
            y1 = min(point[1] for point in points)
            x2 = max(point[0] for point in points)
            y2 = max(point[1] for point in points)
            # LOC 空间 0-1000 → PDF pt
            bbox_pt = [
                x1 * w_pt / _LOC_SPACE,
                y1 * h_pt / _LOC_SPACE,
                x2 * w_pt / _LOC_SPACE,
                y2 * h_pt / _LOC_SPACE,
            ]

        lines.append((text, bbox_pt))

    blocks = _lines_to_blocks(lines, page_index)
    _log_parse_stats("loc", page_index, blocks)
    return blocks


def _location_key(text: str) -> str:
    """坐标挂载用比较键；只忽略排版空白和表格分隔符。"""
    return re.sub(r"[\s|]+", "", normalize_text(text))


def _bbox_coverage(blocks: list[Block]) -> float:
    total = sum(len(_location_key(block.content)) for block in blocks)
    if total <= 0:
        return 0.0
    located = sum(
        len(_location_key(block.content))
        for block in blocks
        if len(block.bbox) >= 4
    )
    return located / total


def _attach_spotting_bboxes(
    content_blocks: list[Block], spotting_blocks: list[Block]
) -> list[Block]:
    """按阅读顺序把 Spotting 行坐标挂到 OCR 内容块。

    OCR 内容仍是比对权威来源；Spotting 只提供 bbox。匹配保持单调，避免合同中
    重复短语把后续坐标抢走。低相似候选不挂载，宁可报告定位不完整也不画错框。
    """
    located = [block for block in spotting_blocks if len(block.bbox) >= 4]
    if not located:
        return content_blocks

    result = [block.model_copy(deep=True) for block in content_blocks]
    spot_keys = [_location_key(block.content) for block in located]
    cursor = 0
    for block in result:
        target = _location_key(block.content)
        if not target:
            continue
        match = _best_spotting_window(target, spot_keys, cursor)
        if match is None:
            continue
        start, end = match
        if len(block.bbox) < 4:
            block.bbox = _union_bbox(
                [candidate.bbox for candidate in located[start:end]]
            )
        cursor = end
    return result


def _best_spotting_window(
    target: str, spot_keys: list[str], cursor: int
) -> tuple[int, int] | None:
    best: tuple[float, int, int] | None = None
    max_window = 24
    for start in range(cursor, len(spot_keys)):
        combined = ""
        for end in range(start, min(len(spot_keys), start + max_window)):
            combined += spot_keys[end]
            if not combined:
                continue
            if combined == target:
                return start, end + 1
            length_ratio = min(len(target), len(combined)) / max(
                len(target), len(combined)
            )
            if length_ratio < 0.65:
                if len(combined) > len(target) * 1.6:
                    break
                continue
            ratio = SequenceMatcher(None, target, combined, autojunk=False).ratio()
            if target in combined or combined in target:
                ratio = max(ratio, length_ratio)
            candidate = (ratio, -start, end + 1)
            if best is None or candidate > best:
                best = candidate
            if len(combined) > len(target) * 1.6:
                break
    if best is None or best[0] < 0.90:
        return None
    return -best[1], best[2]


def _parse_plain_content(
    content: str, page_index: int, meta: PageMeta
) -> list[Block]:
    """解析 PaddleOCR-VL 的纯文本/Markdown 输出为 Block 列表。

    PaddleOCR-VL-1.5 在不同服务上可能返回多种带行结构的文本:
    - 表格以 Markdown ``|``、Tab 分隔，或使用 HTML ``<table>``
    - 段落/字段为普通文本行,段落间以空行分隔

    解析策略:
    - 连续的 ``|`` / Tab 分隔行(≥2 行,含表头 + 至少 1 数据行)归为一个
      ``label=table`` 的 Block，并构造 TableStructure(首行 headers,其余 rows)。
    - HTML table 解析为同一 TableStructure，保留表格前后的普通文本。
    - 其余非空文本行,每行一个 ``label=text`` 的 Block。
      (PaddleOCR-VL 的段落通常已是完整的一行,无需跨行合并。)

    bbox 无法从纯文本恢复(无坐标信息),留空 []。这不阻塞比对——
    对齐与 diff 走文本语义,不强依赖 bbox;仅影响 PDF 高亮定位精度。
    """
    blocks: list[Block] = []
    cursor = 0
    for match in _HTML_TABLE_RE.finditer(content):
        before = content[cursor:match.start()]
        blocks.extend(_lines_to_blocks(
            [(line.strip(), []) for line in before.splitlines()], page_index
        ))
        html_table = _parse_html_table(match.group(0))
        if html_table is not None:
            blocks.append(_table_block(page_index, html_table, []))
        else:
            blocks.extend(_lines_to_blocks([(match.group(0).strip(), [])], page_index))
        cursor = match.end()
    blocks.extend(_lines_to_blocks(
        [(line.strip(), []) for line in content[cursor:].splitlines()], page_index
    ))

    # 分段解析时各段从 0 编号，最终按页面顺序统一编号。
    for bid, block in enumerate(blocks):
        block.block_id = f"p{page_index}-b{bid}"
    _log_parse_stats("plain", page_index, blocks)
    return blocks


def _text_block(
    page_index: int, bid: int, text: str, bbox: list[float] | None = None
) -> Block:
    return Block(
        block_id=f"p{page_index}-b{bid}",
        page_index=page_index,
        label="text",
        bbox=bbox or [],
        content=text,
    )


def _plain_text_to_table(table_text: str) -> TableStructure | None:
    """把 Markdown / ``cell | cell`` / TSV 文本转为 TableStructure。

    根据表头判断是否使用 Markdown 外层包裹管线，保留数据行开头和结尾的
    空单元格，避免合并单元格续行发生列左移。空文本返回 None。
    """
    if not table_text.strip():
        return None
    lines = [line.strip() for line in table_text.splitlines() if line.strip()]
    lines = [
        line for line in lines
        if not (_MD_SEPARATOR_RE.fullmatch(line) and "-" in line)
    ]
    if not lines:
        return None

    header = lines[0]
    wrapped_pipes = (
        "\t" not in header and header.startswith("|") and header.endswith("|")
    )
    rows: list[list[str]] = []
    for line in lines:
        if "\t" in line and "|" not in line:
            cells = [cell.strip() for cell in line.split("\t")]
        else:
            cells = [cell.strip() for cell in line.split("|")]
            if wrapped_pipes:
                if cells and cells[0] == "":
                    cells.pop(0)
                if cells and cells[-1] == "":
                    cells.pop()
        if any(cells):
            rows.append(cells)
    return _rows_to_table(_collapse_repeated_rows(rows))


def _collapse_repeated_rows(rows: list[list[str]]) -> list[list[str]]:
    """清除模型退化时连续重复到 token 上限的表格尾部。"""
    if len(rows) < 2:
        return rows
    result = [rows[0]]
    dropped = 0
    i = 1
    while i < len(rows):
        j = i + 1
        while j < len(rows) and rows[j] == rows[i]:
            j += 1
        count = j - i
        if count >= _REPEATED_ROW_THRESHOLD:
            result.append(rows[i])
            dropped += count - 1
        else:
            result.extend(rows[i:j])
        i = j
    if dropped:
        logger.warning("paddleocr collapsed repeated table rows dropped=%s", dropped)
    return result


def _lines_to_blocks(
    lines: list[tuple[str, list[float]]], page_index: int
) -> list[Block]:
    """将带可选坐标的 OCR 行聚合为普通文本块或结构化表格块。"""
    blocks: list[Block] = []
    i = 0
    while i < len(lines):
        text, bbox = lines[i]
        text = text.strip()
        if not text:
            i += 1
            continue

        if _TABLE_LINE_RE.search(text):
            run: list[tuple[str, list[float]]] = []
            j = i
            while j < len(lines):
                candidate, candidate_bbox = lines[j]
                candidate = candidate.strip()
                if not candidate:  # PaddleOCR 常在表格行之间插入空行
                    j += 1
                    continue
                if not _TABLE_LINE_RE.search(candidate):
                    break
                run.append((candidate, candidate_bbox))
                j += 1

            # 至少两条分隔行，且去掉 Markdown 分隔线后仍有表头和数据行。
            raw_table = "\n".join(row for row, _ in run)
            table = _plain_text_to_table(raw_table) if len(run) >= 2 else None
            if table is not None and table.rows:
                blocks.append(_table_block(
                    page_index, table, _union_bbox([box for _, box in run])
                ))
            else:
                for row, row_bbox in run:
                    blocks.append(_text_block(page_index, len(blocks), row, row_bbox))
            i = max(j, i + 1)
            continue

        blocks.append(_text_block(page_index, len(blocks), text, bbox))
        i += 1

    for bid, block in enumerate(blocks):
        block.block_id = f"p{page_index}-b{bid}"
    return blocks


def _table_block(
    page_index: int, table: TableStructure, bbox: list[float]
) -> Block:
    rows = [table.headers, *table.rows]
    content = "\n".join(" | ".join(row) for row in rows)
    return Block(
        block_id="",
        page_index=page_index,
        label="table",
        bbox=bbox,
        content=content,
        table=table,
    )


def _rows_to_table(rows: list[list[str]]) -> TableStructure | None:
    if not rows:
        return None
    width = max(len(row) for row in rows)
    if width < 2:
        return None
    padded = [row + [""] * (width - len(row)) for row in rows]
    return TableStructure(headers=padded[0], rows=padded[1:])


def _union_bbox(boxes: list[list[float]]) -> list[float]:
    valid = [box for box in boxes if len(box) >= 4]
    if not valid:
        return []
    return [
        min(box[0] for box in valid),
        min(box[1] for box in valid),
        max(box[2] for box in valid),
        max(box[3] for box in valid),
    ]


class _HTMLTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def _parse_html_table(fragment: str) -> TableStructure | None:
    parser = _HTMLTableParser()
    parser.feed(fragment)
    parser.close()
    return _rows_to_table(parser.rows)


def _log_parse_stats(fmt: str, page_index: int, blocks: list[Block]) -> None:
    tables = sum(block.label == "table" for block in blocks)
    with_bbox = sum(len(block.bbox) >= 4 for block in blocks)
    logger.info(
        "paddleocr parsed format=%s page=%s blocks=%s table_blocks=%s "
        "blocks_with_bbox=%s blocks_without_bbox=%s",
        fmt, page_index, len(blocks), tables, with_bbox, len(blocks) - with_bbox,
    )


class _BoundedConcurrency:
    """极简线程池(复用 LLMOCREngine 的实现,保持独立避免循环依赖)。"""

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
        except BaseException as exc:  # noqa: BLE001
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
