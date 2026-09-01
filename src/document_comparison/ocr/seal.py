"""彩色印章检测与确定性颜色抑制。

这里只处理图像证据中仍然存在的颜色信息，不做修复、补字或生成式推断。
红章抑制图保持原始像素尺寸，便于 OCR 结果继续映射到同一 PDF 坐标系。
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from io import BytesIO
import re

import numpy as np
from PIL import Image, ImageFilter

from ..models import Block, PageMeta


@dataclass(frozen=True)
class SealRegion:
    """图像像素坐标中的印章候选矩形。"""

    bbox_px: tuple[int, int, int, int]
    red_pixel_count: int


@dataclass(frozen=True)
class PreparedSealVariant:
    """保持页面尺寸不变的红章抑制 OCR 输入。"""

    png_bytes: bytes
    width_px: int
    height_px: int
    regions_px: tuple[SealRegion, ...]


@dataclass(frozen=True)
class SealRecoveryDiagnostic:
    """单页印章二次识别结果，供可信读取层合并质量语义。"""

    reliable: bool
    reasons: tuple[str, ...]
    region_count: int


@dataclass(frozen=True)
class SealMergeResult:
    blocks: tuple[Block, ...]
    diagnostic: SealRecoveryDiagnostic


def prepare_seal_variant(png_bytes: bytes) -> PreparedSealVariant | None:
    """检测红章并返回红通道增强图；没有可信候选时返回 ``None``。

    红色印章在红通道通常较亮，黑色正文在三个通道都较暗。区域内取红通道
    可以压低红章对 OCR 的干扰，同时保留仍有像素证据的黑字。完全遮挡的字符
    不会在这里被猜测或重建。
    """
    with Image.open(BytesIO(png_bytes)) as source:
        rgb_image = source.convert("RGB")
    rgb = np.asarray(rgb_image, dtype=np.uint8)
    height, width = rgb.shape[:2]
    if width == 0 or height == 0:
        return None

    red = rgb[:, :, 0].astype(np.int16)
    green = rgb[:, :, 1].astype(np.int16)
    blue = rgb[:, :, 2].astype(np.int16)
    red_mask = (
        (red >= 105)
        & ((red - green) >= 38)
        & ((red - blue) >= 38)
        & (red >= (green * 13 // 10))
        & (red >= (blue * 13 // 10))
    )
    minimum_red_pixels = max(80, int(width * height * 0.00012))
    if int(red_mask.sum()) < minimum_red_pixels:
        return None

    regions = _detect_regions(red_mask)
    if not regions:
        return None

    gray = np.asarray(rgb_image.convert("L"), dtype=np.uint8).copy()
    red_channel = rgb[:, :, 0]
    for region in regions:
        x1, y1, x2, y2 = _padded_bbox(region.bbox_px, width, height)
        gray[y1:y2, x1:x2] = np.maximum(
            gray[y1:y2, x1:x2], red_channel[y1:y2, x1:x2]
        )

    output = BytesIO()
    Image.fromarray(gray, mode="L").save(output, format="PNG", optimize=True)
    return PreparedSealVariant(
        png_bytes=output.getvalue(),
        width_px=width,
        height_px=height,
        regions_px=tuple(regions),
    )


def merge_seal_ocr_blocks(
    original_blocks: list[Block],
    recovered_blocks: list[Block],
    prepared: PreparedSealVariant,
    meta: PageMeta,
) -> SealMergeResult:
    """按印章区域融合两路 OCR，并给出保守的页级可信度。

    印章区域外保留原图结果；区域内只采用颜色抑制图结果。没有坐标时无法做
    局部证明，整页采用二次结果并将不一致标记为待复核。
    """
    original_content = [b for b in original_blocks if b.label != "seal"]
    recovered_content = [b for b in recovered_blocks if b.label != "seal"]
    regions_pt = _regions_to_pdf_points(prepared, meta)
    has_locations = all(
        len(block.bbox) >= 4 for block in [*original_content, *recovered_content]
    ) and bool([*original_content, *recovered_content])

    if not has_locations:
        original_text = _normalized_blocks_text(original_content)
        recovered_text = _normalized_blocks_text(recovered_content)
        reliable = bool(recovered_text) and _text_similarity(
            original_text, recovered_text
        ) >= 0.98
        reasons = (
            ("检测到印章，二次 OCR 结果缺少完整坐标或与原图不一致",)
            if not reliable
            else ("检测到印章，二次 OCR 与原图结果一致",)
        )
        return SealMergeResult(
            blocks=tuple(recovered_content),
            diagnostic=SealRecoveryDiagnostic(
                reliable=reliable,
                reasons=reasons,
                region_count=len(regions_pt),
            ),
        )

    outside = [
        block
        for block in original_content
        if not any(_bbox_intersects(block.bbox, region) for region in regions_pt)
    ]
    original_inside = [
        block
        for block in original_content
        if any(_bbox_intersects(block.bbox, region) for region in regions_pt)
    ]
    recovered_inside = [
        block
        for block in recovered_content
        if any(_bbox_intersects(block.bbox, region) for region in regions_pt)
    ]

    original_text = _normalized_blocks_text(original_inside)
    recovered_text = _normalized_blocks_text(recovered_inside)
    reliable = bool(recovered_text) and _text_similarity(
        original_text, recovered_text
    ) >= 0.98
    if reliable:
        reasons = ("检测到印章，覆盖区二次 OCR 与原图结果一致",)
    elif recovered_text:
        reasons = ("检测到印章，覆盖区二次 OCR 与原图结果不一致",)
    else:
        reasons = ("检测到印章，覆盖区文字无法可靠恢复",)

    merged = sorted(
        [*outside, *recovered_inside],
        key=lambda block: (
            block.page_index,
            block.bbox[1] if len(block.bbox) >= 4 else float("inf"),
            block.bbox[0] if len(block.bbox) >= 4 else float("inf"),
        ),
    )
    return SealMergeResult(
        blocks=tuple(merged),
        diagnostic=SealRecoveryDiagnostic(
            reliable=reliable,
            reasons=reasons,
            region_count=len(regions_pt),
        ),
    )


def seal_texts_agree(original_text: str, recovered_text: str) -> bool:
    """纯文本管线的保守一致性判断。"""
    original = re.sub(r"\s+", "", original_text or "")
    recovered = re.sub(r"\s+", "", recovered_text or "")
    return bool(recovered) and _text_similarity(original, recovered) >= 0.98


def _regions_to_pdf_points(
    prepared: PreparedSealVariant, meta: PageMeta
) -> list[tuple[float, float, float, float]]:
    width_pt = meta.pdf_width_pt or prepared.width_px * 72.0 / 200.0
    height_pt = meta.pdf_height_pt or prepared.height_px * 72.0 / 200.0
    scale_x = width_pt / prepared.width_px
    scale_y = height_pt / prepared.height_px
    return [
        (
            region.bbox_px[0] * scale_x,
            region.bbox_px[1] * scale_y,
            region.bbox_px[2] * scale_x,
            region.bbox_px[3] * scale_y,
        )
        for region in prepared.regions_px
    ]


def _bbox_intersects(
    bbox: list[float], region: tuple[float, float, float, float]
) -> bool:
    if len(bbox) < 4:
        return False
    x1, y1, x2, y2 = bbox[:4]
    rx1, ry1, rx2, ry2 = region
    return x1 < rx2 and x2 > rx1 and y1 < ry2 and y2 > ry1


def _normalized_blocks_text(blocks: list[Block]) -> str:
    return "\n".join(
        re.sub(r"\s+", "", block.content)
        for block in blocks
        if block.content and re.sub(r"\s+", "", block.content)
    )


def _text_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def _detect_regions(red_mask: np.ndarray) -> list[SealRegion]:
    height, width = red_mask.shape
    scale = max(2, min(6, round(max(width, height) / 500)))
    reduced_width = max(1, (width + scale - 1) // scale)
    reduced_height = max(1, (height + scale - 1) // scale)
    mask_image = Image.fromarray(red_mask.astype(np.uint8) * 255, mode="L")
    reduced = mask_image.resize(
        (reduced_width, reduced_height), Image.Resampling.BOX
    )
    # 连接同一印章中彼此分离的圆环、文字和五角星笔画。
    connected = reduced.filter(ImageFilter.MaxFilter(9))
    binary = np.asarray(connected, dtype=np.uint8) >= 16

    visited = np.zeros(binary.shape, dtype=bool)
    regions: list[SealRegion] = []
    for start_y, start_x in zip(*np.nonzero(binary & ~visited)):
        if visited[start_y, start_x]:
            continue
        stack = [(int(start_y), int(start_x))]
        visited[start_y, start_x] = True
        min_x = max_x = int(start_x)
        min_y = max_y = int(start_y)
        while stack:
            y, x = stack.pop()
            min_x = min(min_x, x)
            max_x = max(max_x, x)
            min_y = min(min_y, y)
            max_y = max(max_y, y)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    ny, nx = y + dy, x + dx
                    if (
                        0 <= ny < reduced_height
                        and 0 <= nx < reduced_width
                        and binary[ny, nx]
                        and not visited[ny, nx]
                    ):
                        visited[ny, nx] = True
                        stack.append((ny, nx))

        x1 = max(0, min_x * scale)
        y1 = max(0, min_y * scale)
        x2 = min(width, (max_x + 1) * scale)
        y2 = min(height, (max_y + 1) * scale)
        box_width, box_height = x2 - x1, y2 - y1
        if box_width < max(40, int(width * 0.055)):
            continue
        if box_height < max(40, int(height * 0.035)):
            continue
        red_pixels = int(red_mask[y1:y2, x1:x2].sum())
        if red_pixels < max(80, int(width * height * 0.0001)):
            continue
        regions.append(
            SealRegion(
                bbox_px=(x1, y1, x2, y2),
                red_pixel_count=red_pixels,
            )
        )
    return regions


def _padded_bbox(
    bbox: tuple[int, int, int, int], width: int, height: int
) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = bbox
    padding = max(4, round(min(width, height) * 0.006))
    return (
        max(0, x1 - padding),
        max(0, y1 - padding),
        min(width, x2 + padding),
        min(height, y2 + padding),
    )
