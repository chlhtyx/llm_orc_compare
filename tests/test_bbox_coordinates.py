from document_comparison.models import Block, PageMeta
from document_comparison.ocr.llm import _to_pt_bbox
from document_comparison.report.builder import _normalize_regions


def test_llm_absolute_bbox_pixels_convert_to_pdf_points():
    bbox = _to_pt_bbox(
        [300, 600, 900, 1200],
        w_pt=600,
        h_pt=800,
        width_px=3000,
        height_px=4000,
    )

    assert bbox == [60, 120, 180, 240]


def test_llm_normalized_bbox_convert_to_pdf_points():
    bbox = _to_pt_bbox(
        [0.1, 0.2, 0.3, 0.4],
        w_pt=600,
        h_pt=800,
        width_px=3000,
        height_px=4000,
    )

    assert bbox == [60, 160, 180, 320]


def test_llm_qwen1000_bbox_convert_to_pdf_points():
    # Qwen-VL 原生 [0,1000) 归一化区间
    bbox = _to_pt_bbox(
        [100, 200, 500, 800],
        w_pt=600,
        h_pt=800,
        width_px=3000,
        height_px=4000,
    )

    assert bbox == [60, 160, 300, 640]


def test_normalize_regions_sorts_and_clamps_bbox():
    regions = _normalize_regions(
        [
            Block(
                block_id="b1",
                page_index=0,
                label="text",
                bbox=[650, -10, 300, 900],
                content="x",
            )
        ],
        {
            0: PageMeta(
                page_index=0,
                width_px=3000,
                height_px=4000,
                pdf_width_pt=600,
                pdf_height_pt=800,
            )
        },
    )

    assert len(regions) == 1
    assert regions[0].bbox == [0.5, 0.0, 1.0, 1.0]
