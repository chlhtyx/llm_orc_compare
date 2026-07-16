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

配置:独立使用 paddleocr_*(paddleocr_api_base / paddleocr_api_key / paddleocr_model),
与 llm 后端的配置完全隔离。当 ocr_backend=paddleocr 时由 get_ocr_engine() 路由到
本引擎。未配置 paddleocr_api_base 时直接报错,不回退 llm_*。
"""
from __future__ import annotations

import base64
import logging
import random
import re
import threading
import time
from collections import deque
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx

from ..config import settings
from ..models import Block, PageMeta, TableStructure
from ..observability import log_model_request, log_model_response
from ..parsing.pdf import render_pages
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

# 整图(长图)整体 OCR 的生成长度上限:无标注版把整篇拼成一张长图,
# 单次调用需容纳全文,按整篇合同量级放宽到 8000。
_WHOLE_DOC_MAX_TOKENS = 8000

# 表格行:含至少一个 |(Markdown 表格的单元格分隔)。用于识别连续表格行。
_TABLE_LINE_RE = re.compile(r"\||\t")
_HTML_TABLE_RE = re.compile(r"<table\b[\s\S]*?</table\s*>", re.IGNORECASE)
_MD_SEPARATOR_RE = re.compile(r"^[\s:|\-]+$")
_REPEATED_ROW_THRESHOLD = 4


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
        # paddleocr 后端使用独立的 paddleocr_* 配置(与 llm 后端同协议、同连接方式,
        # 但配置项完全隔离,不复用 llm_*)。
        self.api_base = api_base or settings.paddleocr_api_base
        self.api_key = api_key if api_key is not None else settings.paddleocr_api_key
        self.model = model or settings.paddleocr_model
        self.timeout = timeout if timeout is not None else settings.paddleocr_timeout
        self.max_concurrency = (
            max_concurrency if max_concurrency is not None else settings.paddleocr_max_concurrency
        )
        self.max_retries = (
            max_retries if max_retries is not None else settings.paddleocr_max_retries
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

    def recognize_text(
        self, image_bytes: bytes, *, client: httpx.Client | None = None
    ) -> str:
        """单次 OCR 取纯文本(无标注版扫描件识别用)。

        与逐页结构化 `recognize` 的区别:只要纯文本(剥除 <|LOC_|> 坐标标记),
        用更大的 max_tokens 容纳内容。既可处理整张长图,也可处理单页;无标注版
        扫描件逐页并发调用本方法、最后按序拼接成整篇纯文本。

        `client` 可传入共享的 httpx.Client 以复用连接池(并发场景)。
        """
        if not self.api_base:
            raise RuntimeError(
                "paddleocr_api_base 未配置,请在设置页填写专用 OCR 模型的推理服务地址"
            )
        data_url = _to_data_url(image_bytes)
        content = self._chat(data_url, max_tokens=_MAX_TOKENS, client=client)
        # 剥除可能的 <|LOC_N|> 坐标标记,只留文字
        return _LOC_RE.sub("", content)

    def _chat(
        self,
        data_url: str,
        *,
        max_tokens: int = _MAX_TOKENS,
        client: httpx.Client | None = None,
    ) -> str:
        """发送多模态请求,返回模型原始文本(含 <|LOC_|> 标记)。

        `client` 可传入共享的 httpx.Client 以复用连接池(并发场景);不传则自建并关闭。
        """
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
            "max_tokens": max_tokens,
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
                        log_model_response(logger, "paddleocr", resp.status_code, data, request_started)
                        choice = data["choices"][0]
                        content = choice["message"]["content"]
                        if choice.get("finish_reason") == "length":
                            logger.warning(
                                "paddleocr response truncated at max_tokens=%s chars=%s; "
                                "parser will remove repeated/empty table tail but omitted "
                                "source content cannot be recovered",
                                max_tokens, len(content),
                            )
                        return content
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

    lines: list[tuple[str, list[float]]] = []
    for line in content.splitlines():
        locs = _LOC_RE.findall(line)
        text = _LOC_RE.sub("", line).strip()
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

        lines.append((text, bbox_pt))

    blocks = _lines_to_blocks(lines, page_index)
    _log_parse_stats("loc", page_index, blocks)
    return blocks


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
