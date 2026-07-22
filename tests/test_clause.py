"""条款切分测试。"""
from document_comparison.models import RawItem, TableStructure
from document_comparison.structure.clause import (
    build_clauses,
    detect_number,
    detect_field_key,
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


def test_detect_field_key_patterns():
    # 角色字段(可带括号限定语)
    assert detect_field_key("甲方(甲方主体):XX公司") is not None
    assert detect_field_key("甲方(甲方主体):XX公司")[1] == "甲方"
    assert detect_field_key("乙方(乙方主体):YY公司")[1] == "乙方"
    # 纯字段名
    assert detect_field_key("联系地址:上海市")[1] == "地址"
    assert detect_field_key("联系电话:13800")[1] == "电话"
    assert detect_field_key("开户行:工行")[1] == "开户行"
    assert detect_field_key("日期:2026年07月09日")[1] == "日期"
    assert detect_field_key("帐号:039401040006087")[1] == "账号"
    assert detect_field_key("签约代表:王斌")[1] == "签约代表"
    assert detect_field_key("时间:2026-07-13")[1] == "日期"
    # 复合字段名(含 /)
    assert detect_field_key("统一社会信用代码/身份证号:91310115")[1] == "统一社会信用代码"
    # 签字盖章
    assert detect_field_key("甲方(签字/盖章):XX公司")[1] == "甲方"
    # 非字段
    assert detect_field_key("通用两页样板合同") is None
    assert detect_field_key("本合同一式两份") is None


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


def test_build_clauses_splits_inline_numbered_and_field_boundaries():
    """原生 PDF 可能把多个逻辑起点放在同一文本行，仍需拆成独立条款。"""
    items = [
        RawItem(
            text=(
                "第一条合作内容 1.1 服务内容。1.2 支付方式。"
                "开户行:中国工商银行 账户名:甲公司"
            ),
            kind="paragraph",
            page_index=0,
            bbox=[10, 20, 310, 60],
        )
    ]

    clauses = build_clauses(items, "pdf")

    assert [clause.number for clause in clauses[:3]] == ["一", "1.1", "1.2"]
    assert [clause.field_key for clause in clauses[3:]] == ["开户行", "账户名"]
    assert clauses[1].text == "服务内容。"
    assert clauses[2].text == "支付方式。"


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


def test_build_clauses_splits_header_fields():
    """首部键值块按字段名拆成独立 clause,field_key 归一化。"""
    items = [
        RawItem(text="合同标题"),
        RawItem(text="甲方(甲方主体):XX公司"),
        RawItem(text="统一社会信用代码/身份证号:91310115MA1"),
        RawItem(text="联系地址:上海市浦东新区"),
        RawItem(text="联系电话:13800138000"),
        RawItem(text="乙方(乙方主体):YY公司"),
        RawItem(text="联系电话:13900139000"),
    ]
    clauses = build_clauses(items, "word")
    field_keys = [c.field_key for c in clauses if c.field_key]
    # 甲方、统一社会信用代码、地址、电话、乙方、电话
    assert "甲方" in field_keys
    assert "乙方" in field_keys
    assert "统一社会信用代码" in field_keys
    assert "地址" in field_keys
    # 两个联系电话都归一到「电话」
    assert field_keys.count("电话") == 2
    # 无字段无编号的标题行仍是独立 clause,field_key 为空
    assert any(c.field_key == "" and "合同标题" in c.text for c in clauses)


def test_build_clauses_preserves_unnumbered_merge():
    """既无编号又无字段名的连续行仍合并入当前条款(回归保护)。"""
    items = [
        RawItem(text="第一条 合作内容", kind="heading"),
        RawItem(text="正文第一行。"),
        RawItem(text="正文第二行。"),
    ]
    clauses = build_clauses(items, "word")
    assert len(clauses) == 1
    assert "正文第一行" in clauses[0].text
    assert "正文第二行" in clauses[0].text


def test_build_clauses_ignores_pdf_markdown_heading_markers():
    """OCR Markdown 的标题语法不能变成合同正文或阻断编号识别。"""
    items = [
        RawItem(
            text="# 通用两页样板合同",
            kind="paragraph",
            page_index=0,
            bbox=[10, 10, 210, 30],
        ),
        RawItem(text="## 第一条 合作内容", kind="paragraph"),
        RawItem(text="### 1.1 服务内容", kind="paragraph"),
        RawItem(text="服务正文。", kind="paragraph"),
    ]

    clauses = build_clauses(items, "pdf")

    assert [(clause.number, clause.title) for clause in clauses] == [
        ("", ""),
        ("一", "合作内容"),
        ("1.1", "服务内容"),
    ]
    assert clauses[0].text == "通用两页样板合同"
    assert clauses[2].text == "服务内容\n服务正文。"
    # 只清理用于比较的文本；定位证据仍保留 OCR 原文及坐标。
    assert clauses[0].blocks[0].content == "# 通用两页样板合同"
    assert clauses[0].blocks[0].bbox == [10, 10, 210, 30]


def test_build_clauses_does_not_strip_literal_hash_text_or_word_source():
    pdf = build_clauses([RawItem(text="#合同编号 A-001")], "pdf")
    word = build_clauses([RawItem(text="# 合同编号 A-001")], "word")

    assert pdf[0].text == "#合同编号 A-001"
    assert word[0].text == "# 合同编号 A-001"


def test_contract_section_titles_anchor_numbered_and_unnumbered_versions():
    word = build_clauses([
        RawItem(text="质量要求:产品应符合国家标准。"),
        RawItem(text="违约责任:逾期按0.3%/天支付违约金。"),
    ], "word")
    pdf = build_clauses([
        RawItem(text="2. 质量要求:产品应符合国家标准。"),
        RawItem(text="7. 违约责任:逾期按0.3%/天支付违约金。"),
    ], "pdf")

    assert [clause.field_key for clause in word] == ["section:质量要求", "section:违约责任"]
    assert [clause.field_key for clause in pdf] == ["section:质量要求", "section:违约责任"]


def test_signature_key_value_table_splits_into_field_clauses():
    table = TableStructure(
        headers=["甲方:武汉甲公司", "乙方:武汉乙公司"],
        rows=[
            ["帐号:039401040006087", "帐号:416180100100240923"],
            ["签约代表:王斌", "签约代表:邹加盛"],
            ["时间:2026-07-13", "时间:2026-07-13"],
        ],
    )
    clauses = build_clauses([
        RawItem(
            text="\n".join(" | ".join(row) for row in [table.headers, *table.rows]),
            kind="table",
            table=table,
        )
    ], "word")

    assert [clause.field_key for clause in clauses] == [
        "甲方", "乙方", "账号", "账号", "签约代表", "签约代表", "日期", "日期"
    ]
