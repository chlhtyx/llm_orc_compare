"""条款切分测试。"""
from document_comparison.models import RawItem
from document_comparison.structure.clause import (
    build_clauses,
    detect_number,
    blocks_to_raw,
)
from document_comparison.models import Block


def test_detect_number_patterns():
    assert detect_number("第三条 甲方义务") == ("第三条", "三", 1)
    assert detect_number("1.1 总则") == ("1.1", "1.1", 2)
    assert detect_number("1.1.1 细则") == ("1.1.1", "1.1.1", 3)
    assert detect_number("（1）款项") == ("（1）", "1", 3)
    assert detect_number("一、总则") == ("一、", "一", 2)
    assert detect_number("第三条正文") is None  # 编号后须接分隔符或行尾
    assert detect_number("普通正文无编号") is None


def test_build_clauses_by_number():
    items = [
        RawItem(text="第一条 合同标的", kind="heading"),
        RawItem(text="甲方提供设备。"),
        RawItem(text="第二条 合同金额", kind="heading"),
        RawItem(text="金额为100万元。"),
    ]
    clauses = build_clauses(items, "word")
    assert len(clauses) == 2
    assert clauses[0].number == "一" or clauses[0].number == "1"
    # 第二条有金额
    assert "100万元" in clauses[1].text


def test_build_clauses_multiline_in_one_block():
    items = [
        RawItem(text="第三条\n第三条正文内容。\n第四条\n第四条正文。", kind="paragraph"),
    ]
    clauses = build_clauses(items, "pdf")
    assert len(clauses) == 2
    assert "第三条" in (clauses[0].title or clauses[0].text)


def test_build_clauses_splits_multiline_bbox_by_line():
    items = [
        RawItem(
            text="第三条 费用\n3.1 总额\n第四条 权利\n4.1 义务",
            kind="paragraph",
            page_index=0,
            bbox=[10, 20, 210, 100],
        ),
    ]

    clauses = build_clauses(items, "pdf")

    assert len(clauses) == 4
    assert clauses[0].blocks[0].bbox == [10, 20, 210, 40]
    assert clauses[1].blocks[0].bbox == [10, 40, 210, 60]
    assert clauses[2].blocks[0].bbox == [10, 60, 210, 80]
    assert clauses[3].blocks[0].bbox == [10, 80, 210, 100]


def test_blocks_to_raw_maps_labels():
    pages = [[
        Block(block_id="b1", page_index=0, label="doc_title", bbox=[0, 0, 1, 1], content="合同"),
        Block(block_id="b2", page_index=0, label="text", bbox=[0, 1, 1, 2], content="正文"),
        Block(block_id="b3", page_index=0, label="table", bbox=[0, 2, 1, 3], content="|"),
    ]]
    items = blocks_to_raw(pages)
    assert items[0].kind == "title"
    assert items[1].kind == "paragraph"
    assert items[2].kind == "table"
