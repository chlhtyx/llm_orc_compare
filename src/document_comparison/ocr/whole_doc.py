"""整篇文档读取:无标注版管线专用。

与逐页结构化 OCR(`TrustedPDFReader`/`LLMOCREngine`)不同,这里面向"纯文本比对"
场景,目标是把一份 PDF 读成**一段纯文本**:

1. 先做**整篇文字层累计判定**:遍历全部页累计原生文本字符数,达标即认为该 PDF
   带可靠文字层,直接拼接原生文本(逐页复用 `read_native_page`,保留阅读顺序与表格
   结构化文本)。
2. 未达标(扫描件)→ **逐页渲染 PNG 并发 OCR**(`recognize_text`),按页序拼接成整篇
   纯文本。识别阶段并发(受引擎 max_concurrency 限制),只在最后拼文本时保证阅读顺序,
   兼顾速度与"整体输出纯文本"的语义。

本模块不产出 `list[list[Block]]`,只产出纯文本 + 一条整篇级读取诊断,供
`raw_pipeline` 直接消费。
"""
from __future__ import annotations

import logging
import contextvars
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import httpx

try:  # PyMuPDF 新版包名 pymupdf,旧版为 fitz
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz  # type: ignore[import-untyped]

from ..config import settings
from ..models import PageRecognitionDiagnostic
from ..observability import timed_stage
from ..parsing.pdf import render_page, render_pages
from .native import read_native_page
from .seal import prepare_seal_variant, seal_texts_agree

logger = logging.getLogger(__name__)

# 整篇文字层达标的最低字符数(去空白后)。与 ocr/native.py 单页阈值一致:
# read_native_page 内部已对每页做 coverage 校验,过滤掉乱码/空白页,故整篇累计
# 达到单页最小读取量即认为该 PDF 带可靠文字层。真实扫描件整篇为 0,仍走 OCR。
_MIN_NATIVE_CHARS = 16


@dataclass
class WholeDocRead:
    """整篇读取结果。"""

    text: str
    reliable: bool
    source: str  # "native" | "fallback"
    diagnostic: PageRecognitionDiagnostic


def extract_native_full_text(pdf_path: str | Path) -> WholeDocRead:
    """整篇文字层累计判定 + 原生提取。

    遍历全部页,逐页用 `read_native_page` 取原生文本块(保留阅读顺序、表格用
    `cell | cell` 文本),累计去空白后字符数。全篇达标 → reliable,直接返回拼好文本;
    否则 reliable=False、text="",交由逐页并发 OCR 兜底。
    """
    path = Path(pdf_path)
    page_texts: list[str] = []
    total_chars = 0
    page_count = 0
    with fitz.open(str(path)) as doc:
        for page_index, page in enumerate(doc):
            page_count += 1
            native = read_native_page(page, page_index)
            page_text = "\n".join(b.content for b in native.blocks if b.content)
            page_texts.append(page_text)
            total_chars += len(_compact(page_text))

    full_text = "\n".join(t for t in page_texts if t)
    reliable = total_chars >= _MIN_NATIVE_CHARS

    reasons: list[str] = []
    if not reliable:
        if total_chars == 0:
            reasons.append("PDF 全篇文本层为空")
        else:
            reasons.append(f"PDF 全篇文本层字符过少({total_chars}<{_MIN_NATIVE_CHARS})")

    logger.info(
        "whole-doc native probe pages=%s total_chars=%s reliable=%s",
        page_count, total_chars, reliable,
    )
    return WholeDocRead(
        text=full_text if reliable else "",
        reliable=reliable,
        source="native",
        diagnostic=PageRecognitionDiagnostic(
            page_index=0,
            source="native",
            reliable=reliable,
            reasons=reasons,
            char_count=total_chars,
            table_count=0,
        ),
    )


def ocr_whole_document(
    pdf_path: str | Path,
    ocr_engine,
    *,
    on_progress=None,
) -> WholeDocRead:
    """扫描件兜底:逐页并发 OCR 取纯文本,按页序拼接成整篇。

    `ocr_engine` 需提供 `recognize_text(image_bytes, *, client) -> str`
    (见 LLMOCREngine / PaddleOCREngine)。逐页渲染 PNG 后并发识别,识别阶段受引擎
    `max_concurrency` 限制;最终把各页纯文本按页序拼接,保留阅读顺序。

    相比"拼长图单次调用",逐页并发把 N 页扫描件的 OCR 耗时从 ~单页×N 降到
    ~单页×⌈N/并发⌉,与标注版逐页 OCR 的并发度对齐。
    """
    path = Path(pdf_path)
    with timed_stage(logger, "whole_doc_render"):
        images = render_pages(path, settings.pdf_render_dpi)
    if not images:
        logger.info("whole-doc ocr pages=0 (empty pdf)")
        return WholeDocRead(
            text="",
            reliable=False,
            source="fallback",
            diagnostic=PageRecognitionDiagnostic(
                page_index=0, source="fallback", reliable=False,
                reasons=["PDF 无可渲染页面"], char_count=0, table_count=0,
            ),
        )

    max_concurrency = max(1, int(getattr(ocr_engine, "max_concurrency", 4)))
    timeout = float(getattr(ocr_engine, "timeout", settings.llm_timeout))
    logger.info(
        "whole-doc ocr pages=%s dpi=%s concurrency=%s engine=%s",
        len(images), settings.pdf_render_dpi, max_concurrency,
        type(ocr_engine).__name__,
    )

    # Pre-warm certifi CA bundle before spawning threads — certifi.where()
    # uses an unlocked global guard that races under concurrent access.
    try:
        import certifi
        certifi.where()
    except Exception:  # pragma: no cover
        pass

    total = len(images)
    results: list[str | None] = [None] * total
    seal_variants: list[bytes | None] = [None] * total
    seal_reliable: list[bool | None] = [None] * total
    if getattr(settings, "seal_recovery_enabled", False):
        recovery_dpi = max(
            settings.pdf_render_dpi,
            int(getattr(settings, "seal_recovery_dpi", 300)),
        )
        for page_index, image in enumerate(images):
            prepared = prepare_seal_variant(image)
            if prepared is None:
                continue
            if recovery_dpi != settings.pdf_render_dpi:
                high_res = render_page(path, page_index, dpi=recovery_dpi)
                prepared = prepare_seal_variant(high_res) or prepared
            seal_variants[page_index] = prepared.png_bytes
    done_count = [0]  # mutable counter for threads

    # 共享连接池:httpx.Client 跨线程可复用 TCP/TLS 连接,避免每页重新握手。
    limits = httpx.Limits(
        max_connections=max_concurrency,
        max_keepalive_connections=max_concurrency,
    )
    with httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0), limits=limits) as client:
        with _BoundedConcurrency(max_concurrency) as pool:
            for i, img in enumerate(images):
                pool.submit(
                    _recognize_page_text,
                    i, img, results, ocr_engine, client,
                    on_progress, total, done_count,
                    seal_variants[i], seal_reliable,
                )

    page_texts = [t for t in results if t]
    text = "\n".join(page_texts)
    char_count = len(_compact(text))

    reasons: list[str] = []
    if char_count == 0:
        reasons.append("整篇 OCR 未返回任何文本")
    conflicting_seal_pages = [
        page_index + 1
        for page_index, item in enumerate(seal_reliable)
        if item is False
    ]
    if conflicting_seal_pages:
        reasons.append(
            "检测到印章，二次 OCR 与原图不一致或失败，页码:"
            + ",".join(str(page) for page in conflicting_seal_pages)
        )
    logger.info(
        "whole-doc ocr done pages=%s chars=%s engine=%s",
        total, char_count, type(ocr_engine).__name__,
    )
    reliable = char_count > 0 and not conflicting_seal_pages
    return WholeDocRead(
        text=text,
        reliable=reliable,
        source="fallback",
        diagnostic=PageRecognitionDiagnostic(
            page_index=0,
            source="fallback",
            reliable=reliable,
            reasons=reasons,
            char_count=char_count,
            table_count=0,
        ),
    )


def _recognize_page_text(
    page_index: int,
    png_bytes: bytes,
    out: list[str | None],
    ocr_engine,
    client: httpx.Client | None,
    on_progress=None,
    total_pages: int = 1,
    done_count: list[int] | None = None,
    seal_variant: bytes | None = None,
    seal_reliable: list[bool | None] | None = None,
) -> None:
    """单页 OCR 取纯文本,写入 out[page_index]。并发任务的工作单元。"""
    text = ocr_engine.recognize_text(png_bytes, client=client)
    if seal_variant is not None:
        try:
            recovered_text = ocr_engine.recognize_text(
                seal_variant, client=client
            )
        except Exception as exc:  # noqa: BLE001 -- 原图 OCR 已成功，二次恢复只降级
            logger.warning(
                "whole-doc seal recovery failed page=%s error_type=%s",
                page_index,
                type(exc).__name__,
            )
            if seal_reliable is not None:
                seal_reliable[page_index] = False
        else:
            if seal_reliable is not None:
                seal_reliable[page_index] = seal_texts_agree(
                    text, recovered_text
                )
            text = recovered_text
    out[page_index] = text or ""
    if on_progress:
        done_count[0] += 1
        frac = 0.15 + (done_count[0] / total_pages) * 0.55
        on_progress("ocr", frac)


class _BoundedConcurrency:
    """极简线程池:submit 阻塞至有空闲槽位,退出时 join 全部。

    工作线程抛出的异常会被收集,并在 __exit__ 重新抛出,避免被 daemon
    线程默认 excepthook 静默吞掉(否则失败页会留下 None,污染下游结果)。
    与 ocr/llm.py 中的实现一致(此处复制以解耦)。
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


def _compact(text: str) -> str:
    """去除所有空白,用于字符计数。"""
    import re

    return re.sub(r"\s+", "", text)
