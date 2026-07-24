"""RawBlock/RawSpan 联合分段对齐测试。"""
from __future__ import annotations

from document_comparison.align.raw_plan import align_raw_items
from document_comparison.embed.mock import MockEmbedding
from document_comparison.models import (
    PageMeta,
    RawAlignmentGroup,
    RawAlignmentPlan,
    RawItem,
    RawSpan,
)
from document_comparison.report import build_report


def test_joint_raw_plan_prevents_section_heading_from_becoming_party_change():
    """LLM 在原始段落层联合分段后，切分差异不能污染乙方字段。"""
    word_items = [
        RawItem(text="乙方：(供方)湖北欧朗机械有限公司"),
        RawItem(text="甲乙双方本着平等互利,诚实信用的原则,经过协商一致订立本合同,恪守合同规定。"),
        RawItem(text="一 、 合同标的"),
    ]
    pdf_items = [
        RawItem(text="乙方：(供方)湖北欧朗机械有限公司", bbox=[10, 10, 200, 30]),
        RawItem(
            text="甲乙双方本着平等互利,诚实信用的原则,经过协商一致订立本合同,恪守合同规定。",
            bbox=[10, 30, 500, 50],
        ),
        RawItem(text="一、合同标的", bbox=[10, 50, 150, 70]),
    ]

    def planner(word_blocks, pdf_blocks):
        assert [block.block_id for block in word_blocks] == ["w-1", "w-2", "w-3"]
        assert [block.block_id for block in pdf_blocks] == ["p-1", "p-2", "p-3"]
        return RawAlignmentPlan(
            groups=[
                RawAlignmentGroup(
                    word_spans=[RawSpan(block_id="w-1", start=0, end=len(word_items[0].text))],
                    pdf_spans=[RawSpan(block_id="p-1", start=0, end=len(pdf_items[0].text))],
                    confidence=0.99,
                    reason="乙方主体字段对应",
                ),
                RawAlignmentGroup(
                    word_spans=[RawSpan(block_id="w-2", start=0, end=len(word_items[1].text))],
                    pdf_spans=[RawSpan(block_id="p-2", start=0, end=len(pdf_items[1].text))],
                    confidence=0.99,
                    reason="合同前言对应",
                ),
                RawAlignmentGroup(
                    word_spans=[RawSpan(block_id="w-3", start=0, end=len(word_items[2].text))],
                    pdf_spans=[RawSpan(block_id="p-3", start=0, end=len(pdf_items[2].text))],
                    confidence=0.99,
                    reason="合同标的标题对应",
                ),
            ]
        )

    result = align_raw_items(
        word_items,
        pdf_items,
        embed=MockEmbedding(),
        planner=planner,
    )

    assert result is not None
    report = build_report(
        alignments=result.alignments,
        word_by=result.word_by,
        pdf_by=result.pdf_by,
        embed=MockEmbedding(),
        page_metas=[
            PageMeta(
                page_index=0,
                width_px=595,
                height_px=842,
                pdf_width_pt=595,
                pdf_height_pt=842,
            )
        ],
        thresholds={"identical": 0.995, "modified": 0.85},
        source="source.docx",
        target="target.pdf",
    )

    assert report.change_status == "clean"
    assert report.diffs == []
    assert report.unmatched_clauses == []
    assert [clause.text for clause in result.word_by.values()] == [
        "乙方：(供方)湖北欧朗机械有限公司",
        word_items[1].text,
        "一 、 合同标的",
    ]


def test_joint_raw_plan_can_split_multiple_sections_inside_one_ocr_block():
    word_items = [
        RawItem(text="第一条 合同标的"),
        RawItem(text="设备一套。"),
        RawItem(text="第二条 合同金额"),
        RawItem(text="金额100万元。"),
    ]
    merged = "第一条 合同标的设备一套。第二条 合同金额金额100万元。"
    pdf_items = [
        RawItem(text=merged, bbox=[10, 10, 500, 100]),
    ]
    cuts = [
        0,
        len("第一条 合同标的"),
        len("第一条 合同标的设备一套。"),
        len("第一条 合同标的设备一套。第二条 合同金额"),
        len(merged),
    ]

    def planner(word_blocks, pdf_blocks):
        return RawAlignmentPlan(
            groups=[
                RawAlignmentGroup(
                    word_spans=[
                        RawSpan(
                            block_id=f"w-{index}",
                            start=0,
                            end=len(word_blocks[index - 1].text),
                        )
                    ],
                    pdf_spans=[
                        RawSpan(block_id="p-1", start=cuts[index - 1], end=cuts[index])
                    ],
                    confidence=0.98,
                    reason="同一原始段落",
                )
                for index in range(1, 5)
            ]
        )

    result = align_raw_items(
        word_items,
        pdf_items,
        embed=MockEmbedding(),
        planner=planner,
    )

    assert result is not None
    assert [clause.text for clause in result.pdf_by.values()] == [
        "第一条 合同标的",
        "设备一套。",
        "第二条 合同金额",
        "金额100万元。",
    ]
    assert all(
        clause.blocks[0].bbox == [10.0, 10.0, 500.0, 100.0]
        for clause in result.pdf_by.values()
    )


def test_joint_raw_plan_rejects_omitted_text_and_returns_fallback_signal():
    word_items = [RawItem(text="合同金额100万元")]
    pdf_items = [RawItem(text="合同金额100万元")]

    def incomplete_planner(_word_blocks, _pdf_blocks):
        return RawAlignmentPlan(
            groups=[
                RawAlignmentGroup(
                    word_spans=[RawSpan(block_id="w-1", start=0, end=4)],
                    pdf_spans=[RawSpan(block_id="p-1", start=0, end=4)],
                    confidence=0.99,
                    reason="故意遗漏金额",
                )
            ]
        )

    assert (
        align_raw_items(
            word_items,
            pdf_items,
            embed=MockEmbedding(),
            planner=incomplete_planner,
        )
        is None
    )


def test_joint_raw_plan_cannot_hide_changed_party_label():
    word_items = [RawItem(text="乙方：湖北欧朗机械有限公司")]
    pdf_items = [RawItem(text="甲方：湖北欧朗机械有限公司", bbox=[10, 10, 200, 30])]

    def planner(word_blocks, pdf_blocks):
        return RawAlignmentPlan(
            groups=[
                RawAlignmentGroup(
                    word_spans=[
                        RawSpan(block_id="w-1", start=0, end=len(word_blocks[0].text))
                    ],
                    pdf_spans=[
                        RawSpan(block_id="p-1", start=0, end=len(pdf_blocks[0].text))
                    ],
                    confidence=0.99,
                    reason="模型错误地把不同角色配在一起",
                )
            ]
        )

    result = align_raw_items(
        word_items,
        pdf_items,
        embed=MockEmbedding(),
        planner=planner,
    )
    assert result is not None
    report = build_report(
        alignments=result.alignments,
        word_by=result.word_by,
        pdf_by=result.pdf_by,
        embed=MockEmbedding(),
        page_metas=[],
        thresholds={"identical": 0.995, "modified": 0.85},
        source="source.docx",
        target="target.pdf",
    )

    assert report.change_status == "changed"
    changed_segments = [
        (segment.op, segment.text)
        for diff in report.diffs
        for segment in diff.segments
        if segment.op != "equal"
    ]
    assert ("delete", "乙") in changed_segments
    assert ("insert", "甲") in changed_segments
