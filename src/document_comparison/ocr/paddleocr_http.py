"""PaddleOCR-VL 引擎:通过 OpenAI 兼容 /chat/completions 接口调用 PaddleOCR-VL 模型。

PaddleOCR-VL 是专用 OCR 模型,**不遵循 system prompt 的 JSON 格式指令**。
其实际输出取决于模型版本与调用方式,本引擎按返回内容自适应解析:

1. **Markdown 纯文本**(PaddleOCR-VL-1.5 在 SiliconFlow 等平台上的实际行为):
   返回带行结构的纯文本,表格以 Markdown `|` 分隔(含 `---` 分隔行),段落间空行分隔。
   → 由 `_parse_plain_content` 解析:连续 `|` 行归为一个 `label=table` 的 Block
     (构造 TableStructure),其余行归为 `label=text` 的 Block。

2. **文字行 + `<|LOC_|>` 坐标标记**(早期约定):
   每行文字后跟 8 个 `<|LOC_N|>` token,构成四角点坐标:
       [x1, y1, x2, y1, x2, y2, x1, y2](0-1000 归一化空间)
   → 由 `_parse_loc_content` 解析,每行一个带 bbox 的 Block。

本引擎在拿到响应后检测是否含 `<|LOC_` 标记:有则走 LOC 分支,否则走 plain 分支,
对 PaddleOCR-VL 各版本兼容。

关键调用约定:
- user 消息只发图片 + 简短「OCR」指令(不发复杂 system prompt,模型不遵循)
- **必须限制 `max_tokens`**:PaddleOCR-VL 在表格行上不自我停止,会重复 hallucination
  直到 token 上限,导致合计行/后续内容丢失(实测 200DPI 大图 385 行死循环)。
- 不带 `response_format`(强制 JSON 会让该模型陷入构造死循环)

配置:复用 llm_*(llm_api_base / llm_api_key / llm_model),与 llm 后端共用
同一套推理服务连接。当 ocr_backend=paddleocr 时由 get_ocr_engine() 路由到
本引擎——用户只需填一套推理服务配置,切换 ocr_backend 即改变解析逻辑。
"""
from __future__ import annotations

import base64
import logging
import random
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import httpx

from ..config import settings
from ..models import Block, PageMeta, TableStructure
from ..parsing.pdf import render_pages
from ..structure.normalize import normalize_table_text
from .base import ProgressCb

logger = logging.getLogger(__name__)

# <|LOC_数字|> token 正则
_LOC_RE = re.compile(r"<\|LOC_(\d+)\|>")

# LOC 坐标空间:PaddleOCR-VL 使用 0-1000 归一化(与 Qwen-VL 一致)
_LOC_SPACE = 1000.0

# 限制生成长度:PaddleOCR-VL 在表格行上不自我停止,会重复 hallucination
# 直到 token 上限,导致合计行/后续内容丢失。实测 2000 足够覆盖单页合同
# (含表格)的全部内容,过小会截断尾部,过大会放任死循环。
_MAX_TOKENS = 2000

# 表格行:含至少一个 |(Markdown 表格的单元格分隔)。用于识别连续表格行。
_TABLE_LINE_RE = re.compile(r"\|")


class PaddleOCREngine:
    """通过 OpenAI 兼容 /chat/completions 调用 PaddleOCR-VL。

    与 LLMOCREngine 的区别:
    - 请求:user 消息只发图片 + 简单指令(不发 system prompt 要求 JSON 格式)
    - 响应:解析 `<|LOC_|>` 标记格式,而非 JSON blocks
    - 坐标:LOC 坐标 0-1000 → 换算为 PDF pt(复用 _to_pt_bbox 逻辑)
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
        # paddleocr 后端复用 llm_* 配置(与 llm 后端同协议、同连接,仅解析逻辑不同):
        # 都走 OpenAI 兼容 /chat/completions,只是请求指令与响应解析不同。
        # 用户只需在设置页填一套推理服务配置,切换 ocr_backend 即可。
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

    # —— 公共接口(与 LLMOCREngine 对称)——
    def recognize(
        self,
        pdf_path: Path,
        page_metas: list[PageMeta],
        *,
        on_progress: ProgressCb | None = None,
    ) -> list[list[Block]]:
        if not self.api_base:
            raise RuntimeError(
                "llm_api_base 未配置,请在设置页填写推理服务地址"
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
        total = len(images)
        done_count = [0]

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
        # 自适应解析:含 <|LOC_ 标记走 LOC 分支(带 bbox),否则走 plain 分支
        # (Markdown 表格/段落文本,PaddleOCR-VL-1.5 的实际输出格式)。
        if "<|LOC_" in content:
            blocks = _parse_loc_content(content, page_index, meta)
        else:
            blocks = _parse_plain_content(content, page_index, meta)
        out[page_index] = blocks
        if on_progress:
            done_count[0] += 1
            frac = 0.10 + (done_count[0] / total_pages) * 0.60
            on_progress("ocr", frac)

    def _chat(self, data_url: str) -> str:
        """发送多模态请求,返回模型原始文本(含 <|LOC_|> 标记)。"""
        url = self.api_base.rstrip("/") + "/chat/completions"
        # PaddleOCR-VL 不遵循复杂 system prompt,用最简指令
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "OCR"},
                        {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}},
                    ],
                }
            ],
            "temperature": 0,
            # 限制生成长度:见模块 docstring,PaddleOCR-VL 在表格行上不自我停止,
            # 会重复 hallucination 刷到默认上限,导致后续内容(合计行/签字页)丢失。
            "max_tokens": _MAX_TOKENS,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        timeout = httpx.Timeout(self.timeout, connect=10.0)

        last_exc: Exception | None = None
        with httpx.Client(timeout=timeout) as client:
            for attempt in range(self.max_retries + 1):
                try:
                    resp = client.post(url, json=payload, headers=headers)
                except httpx.TransportError as exc:
                    last_exc = exc
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
                        logger.warning(
                            "paddleocr transient http status=%s attempt=%s/%s",
                            resp.status_code, attempt + 1, self.max_retries + 1,
                        )
                    else:
                        resp.raise_for_status()
                        data = resp.json()
                        return data["choices"][0]["message"]["content"]
                if attempt < self.max_retries:
                    backoff = min(2 ** attempt, 8) + random.random()
                    logger.info("paddleocr retry after %.1fs", backoff)
                    time.sleep(backoff)
        assert last_exc is not None
        logger.error(
            "paddleocr give up after %s attempts: %s", self.max_retries + 1, last_exc
        )
        raise last_exc


# —— 解析函数 ——


def _to_data_url(png_bytes: bytes) -> str:
    b64 = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _parse_loc_content(
    content: str, page_index: int, meta: PageMeta
) -> list[Block]:
    """解析 PaddleOCR-VL 的「文字行 + <|LOC_|> 标记」格式为 Block 列表。

    格式:每行 = 文字 + 8个 <|LOC_N|> token(四角点坐标,0-1000 空间)
    示例:合同标题<|LOC_146|><|LOC_89|><|LOC_499|><|LOC_89|><|LOC_499|><|LOC_121|><|LOC_146|><|LOC_121|>
    """
    # 页面尺寸:优先用 PDF 点坐标;若为 0 退回像素(并按 DPI 反算)
    w_pt = meta.pdf_width_pt or (meta.width_px * 72.0 / settings.pdf_render_dpi)
    h_pt = meta.pdf_height_pt or (meta.height_px * 72.0 / settings.pdf_render_dpi)

    blocks: list[Block] = []
    for line in content.splitlines():
        locs = _LOC_RE.findall(line)
        text = _LOC_RE.sub("", line).strip()
        if not text:
            continue

        bbox_pt: list[float] = []
        if len(locs) >= 8:
            # 四角点:[x1,y1, x2,y1, x2,y2, x1,y2] → 取 [x1,y1,x2,y2]
            nums = [int(v) for v in locs[:8]]
            x1, y1 = nums[0], nums[1]
            # x2,y2 在第 4、6 个位置(nums[4]=x2, nums[5]=y2)
            x2, y2 = nums[4], nums[5]
            # LOC 空间 0-1000 → PDF pt
            bbox_pt = [
                x1 * w_pt / _LOC_SPACE,
                y1 * h_pt / _LOC_SPACE,
                x2 * w_pt / _LOC_SPACE,
                y2 * h_pt / _LOC_SPACE,
            ]

        blocks.append(
            Block(
                block_id=f"p{page_index}-b{len(blocks)}",
                page_index=page_index,
                label="text",
                bbox=bbox_pt,
                content=text,
            )
        )

    if blocks:
        logger.debug(
            "paddleocr parsed page=%s blocks=%s first=%s",
            page_index, len(blocks), blocks[0].content[:40],
        )
    return blocks


def _parse_plain_content(
    content: str, page_index: int, meta: PageMeta
) -> list[Block]:
    """解析 PaddleOCR-VL 的纯文本/Markdown 输出为 Block 列表。

    PaddleOCR-VL-1.5 在 SiliconFlow 等平台上返回带行结构的纯文本:
    - 表格以 Markdown ``|`` 分隔(首行表头,可能有 ``---`` 分隔行,每行单元格数对齐)
    - 段落/字段为普通文本行,段落间以空行分隔

    解析策略:
    - 连续的 ``|`` 分隔行(≥2 行,含表头 + 至少 1 数据行)归为一个
      ``label=table`` 的 Block,用 normalize_table_text 归一化后构造
      TableStructure(首行 headers,其余 rows)。
    - 其余非空文本行,每行一个 ``label=text`` 的 Block。
      (PaddleOCR-VL 的段落通常已是完整的一行,无需跨行合并。)

    bbox 无法从纯文本恢复(无坐标信息),留空 []。这不阻塞比对——
    对齐与 diff 走文本语义,不强依赖 bbox;仅影响 PDF 高亮定位精度。
    """
    lines = content.splitlines()
    blocks: list[Block] = []
    bid = 0

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        # 收集连续的表格行(含 | 分隔)
        if _TABLE_LINE_RE.search(line):
            table_lines: list[str] = []
            while i < len(lines) and _TABLE_LINE_RE.search(lines[i].strip()):
                tl = lines[i].strip()
                if tl:
                    table_lines.append(tl)
                i += 1
            # 至少 2 行(表头 + 1 数据行)才算表格,否则当普通文本
            if len(table_lines) >= 2:
                norm = normalize_table_text("\n".join(table_lines))
                if norm.strip():
                    tbl = _plain_text_to_table(norm)
                    blocks.append(
                        Block(
                            block_id=f"p{page_index}-b{bid}",
                            page_index=page_index,
                            label="table",
                            bbox=[],
                            content=norm,
                            table=tbl,
                        )
                    )
                    bid += 1
                    continue
            # 不足 2 行或归一化后为空,回退当普通文本处理
            for tl in table_lines:
                if tl.strip():
                    blocks.append(_text_block(page_index, bid, tl.strip()))
                    bid += 1
            continue

        # 普通文本行
        blocks.append(_text_block(page_index, bid, line))
        bid += 1
        i += 1

    if blocks:
        logger.debug(
            "paddleocr parsed(plain) page=%s blocks=%s first=%s",
            page_index, len(blocks), blocks[0].content[:40],
        )
    return blocks


def _text_block(page_index: int, bid: int, text: str) -> Block:
    return Block(
        block_id=f"p{page_index}-b{bid}",
        page_index=page_index,
        label="text",
        bbox=[],
        content=text,
    )


def _plain_text_to_table(norm_text: str) -> TableStructure | None:
    """把已归一化的 ``cell | cell`` 多行文本转为 TableStructure。

    首行为 headers,其余为 rows。空文本返回 None。
    """
    if not norm_text.strip():
        return None
    rows = [line.split(" | ") for line in norm_text.splitlines() if line.strip()]
    if not rows:
        return None
    headers = rows[0]
    return TableStructure(headers=headers, rows=rows[1:])


class _BoundedConcurrency:
    """极简线程池(复用 LLMOCREngine 的实现,保持独立避免循环依赖)。"""

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
