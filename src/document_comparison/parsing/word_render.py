"""DOCX 的可视化渲染与原文版面定位。

DOCX 仍由 :mod:`word` 解析并参与合同结构比对；本模块只把同一文件渲染成
PDF，为原件侧高亮提供可验证的页码和坐标。绝不根据 OOXML 页序推断坐标。
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..models import Block, RawItem
from ..structure.normalize import normalize_text

logger = logging.getLogger(__name__)


class DocxRenderError(RuntimeError):
    """LibreOffice 未能生成可用 PDF。"""


def render_docx_to_pdf(
    source_path: str | Path,
    output_path: str | Path,
    *,
    executable: str = "soffice",
    timeout_seconds: float = 90.0,
) -> Path:
    """使用 LibreOffice headless 把 DOCX 渲染成派生 PDF。

    输出路径由任务层以 task_id 固定命名，原上传文件不会被改写。转换在输出目录
    的私有临时目录中完成，避免 LibreOffice 同时任务互相覆盖同名 ``source.pdf``。
    """
    source = Path(source_path)
    output = Path(output_path)
    if source.suffix.lower() != ".docx":
        raise DocxRenderError("原件不是 DOCX，不能使用 LibreOffice 渲染")
    if not source.is_file():
        raise DocxRenderError("DOCX 原件文件不存在")
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="dc-docx-render-", dir=output.parent) as tmp:
        out_dir = Path(tmp)
        profile_dir = out_dir / "libreoffice-profile"
        profile_dir.mkdir()
        command = [
            executable,
            "--headless",
            f"-env:UserInstallation={profile_dir.as_uri()}",
            "--convert-to",
            "pdf:writer_pdf_Export",
            "--outdir",
            str(out_dir),
            str(source),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise DocxRenderError("LibreOffice 渲染器不可用(未找到 soffice)") from exc
        except subprocess.TimeoutExpired as exc:
            raise DocxRenderError(f"LibreOffice 渲染超时({timeout_seconds:g}s)") from exc

        rendered = out_dir / f"{source.stem}.pdf"
        if completed.returncode != 0 or not rendered.is_file() or rendered.stat().st_size == 0:
            logger.warning(
                "docx render failed source=%s exit=%s has_output=%s",
                source.name,
                completed.returncode,
                rendered.is_file(),
            )
            raise DocxRenderError("LibreOffice 未生成可用 PDF")
        shutil.move(str(rendered), str(output))

    logger.info("docx rendered for source annotation source=%s output=%s", source.name, output.name)
    return output


def _match_key(text: str) -> str:
    """用于同源 DOCX/PDF 文本匹配的保守比较键。"""
    normalized = normalize_text(text or "")
    return re.sub(r"[\s,，。.;；:：、()（）\[\]【】]+", "", normalized).lower()


def _matched_blocks(needle: str, pages_blocks: list[list[Block]]) -> list[Block]:
    """返回可被原文文本精确验证的 PDF 块；多页歧义时宁可不返回。"""
    key = _match_key(needle)
    if len(key) < 2:
        return []
    candidates: dict[int, list[Block]] = {}
    for page_index, blocks in enumerate(pages_blocks):
        with_bbox = [block for block in blocks if len(block.bbox) >= 4 and block.content]
        direct = [block for block in with_bbox if key in _match_key(block.content)]
        if direct:
            candidates[page_index] = direct
            continue
        # DOCX 一个段落在渲染 PDF 中被拆成多行时，联合同页文字并只取实际重叠块。
        joined = ""
        ranges: list[tuple[int, int, Block]] = []
        for block in with_bbox:
            block_key = _match_key(block.content)
            start = len(joined)
            joined += block_key
            ranges.append((start, len(joined), block))
        start = joined.find(key)
        if start >= 0:
            end = start + len(key)
            candidates[page_index] = [
                block for block_start, block_end, block in ranges
                if block_start < end and start < block_end
            ]

    # DOCX 的 OOXML 页序只是线索、并非渲染坐标；同一句出现在不同页时不能凭它猜测。
    if len(candidates) != 1:
        return []
    return next(iter(candidates.values()))


def attach_docx_layout(
    raw_items: list[RawItem], pages_blocks: list[list[Block]]
) -> tuple[list[RawItem], int]:
    """把 DOCX 原文项映射到渲染 PDF 的真实 bbox。

    ``RawItem`` 只承载一个 bbox；段落跨多块时选取第一个经文本验证的块，避免
    用包围大框覆盖相邻无关文字。剩余块不会伪造坐标，调用方会将报告置为部分可用。
    """
    mapped = 0
    located: list[RawItem] = []
    for item in raw_items:
        matches = _matched_blocks(item.text, pages_blocks)
        if matches:
            block = matches[0]
            located.append(item.model_copy(update={
                "page_index": block.page_index,
                "bbox": list(block.bbox),
            }))
            mapped += 1
        else:
            located.append(item)
    logger.info("docx layout mapping items=%s located=%s", len(raw_items), mapped)
    return located, mapped
