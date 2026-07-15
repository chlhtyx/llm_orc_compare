"""docx 差异高亮烧录测试。

覆盖：
- modified 条款整段黄底（编号定位）
- deleted 条款整段浅红底
- 多候选编号用正文消歧
- 无编号字段块用正文匹配
- 定位失败时跳过不误标
- run 原格式保留
"""
from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.oxml.ns import qn

from document_comparison.models import (
    Diff,
    DiffSegment,
    KeyElement,
    TamperReport,
)
from document_comparison.report.docx_burn import build_docx_preview, burn_docx


def _shaded_fill(paragraph) -> str | None:
    """返回段落底纹 fill 值，无底纹返回 None。"""
    pPr = paragraph._p.pPr
    if pPr is None:
        return None
    shd = pPr.find(qn("w:shd"))
    return shd.get(qn("w:fill")) if shd is not None else None


def _make_docx(path: Path, paragraphs: list[str], *, multi_run_indices: set[int] = ()) -> None:
    """构造一个含若干段落的临时 docx。multi_run_indices 的段落拆成多 run。"""
    doc = Document()
    for i, text in enumerate(paragraphs):
        p = doc.add_paragraph()
        if i in multi_run_indices and len(text) > 4:
            # 拆成 2 个 run，验证底纹不破坏多 run 格式
            mid = len(text) // 2
            p.add_run(text[:mid])
            p.add_run(text[mid:])
        else:
            p.add_run(text)
    doc.save(str(path))


def _diff(
    *,
    status: str = "modified",
    number: str = "",
    word_text: str = "",
    pdf_text: str = "",
    risk: str = "high",
) -> Diff:
    """构造一条 diff，segments 由 word/pdf 文本拼成 equal+delete / equal+insert。"""
    segs: list[DiffSegment] = []
    if word_text:
        segs.append(DiffSegment(op="delete", text=word_text))
    if pdf_text:
        segs.append(DiffSegment(op="insert", text=pdf_text))
    # 给一段公共 equal 让 segments 非空且可重建 word 文本
    return Diff(
        alignment_id="al0",
        status=status,
        segments=segs,
        risk_level=risk,
        number=number,
    )


def test_modified_clause_gets_yellow_shade_by_number(tmp_path: Path):
    src = tmp_path / "src.docx"
    out = tmp_path / "out.docx"
    _make_docx(src, [
        "第一条 合作内容",
        "1.1 乙方按照本合同约定提供服务。",
        "3.2 支付方式：甲方按以下方式向乙方付款：合同签订后3个工作日内支付。",
        "5.1 乙方完成约定工作后，书面通知甲方验收，甲方需在收到通知后3-7个工作日内完成。",
    ])
    report = TamperReport(
        source="src.docx",
        target="tgt.pdf",
        overall_risk="high",
        diffs=[
            _diff(number="3.2", word_text="支付方式：甲方按以下方式向乙方付款", risk="high"),
            _diff(number="5.1", word_text="乙方完成约定工作后", risk="high"),
        ],
    )

    burn_docx(src, report, out)

    d = Document(str(out))
    # 只有 3.2 / 5.1 被标黄，其他段落无底纹
    assert _shaded_fill(d.paragraphs[2]) == "FFEB3B"  # 3.2
    assert _shaded_fill(d.paragraphs[3]) == "FFEB3B"  # 5.1
    assert _shaded_fill(d.paragraphs[0]) is None       # 第一条
    assert _shaded_fill(d.paragraphs[1]) is None       # 1.1


def test_deleted_clause_gets_red_shade(tmp_path: Path):
    src = tmp_path / "src.docx"
    out = tmp_path / "out.docx"
    _make_docx(src, [
        "4.1 甲方权利义务",
        "7.3 任何一方擅自单方解除合同需支付违约金。",
    ])
    report = TamperReport(
        source="src.docx",
        target="tgt.pdf",
        overall_risk="medium",
        unmatched_clauses=[
            Diff(
                alignment_id="al1",
                status="deleted",
                segments=[DiffSegment(op="delete", text="7.3 任何一方擅自单方解除合同")],
                risk_level="high",
                number="7.3",
            ),
        ],
    )

    burn_docx(src, report, out)

    d = Document(str(out))
    assert _shaded_fill(d.paragraphs[1]) == "FFCDD2"  # 7.3 浅红
    assert _shaded_fill(d.paragraphs[0]) is None


def test_shading_preserves_multi_run_format(tmp_path: Path):
    """多 run 段落加底纹后，原 run 数量和文字不丢。"""
    src = tmp_path / "src.docx"
    out = tmp_path / "out.docx"
    full = "3.2 支付方式：甲方按以下方式向乙方付款：合同签订后3个工作日内支付。"
    _make_docx(src, ["3.2 支付方式：甲方按以下方式向乙方付款"], multi_run_indices={0})
    report = TamperReport(
        source="src.docx",
        target="tgt.pdf",
        diffs=[_diff(number="3.2", word_text="支付方式", risk="high")],
    )

    burn_docx(src, report, out)

    d = Document(str(out))
    p = d.paragraphs[0]
    assert _shaded_fill(p) == "FFEB3B"
    # 多 run 保留
    assert len(p.runs) == 2
    assert p.runs[0].text + p.runs[1].text == "3.2 支付方式：甲方按以下方式向乙方付款"


def test_number_disambiguation_picks_best_match(tmp_path: Path):
    """同编号多候选时用正文相似度消歧，标到内容最像的那段。"""
    src = tmp_path / "src.docx"
    out = tmp_path / "out.docx"
    _make_docx(src, [
        "（1）有权对乙方的服务进度进行监督。",
        "（1）按照本合同约定按时足额支付合作费用。",
    ])
    # 两条都以（1）开头，用正文消歧应命中第二条
    report = TamperReport(
        source="src.docx",
        target="tgt.pdf",
        diffs=[_diff(number="1", word_text="按照本合同约定按时足额支付合作费用", risk="medium")],
    )

    burn_docx(src, report, out)

    d = Document(str(out))
    assert _shaded_fill(d.paragraphs[0]) is None
    assert _shaded_fill(d.paragraphs[1]) == "FFE082"  # medium 暖黄


def test_locate_failure_skips_without_mislabeling(tmp_path: Path):
    """diff 的 number/正文在 docx 里找不到时，跳过且不误标其他段落。"""
    src = tmp_path / "src.docx"
    out = tmp_path / "out.docx"
    _make_docx(src, [
        "第一条 合作内容",
        "1.1 乙方提供服务。",
    ])
    report = TamperReport(
        source="src.docx",
        target="tgt.pdf",
        diffs=[_diff(number="9.9", word_text="完全不相关的随机文字内容xyz", risk="high")],
    )

    burn_docx(src, report, out)

    d = Document(str(out))
    # 没有段落被标（定位失败，正文相似度 < 0.5）
    for p in d.paragraphs:
        assert _shaded_fill(p) is None


def test_full_width_punctuation_tolerated(tmp_path: Path):
    """docx 用全角：（），diff 文本用半角 :()，归一化后仍能匹配。"""
    src = tmp_path / "src.docx"
    out = tmp_path / "out.docx"
    _make_docx(src, [
        "3.2 支付方式：甲方付款。",
    ])
    # word_text 用半角标点（模拟 normalize_text 后的报告文本）
    report = TamperReport(
        source="src.docx",
        target="tgt.pdf",
        diffs=[_diff(number="3.2", word_text="支付方式:甲方付款", risk="high")],
    )

    burn_docx(src, report, out)

    d = Document(str(out))
    assert _shaded_fill(d.paragraphs[0]) == "FFEB3B"


def test_build_docx_preview_returns_all_paragraphs_with_marks(tmp_path: Path):
    """build_docx_preview 返回全段落列表，命中的段落带 highlight 标记。"""
    src = tmp_path / "src.docx"
    _make_docx(src, [
        "通用样板合同",
        "1.1 乙方提供服务。",
        "3.2 支付方式：甲方按以下方式向乙方付款：合同签订后3个工作日内支付。",
        "5.1 乙方完成约定工作后，书面通知甲方验收。",
    ])
    report = TamperReport(
        source="src.docx",
        target="tgt.pdf",
        overall_risk="high",
        diffs=[
            _diff(number="3.2", word_text="支付方式：甲方按以下方式向乙方付款", risk="high"),
            _diff(number="5.1", word_text="乙方完成约定工作后", risk="medium"),
        ],
    )

    preview = build_docx_preview(src, report)

    # 全 4 段都返回（含一致的）
    assert len(preview) == 4
    # 标题段无标记
    assert preview[0] == {
        "text": "通用样板合同", "highlight": "", "status": "identical",
        "risk_level": "none", "number": "",
    }
    # 1.1 无标记
    assert preview[1]["highlight"] == ""
    # 3.2 modified / high
    assert preview[2]["highlight"] == "modified"
    assert preview[2]["risk_level"] == "high"
    assert preview[2]["number"] == "3.2"
    # 5.1 modified / medium
    assert preview[3]["highlight"] == "modified"
    assert preview[3]["risk_level"] == "medium"


def test_build_docx_preview_marks_deleted(tmp_path: Path):
    """deleted 条款在预览里标 deleted。"""
    src = tmp_path / "src.docx"
    _make_docx(src, [
        "1.1 保留条款。",
        "7.3 已被删除的条款。",
    ])
    report = TamperReport(
        source="src.docx",
        target="tgt.pdf",
        unmatched_clauses=[
            Diff(
                alignment_id="al1",
                status="deleted",
                segments=[DiffSegment(op="delete", text="7.3 已被删除的条款")],
                risk_level="high",
                number="7.3",
            ),
        ],
    )

    preview = build_docx_preview(src, report)
    assert preview[0]["highlight"] == ""          # 1.1
    assert preview[1]["highlight"] == "deleted"   # 7.3
    assert preview[1]["status"] == "deleted"

