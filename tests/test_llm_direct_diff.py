"""标准合同比对的 LLM 直接比对(enable_llm_direct_diff)分支测试。

开启该开关后 run_pipeline 跳过条款切分/对齐/裁决,把 Word 与 PDF 解析成纯文本后
直接交给 LLM 比对差异并标注(复用无标注版管线 + llm_text_diff),结果适配为标准
TamperReport,使任务/历史/外部 API/报告页链路无感复用。

这里 mock 掉底层 LLM 调用(`llm_text_diff`),保证测试稳定可复现、不依赖外部服务。
"""
import io
import json
from pathlib import Path

import httpx
import pytest

from docx import Document  # type: ignore[import-untyped]

try:
    import pymupdf as fitz
except ImportError:
    import fitz  # type: ignore

from document_comparison.models import (
    Block,
    DiffSegment,
    PageMeta,
    PageRecognitionDiagnostic,
    TextDiffHunk,
    TextDiffReport,
    TamperReport,
)
from document_comparison.report.builder import locate_hunk_regions
from document_comparison.pipeline import run_pipeline
from document_comparison.compare.llm_diff import text_diff_to_tamper_report

import document_comparison.ocr.whole_doc as whole_doc_mod
import document_comparison.compare.llm_diff as llm_diff_mod
import document_comparison.compare.judge as judge_mod
from document_comparison.ocr.native import NativePDFEngine


def _make_word(parts) -> io.BytesIO:
    doc = Document()
    for kind, text in parts:
        if kind == "p":
            doc.add_paragraph(text)
        elif kind == "h1":
            doc.add_heading(text, level=1)
    buf = io.BytesIO()
    doc.save(buf)
    return buf


def _make_pdf_from_lines(lines) -> io.BytesIO:
    pdf = fitz.open()
    page = pdf.new_page(width=595, height=842)
    y = 72
    for ln in lines:
        page.insert_text((72, y), ln, fontname="china-s", fontsize=11)
        y += 20
    buf = io.BytesIO()
    pdf.save(buf)
    pdf.close()
    buf.seek(0)
    return buf


def _to_files(word_buf, pdf_buf, tmp_path):
    wpath = tmp_path / "c.docx"
    ppath = tmp_path / "c.pdf"
    wpath.write_bytes(word_buf.getvalue())
    ppath.write_bytes(pdf_buf.getvalue())
    word_buf.seek(0)
    return wpath, ppath


def _mock_llm_report(hunks, similarity=1.0):
    return TextDiffReport(
        source="",
        target="",
        word_text="",
        pdf_text="",
        hunks=hunks,
        stats={
            "similarity": similarity,
            "equal_lines": 0,
            "replaced": sum(
                max(len(h.word_lines), len(h.pdf_lines))
                for h in hunks
                if h.tag == "replace"
            ),
            "deleted": sum(len(h.word_lines) for h in hunks if h.tag == "delete"),
            "inserted": sum(len(h.pdf_lines) for h in hunks if h.tag == "insert"),
            "engine": "llm",
        },
    )


# —— 适配函数 text_diff_to_tamper_report:结构映射正确性 ——


def test_adapter_maps_each_hunk_tag_to_diff_status():
    """replace→modified、delete→deleted、insert→added;无差异时 change_status=clean。"""
    raw = _mock_llm_report(
        [
            TextDiffHunk(tag="replace", word_lines=["甲方"], pdf_lines=["乙方"]),
            TextDiffHunk(tag="delete", word_lines=["被删除的条款"]),
            TextDiffHunk(tag="insert", pdf_lines=["新增的条款"]),
        ],
        similarity=0.8,
    )
    report = text_diff_to_tamper_report(raw, source="a.docx", target="b.pdf")

    assert isinstance(report, TamperReport)
    assert [d.status for d in report.diffs] == ["modified", "deleted", "added"]
    assert report.change_status == "changed"
    assert report.overall_risk == "changed"
    assert report.summary["llm_direct_diff"] is True
    # per-diff 不做风险分级
    assert all(d.risk_level == "none" for d in report.diffs)
    # 无 page_regions(LLM 直接比对没有 PDF 坐标)
    assert all(d.page_regions == [] for d in report.diffs)
    # replace 无 char_segments 时退化为 delete+insert 片段
    segs = report.diffs[0].segments
    assert [s.op for s in segs] == ["delete", "insert"]
    assert segs[0].text == "甲方" and segs[1].text == "乙方"


def test_adapter_replace_uses_char_segments_when_present():
    """replace 且 LLM 给出 char_segments 时直接复用(行内字符级标注)。"""
    raw = _mock_llm_report(
        [
            TextDiffHunk(
                tag="replace",
                word_lines=["定金三万元"],
                pdf_lines=["定金五万元"],
                char_segments=[
                    DiffSegment(op="equal", text="定金"),
                    DiffSegment(op="delete", text="三"),
                    DiffSegment(op="insert", text="五"),
                    DiffSegment(op="equal", text="万元"),
                ],
            ),
        ]
    )
    report = text_diff_to_tamper_report(raw, source="a.docx", target="b.pdf")
    assert [s.op for s in report.diffs[0].segments] == [
        "equal",
        "delete",
        "insert",
        "equal",
    ]


def test_adapter_empty_hunks_is_clean():
    raw = _mock_llm_report([], similarity=1.0)
    report = text_diff_to_tamper_report(raw, source="a.docx", target="b.pdf")
    assert report.diffs == []
    assert report.change_status == "clean"
    assert report.overall_risk == "clean"


def test_adapter_context_before_becomes_title():
    raw = _mock_llm_report(
        [TextDiffHunk(tag="replace", word_lines=["x"], pdf_lines=["y"], context_before=["第三条 付款"])]
    )
    report = text_diff_to_tamper_report(raw, source="a.docx", target="b.pdf")
    assert report.diffs[0].title == "第三条 付款"


# —— pipeline LLM 直接比对分支:端到端(真实 Word/PDF 解析,mock LLM)——


def _setup_judge(monkeypatch):
    """LLM 直接比对复用 judge_* 配置;测试需提供非空配置,否则 llm_text_diff 会 raise。"""
    from document_comparison.config import settings

    monkeypatch.setattr(settings, "judge_api_base", "https://example.com/v1")
    monkeypatch.setattr(settings, "judge_api_key", "test-key")
    monkeypatch.setattr(settings, "judge_model", "test-model")


def test_llm_text_diff_uses_custom_prompt_when_configured(monkeypatch):
    """settings.llm_direct_diff_prompt 非空时,作为系统提示词发给 LLM;空则回退内置默认。"""
    from document_comparison.config import settings

    _setup_judge(monkeypatch)

    captured: dict = {}

    def fake_post(system_prompt, user_content):
        captured["system_prompt"] = system_prompt
        captured["user_content"] = user_content
        # 返回一个合法的空差异 JSON
        return '{"hunks": [], "similarity": 1.0}'

    monkeypatch.setattr(llm_diff_mod, "_post_judge_chat", fake_post)

    # ① 自定义提示词:作为系统提示词前缀,且 char_level=True 时拼接字符级补充指令
    custom = "【自定义比对规则】只关注金额变化。"
    monkeypatch.setattr(settings, "llm_direct_diff_prompt", custom)
    llm_diff_mod.llm_text_diff("原文A", "待核A", char_level=True)
    assert captured["system_prompt"].startswith(custom)
    assert llm_diff_mod._CHAR_DIFF_INSTRUCTION in captured["system_prompt"]
    # 末尾统一追加 /no_think(GLM-4.5/4.6 关闭思考链指令)
    assert captured["system_prompt"].endswith(llm_diff_mod._NO_THINK_SUFFIX)
    assert "原文A" in captured["user_content"] and "待核A" in captured["user_content"]

    # ② 留空:回退内置默认提示词(同样以 /no_think 结尾)
    monkeypatch.setattr(settings, "llm_direct_diff_prompt", "")
    llm_diff_mod.llm_text_diff("原文B", "待核B", char_level=False)
    assert (
        captured["system_prompt"]
        == llm_diff_mod._DIFF_SYSTEM_PROMPT + llm_diff_mod._NO_THINK_SUFFIX
    )
    assert llm_diff_mod._CHAR_DIFF_INSTRUCTION not in captured["system_prompt"]


def test_judge_and_llm_diff_system_prompts_carry_no_think(monkeypatch):
    """两条比对通道(judge 风险复核 + llm-diff 直接比对)的出站 system 消息必须以
    /no_think 结尾(GLM-4.5/4.6 关闭 <think> 思考链指令),且与
    chat_template_kwargs.enable_thinking=False(Qwen3 侧等价控制)并存于同一 payload。

    在 HTTP 层 mock httpx.Client,验证真正发出的 payload 而非 mock 出的入参。
    """
    from document_comparison.config import settings

    _setup_judge(monkeypatch)
    monkeypatch.setattr(settings, "llm_max_retries", 0)

    captured: dict = {}

    def _respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.setdefault("system_contents", []).append(
            body["messages"][0]["content"]
        )
        # enable_thinking 嵌在 chat_template_kwargs 里(vLLM 应用到 chat template 的
        # 正确位置),不在顶层。
        captured.setdefault("enable_thinking", []).append(
            body.get("chat_template_kwargs", {}).get("enable_thinking")
        )
        # judge 期望 JSON;llm-diff 走纯 prompt 约束,也回 JSON 兼容两条路径
        content = '{"risk_level": "low", "reason": "措辞差异"}'
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": content}}]},
        )

    original_client = httpx.Client
    for mod_path in (
        "document_comparison.compare.llm_diff.httpx.Client",
        "document_comparison.compare.judge.httpx.Client",
    ):
        monkeypatch.setattr(
            mod_path,
            lambda **kwargs: original_client(
                transport=httpx.MockTransport(_respond), **kwargs
            ),
        )

    # ① llm-diff 直接比对
    llm_diff_mod.llm_text_diff("原文", "待核")

    # ② judge 风险复核
    judge_mod.llm_judge_diff(
        word_text="金额100万元",
        pdf_text="金额200万元",
        segments=[
            judge_mod.DiffSegment(op="delete", text="100"),
            judge_mod.DiffSegment(op="insert", text="200"),
        ],
        rule_risk="high",
    )

    assert len(captured["system_contents"]) == 2
    for sys_content in captured["system_contents"]:
        assert sys_content.endswith("/no_think")
    # /no_think 是运行时追加的指令,内置默认规则常量本身不混入该后缀
    assert not llm_diff_mod._DIFF_SYSTEM_PROMPT.endswith("/no_think")
    assert not judge_mod._JUDGE_SYSTEM_PROMPT.endswith("/no_think")
    # 两条通道同时保留 chat_template_kwargs.enable_thinking=False
    # (Qwen3 侧的等价控制,嵌进 chat_template_kwargs 才会被 vLLM 应用),与 /no_think 并存
    assert captured["enable_thinking"] == [False, False]


def test_judge_and_llm_diff_omit_no_think_when_disabled(monkeypatch):
    """关闭 llm_diff_no_think_enabled 开关后,两条比对通道的系统提示词都不再追加 /no_think。

    验证开关真正作用于运行时拼装的 system 消息,而非常量。
    """
    from document_comparison.config import settings

    _setup_judge(monkeypatch)
    monkeypatch.setattr(settings, "llm_max_retries", 0)
    monkeypatch.setattr(settings, "llm_diff_no_think_enabled", False)

    captured: dict = {}

    def _respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.setdefault("system_contents", []).append(body["messages"][0]["content"])
        content = '{"risk_level": "low", "reason": "措辞差异"}'
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": content}}]},
        )

    original_client = httpx.Client
    for mod_path in (
        "document_comparison.compare.llm_diff.httpx.Client",
        "document_comparison.compare.judge.httpx.Client",
    ):
        monkeypatch.setattr(
            mod_path,
            lambda **kwargs: original_client(
                transport=httpx.MockTransport(_respond), **kwargs
            ),
        )

    llm_diff_mod.llm_text_diff("原文", "待核")
    judge_mod.llm_judge_diff(
        word_text="金额100万元",
        pdf_text="金额200万元",
        segments=[judge_mod.DiffSegment(op="delete", text="100")],
        rule_risk="high",
    )

    assert len(captured["system_contents"]) == 2
    for sys_content in captured["system_contents"]:
        assert "/no_think" not in sys_content


def test_pipeline_llm_direct_diff_branch_produces_tamper_report(tmp_path, monkeypatch):
    """开启 enable_llm_direct_diff 后,run_pipeline 走 LLM 直接比对并返回 TamperReport。

    注入原生 PDF 引擎(其 blocks 带 bbox),验证文本与坐标同源链路:LLM 摘出的片段
    能在 block content 里命中并挂上真实 page_region。
    """
    _setup_judge(monkeypatch)
    # 测试 PDF 文本量较小,降低原生文字层阈值使该页被判 reliable。
    import document_comparison.ocr.native as native_mod
    monkeypatch.setattr(native_mod, "_MIN_NATIVE_CHARS", 1)

    word_buf = _make_word([("h1", "合同"), ("p", "甲方应当支付定金三万元。")])
    pdf_buf = _make_pdf_from_lines(["甲方应当支付定金五万元。"])
    wpath, ppath = _to_files(word_buf, pdf_buf, tmp_path)

    def fake_llm(word_text, pdf_text, **kw):
        # pdf_lines 必须是实际 PDF block content 的子串,定位才能命中
        return _mock_llm_report(
            [
                TextDiffHunk(
                    tag="replace",
                    word_lines=["定金三万元"],
                    pdf_lines=["定金五万元"],
                    context_before=["定金"],
                )
            ],
            similarity=0.9,
        )

    monkeypatch.setattr(llm_diff_mod, "llm_text_diff", fake_llm)

    report = run_pipeline(
        wpath, ppath, ocr=NativePDFEngine(), enable_llm_direct_diff=True
    )

    assert isinstance(report, TamperReport)
    assert report.summary.get("llm_direct_diff") is True
    assert len(report.diffs) == 1
    assert report.diffs[0].status == "modified"
    assert report.diffs[0].judged_by == "llm"
    assert report.change_status == "changed"
    # 同源链路:replace hunk 应挂上真实高亮坐标
    assert len(report.diffs[0].page_regions) >= 1
    assert report.diffs[0].page_regions[0].kind == "real"


def test_pipeline_llm_direct_diff_silently_ignores_alignment_and_judge(
    tmp_path, monkeypatch, caplog,
):
    """LLM 直接比对与标准管线(对齐/辅助说明)互斥。

    同时传 direct_diff=True + alignment=True + judge=True 时,pipeline 入口应静默
    归一 alignment/judge=False 并记 warning,然后照常走直接比对分支返回 TamperReport
    (不会因为 alignment/judge 同时为真而进入标准管线或报错)。
    """
    import logging

    _setup_judge(monkeypatch)
    import document_comparison.ocr.native as native_mod
    monkeypatch.setattr(native_mod, "_MIN_NATIVE_CHARS", 1)

    word_buf = _make_word([("h1", "合同"), ("p", "甲方应当支付定金三万元。")])
    pdf_buf = _make_pdf_from_lines(["甲方应当支付定金五万元。"])
    wpath, ppath = _to_files(word_buf, pdf_buf, tmp_path)

    monkeypatch.setattr(
        llm_diff_mod,
        "llm_text_diff",
        lambda wt, pt, **kw: _mock_llm_report(
            [TextDiffHunk(tag="replace", word_lines=["三万"], pdf_lines=["五万"])],
            similarity=0.9,
        ),
    )

    with caplog.at_level(logging.WARNING, logger="document_comparison.pipeline"):
        report = run_pipeline(
            wpath, ppath,
            ocr=NativePDFEngine(),
            enable_llm_direct_diff=True,
            enable_llm_alignment=True,
            enable_llm_judge=True,
        )

    # 仍走直接比对分支
    assert isinstance(report, TamperReport)
    assert report.summary.get("llm_direct_diff") is True
    # 归一被记录
    assert any(
        "enable_llm_direct_diff=True 隐式忽略" in rec.message
        for rec in caplog.records
    ), [r.message for r in caplog.records]


def test_pipeline_llm_direct_diff_unreliable_ocr_marks_needs_review(tmp_path, monkeypatch):
    """适配后经 apply_recognition_gate:不可靠读取应把结论降级为 needs_review。

    这是 pipeline LLM 直接比对分支的收尾逻辑(adapter 之后立即调用 gate)。
    LLM 直接比对的差异没有 page_regions,坏页时按「无坐标无法证明可靠」处理。
    """
    from document_comparison.ocr.quality import apply_recognition_gate

    raw = _mock_llm_report(
        [TextDiffHunk(tag="replace", word_lines=["X"], pdf_lines=["Y"])],
        similarity=0.5,
    )
    diag = PageRecognitionDiagnostic(
        page_index=0, source="fallback", reliable=False, reasons=["ocr low quality"]
    )
    report = text_diff_to_tamper_report(raw, source="a.docx", target="b.pdf")
    apply_recognition_gate(report, [diag], enable_risk_assessment=False)

    assert report.recognition_status == "needs_review"
    # 无 page_regions 的差异在坏页下被复核
    assert report.change_status == "needs_review"
    assert report.overall_risk == "needs_review"
    assert report.diffs[0].verdict == "needs_review"
    assert report.diffs[0].confidence == "low"


# —— hunk 坐标定位:locate_hunk_regions ——


def _meta(page_index=0):
    return PageMeta(
        page_index=page_index,
        width_px=1654,
        height_px=2339,
        pdf_width_pt=595,
        pdf_height_pt=842,
    )


def test_locate_replace_hunk_matches_block_bbox():
    """replace/insert 的 pdf_lines 命中 PDF block 时,产出归一化的 real PageRegion。"""
    pmeta = {0: _meta()}
    blocks = [[Block(
        block_id="b1", page_index=0, label="text",
        bbox=[100, 100, 300, 140],
        content="甲方应当支付定金五万元整",
    )]]
    hunks = [TextDiffHunk(tag="replace", word_lines=["定金三万元"], pdf_lines=["定金五万元"])]

    regions = locate_hunk_regions(hunks, blocks, pmeta)

    assert len(regions) == 1
    r = regions[0][0]
    assert r.page_index == 0
    assert r.kind == "real"
    # 归一化到 [0,1]:x≈100/595..300/595, y≈100/842..140/842
    assert 0.1 < r.bbox[0] < 0.6 and 0.05 < r.bbox[1] < 0.25


def test_locate_insert_hunk_uses_pdf_lines():
    """insert 同样用 pdf_lines 定位(PDF 侧新增内容)。"""
    pmeta = {0: _meta()}
    blocks = [[Block(
        block_id="b1", page_index=0, label="text",
        bbox=[50, 200, 250, 230],
        content="本条为新增条款内容",
    )]]
    hunks = [TextDiffHunk(tag="insert", pdf_lines=["新增条款"])]

    regions = locate_hunk_regions(hunks, blocks, pmeta)
    assert len(regions[0]) == 1
    assert regions[0][0].kind == "real"


def test_locate_delete_hunk_gets_placeholder_near_previous_anchor():
    """delete 在回收件无内容:用上一个已定位 hunk 同页下方生成 placeholder 占位框。"""
    pmeta = {0: _meta()}
    blocks = [[Block(
        block_id="b1", page_index=0, label="text",
        bbox=[100, 100, 300, 140],
        content="存在的条款内容",
    )]]
    hunks = [
        TextDiffHunk(tag="replace", word_lines=["旧"], pdf_lines=["存在的条款内容"]),
        TextDiffHunk(tag="delete", word_lines=["被删除的旧条款"]),
    ]

    regions = locate_hunk_regions(hunks, blocks, pmeta)

    assert len(regions[0]) == 1 and regions[0][0].kind == "real"
    # delete 有占位框,且是 placeholder,在 replace 锚点下方
    assert len(regions[1]) == 1
    ph = regions[1][0]
    assert ph.kind == "placeholder"
    assert ph.bbox[1] > regions[0][0].bbox[3]  # top 在锚点 bottom 之下


def test_locate_delete_without_any_anchor_is_empty():
    """无任何已定位锚点时,delete 不生成占位框(优雅降级)。"""
    pmeta = {0: _meta()}
    blocks = [[]]  # 没有任何带坐标的 block
    hunks = [TextDiffHunk(tag="delete", word_lines=["孤立删除"])]

    regions = locate_hunk_regions(hunks, blocks, pmeta)
    assert regions == [[]]


def test_locate_match_failure_leaves_empty_regions():
    """pdf_lines 在 blocks 里匹配不到时,该 hunk 无坐标(不报错)。"""
    pmeta = {0: _meta()}
    blocks = [[Block(
        block_id="b1", page_index=0, label="text",
        bbox=[100, 100, 300, 140],
        content="完全无关的内容",
    )]]
    hunks = [TextDiffHunk(tag="replace", word_lines=["X"], pdf_lines=["找不到的片段"])]

    regions = locate_hunk_regions(hunks, blocks, pmeta)
    assert regions == [[]]


def test_adapter_attaches_located_regions_to_diffs():
    """text_diff_to_tamper_report 把 page_regions 按 hunk 顺序挂到对应 Diff。"""
    from document_comparison.compare.llm_diff import text_diff_to_tamper_report

    pmeta = {0: _meta()}
    blocks = [[Block(
        block_id="b1", page_index=0, label="text",
        bbox=[100, 100, 300, 140],
        content="定金五万元",
    )]]
    hunks = [
        TextDiffHunk(tag="replace", word_lines=["定金三万元"], pdf_lines=["定金五万元"]),
        TextDiffHunk(tag="delete", word_lines=["被删除条款"]),
    ]
    regions = locate_hunk_regions(hunks, blocks, pmeta)

    raw = TextDiffReport(
        source="a.docx", target="b.pdf",
        hunks=hunks,
        stats={"similarity": 0.9, "engine": "llm"},
    )
    report = text_diff_to_tamper_report(raw, page_regions=regions)

    assert len(report.diffs[0].page_regions) == 1
    assert report.diffs[0].page_regions[0].kind == "real"
    # delete 占位框
    assert len(report.diffs[1].page_regions) == 1
    assert report.diffs[1].page_regions[0].kind == "placeholder"


def test_adapter_without_page_regions_stays_empty():
    """不传 page_regions 时,diff.page_regions 为空(向后兼容)。"""
    from document_comparison.compare.llm_diff import text_diff_to_tamper_report

    raw = TextDiffReport(
        source="a.docx", target="b.pdf",
        hunks=[TextDiffHunk(tag="replace", word_lines=["x"], pdf_lines=["y"])],
        stats={"similarity": 0.9, "engine": "llm"},
    )
    report = text_diff_to_tamper_report(raw)
    assert report.diffs[0].page_regions == []



# —— burn_pdf 高亮端到端(回归:此前因缺 page_meta 导致全部高亮被跳过)——


def test_burn_pdf_renders_highlight_with_page_meta(tmp_path):
    """page_regions + page_meta 齐备时,burn_pdf 真正在 PDF 上画出高亮矩形。

    此前 text_diff_to_tamper_report 未设置 page_meta,导致 burn_pdf 内
    `pmeta = {m.page_index: m for m in report.page_meta}` 为空,
    每页 meta=None 直接 continue,高亮全部丢失。本测试钉住该回归。
    """
    from document_comparison.compare.llm_diff import text_diff_to_tamper_report
    from document_comparison.report.builder import locate_hunk_regions, burn_pdf

    # 构造带文字层的原生 PDF(出带 bbox 的 block)
    pdf_buf = _make_pdf_from_lines(["甲方应当支付定金五万元。"])
    pdf_path = tmp_path / "src.pdf"
    pdf_path.write_bytes(pdf_buf.getvalue())

    from document_comparison.ocr.native import read_native_page
    import pymupdf as _fitz

    doc = _fitz.open(str(pdf_path))
    nr = read_native_page(doc[0], 0)
    doc.close()
    assert any(len(b.bbox) >= 4 for b in nr.blocks), "测试 PDF 未产出带 bbox 的 block"

    pmeta = {0: _meta()}
    regions = locate_hunk_regions(
        [TextDiffHunk(tag="replace", word_lines=["定金三万元"], pdf_lines=["定金五万元"])],
        [nr.blocks], pmeta,
    )
    assert regions[0], "定位未命中(前置失败,无法验证 burn)"

    raw = TextDiffReport(
        source="a.docx", target=str(pdf_path),
        hunks=[TextDiffHunk(tag="replace", word_lines=["定金三万元"], pdf_lines=["定金五万元"])],
        stats={"similarity": 0.9, "engine": "llm"},
    )
    report = text_diff_to_tamper_report(raw, page_regions=regions, page_metas=[pmeta[0]])

    # 钉回归点:page_meta 必须非空,否则 burn_pdf 全部跳过
    assert len(report.page_meta) == 1

    out_path = tmp_path / "burned.pdf"
    burn_pdf(pdf_path, report, out_path)

    burned = _fitz.open(str(out_path))
    annots = list(burned[0].annots() or [])
    burned.close()
    assert len(annots) >= 1, "burn_pdf 未画出高亮矩形(page_meta 缺失的回归)"


def test_burn_pdf_skips_when_page_meta_missing(tmp_path):
    """page_meta 为空时 burn_pdf 不画高亮(文档化降级行为,佐证为何必须补 page_meta)。"""
    from document_comparison.compare.llm_diff import text_diff_to_tamper_report
    from document_comparison.report.builder import burn_pdf

    pdf_buf = _make_pdf_from_lines(["任意内容"])
    pdf_path = tmp_path / "src.pdf"
    pdf_path.write_bytes(pdf_buf.getvalue())

    raw = TextDiffReport(
        source="a.docx", target=str(pdf_path),
        hunks=[TextDiffHunk(tag="replace", word_lines=["x"], pdf_lines=["y"])],
        stats={"similarity": 0.9, "engine": "llm"},
    )
    # 不传 page_metas → report.page_meta 为空
    report = text_diff_to_tamper_report(raw)
    assert report.page_meta == []

    out_path = tmp_path / "burned.pdf"
    burn_pdf(pdf_path, report, out_path)

    import pymupdf as _fitz
    burned = _fitz.open(str(out_path))
    annots = list(burned[0].annots() or [])
    burned.close()
    assert annots == [], "page_meta 缺失时不应画出高亮"


# —— SDK 路径高亮修复:NFKC 对齐 + 跨块回退 ——


def test_locate_nfkc_full_width_match():
    """SDK 输出全角字符时,定位仍能命中(NFKC 在匹配两侧同步生效)。

    此前 _block_norm_text 不做 NFKC,而 pipeline 喂给 LLM 的 pdf_text 经
    normalize_text 做了 NFKC,导致全角字母/数字对不上、高亮落空。
    """
    pmeta = {0: _meta()}
    # block content 含全角字母(模拟 SDK 原样输出),needle 为半角(LLM NFKC 后)
    blocks = [[Block(
        block_id="b1", page_index=0, label="text",
        bbox=[100, 100, 300, 140],
        content="甲方编号Ａ１２３",
    )]]
    hunks = [TextDiffHunk(tag="replace", word_lines=["x"], pdf_lines=["A123"])]

    regions = locate_hunk_regions(hunks, blocks, pmeta)
    assert len(regions[0]) == 1, "全角字符应经 NFKC 对齐后命中"


def test_locate_cross_block_fragment_fallback():
    """LLM 片段跨多个细粒度 block 时,按页联合文本回退命中该页所有带 bbox block。

    SDK markdown 模式每行一个 block,LLM 摘出的连续片段逐块匹配会落空。
    """
    pmeta = {0: _meta()}
    blocks = [[
        Block(block_id="b1", page_index=0, label="text", bbox=[100, 100, 300, 120], content="甲方应当"),
        Block(block_id="b2", page_index=0, label="text", bbox=[100, 125, 300, 145], content="支付定金"),
        Block(block_id="b3", page_index=0, label="text", bbox=[100, 150, 300, 170], content="五万元整"),
    ]]
    # LLM 摘出的片段跨了三个 block,任何单块都不是其子串
    hunks = [TextDiffHunk(tag="replace", word_lines=["x"], pdf_lines=["甲方应当支付定金五万元整"])]

    regions = locate_hunk_regions(hunks, blocks, pmeta)
    # 跨块回退应命中该页所有带 bbox 的 block(3 个)
    assert len(regions[0]) == 3


def test_locate_cross_block_fragment_excludes_unrelated_page_blocks():
    """跨块命中只能返回覆盖 needle 的最小连续窗口，不能高亮整页。

    DOCX 经 LibreOffice 渲染后通常每个段落/物理行各自成为一个 block。此前
    跨块回退只要在整页拼接文本里找到 needle，就返回该页全部带 bbox 的 block，
    导致一个跨行差异把同页其余无差异条款全部高亮。
    """
    pmeta = {0: _meta()}
    blocks = [[
        Block(block_id="before-1", page_index=0, label="text", bbox=[100, 50, 300, 70], content="第三条 费用及支付方式"),
        Block(block_id="before-2", page_index=0, label="text", bbox=[100, 75, 300, 95], content="3.2 支付方式无变化"),
        Block(block_id="match-1", page_index=0, label="text", bbox=[100, 100, 300, 120], content="甲方需在收到通知后3-7个"),
        Block(block_id="match-2", page_index=0, label="text", bbox=[100, 125, 300, 145], content="工作日内完成验收工作"),
        Block(block_id="after", page_index=0, label="text", bbox=[100, 150, 300, 170], content="第六条 违约责任"),
    ]]
    hunks = [TextDiffHunk(
        tag="replace",
        word_lines=["x"],
        pdf_lines=["甲方需在收到通知后3-7个工作日内完成验收工作"],
    )]

    regions = locate_hunk_regions(hunks, blocks, pmeta)

    assert len(regions[0]) == 2
    assert [region.bbox[1] for region in regions[0]] == pytest.approx([
        100 / pmeta[0].pdf_height_pt,
        125 / pmeta[0].pdf_height_pt,
    ])


def test_locate_markdown_blocks_without_bbox_fall_back_to_page():
    """SDK markdown fallback 产出的 block 无 bbox 时,若同页有带 bbox 的 block 仍可定位。

    纯 markdown block(bbox=[])会被 _match_blocks 跳过;但若同页混合了带坐标的
    block,跨块回退仍能利用后者画出高亮。
    """
    pmeta = {0: _meta()}
    blocks = [[
        # 相邻 block 内容不重叠(真实 SDK 输出如此)
        Block(block_id="no-bbox", page_index=0, label="text", bbox=[], content="甲方应当支付"),
        Block(block_id="has-bbox", page_index=0, label="text", bbox=[100, 100, 300, 140], content="定金五万元"),
    ]]
    # needle 跨了 no-bbox 和 has-bbox 两个块的内容
    hunks = [TextDiffHunk(tag="replace", word_lines=["x"], pdf_lines=["甲方应当支付定金五万元"])]

    regions = locate_hunk_regions(hunks, blocks, pmeta)
    # 联合文本命中,返回该页带 bbox 的 block(has-bbox)
    assert len(regions[0]) == 1
    assert regions[0][0].kind == "real"


def test_locate_all_blocks_without_bbox_yields_empty():
    """整页所有 block 都无 bbox(SDK markdown 全无坐标)时,优雅降级为无高亮。"""
    pmeta = {0: _meta()}
    blocks = [[
        Block(block_id="b1", page_index=0, label="text", bbox=[], content="甲方应当支付定金五万元"),
    ]]
    hunks = [TextDiffHunk(tag="replace", word_lines=["x"], pdf_lines=["定金五万元"])]

    regions = locate_hunk_regions(hunks, blocks, pmeta)
    assert regions == [[]]


# —— LLM 比对 JSON 解析容错与降级(_llm_json 共享 helper + llm_text_diff 降级)——


def test_llm_text_diff_accepts_clean_object(monkeypatch):
    """回归:标准 JSON 对象仍正常解析出 hunks,不触发降级。"""
    _setup_judge(monkeypatch)
    good = (
        '{"hunks": [{"tag": "replace", "word_lines": ["100"], "pdf_lines": ["200"], '
        '"context_before": ["金额"], "context_after": []}], "similarity": 0.5}'
    )
    monkeypatch.setattr(llm_diff_mod, "_post_judge_chat", lambda *a, **k: good)
    report = llm_diff_mod.llm_text_diff("原文", "待核")
    assert report.recognition_status == "reliable"
    assert len(report.hunks) == 1
    assert report.hunks[0].tag == "replace"
    assert report.stats.get("llm_parse_failed") is None  # 未降级


def test_llm_text_diff_accepts_markdown_fenced_json(monkeypatch):
    """LLM 用 ```json 围栏包裹输出时仍能正确解析(共享 helper 去围栏)。"""
    _setup_judge(monkeypatch)
    fenced = (
        "```json\n"
        '{"hunks": [{"tag": "delete", "word_lines": ["违约金条款"], '
        '"pdf_lines": [], "context_before": [], "context_after": []}], '
        '"similarity": 0.9}\n'
        "```"
    )
    monkeypatch.setattr(llm_diff_mod, "_post_judge_chat", lambda *a, **k: fenced)
    report = llm_diff_mod.llm_text_diff("原文", "待核")
    assert report.recognition_status == "reliable"
    assert len(report.hunks) == 1
    assert report.hunks[0].tag == "delete"


def test_llm_text_diff_degrades_on_bare_json_array(monkeypatch):
    """回归 #1:LLM 返回裸数组 [{...}] 时不再 raise,降级为 needs_review。

    历史 bug:旧 `_extract_json` 直接 `json.loads` 返回 list,正则兜底不触发,
    `isinstance(parsed, dict)` 为 False → raise ValueError「LLM 比对返回非 JSON 对象」,
    整个任务标记 failed。修复后降级为空 hunks + needs_review。
    """
    _setup_judge(monkeypatch)
    bare_array = '[{"tag": "replace", "word_lines": ["100"], "pdf_lines": ["200"]}]'
    monkeypatch.setattr(llm_diff_mod, "_post_judge_chat", lambda *a, **k: bare_array)
    report = llm_diff_mod.llm_text_diff("原文", "待核")
    assert report.recognition_status == "needs_review"
    assert report.hunks == []
    assert report.stats.get("llm_parse_failed") is True
    # 降级产物仍是合法 TextDiffReport,可被 text_diff_to_tamper_report 适配
    tamper = llm_diff_mod.text_diff_to_tamper_report(report)
    assert tamper.change_status == "clean"  # 无 hunks → clean,但 recognition 标记保留


def test_llm_text_diff_degrades_on_plain_text(monkeypatch):
    """回归 #2:LLM 返回纯叙述文本(无法解析)时降级为 needs_review,不中断任务。"""
    _setup_judge(monkeypatch)
    prose = "抱歉,这两段文本太长,我无法完成比对。请缩短后重试。"
    monkeypatch.setattr(llm_diff_mod, "_post_judge_chat", lambda *a, **k: prose)
    report = llm_diff_mod.llm_text_diff("原文", "待核")
    assert report.recognition_status == "needs_review"
    assert report.hunks == []
    assert report.stats.get("llm_parse_failed") is True


def test_llm_text_diff_degrades_on_json_with_surrounding_prose(monkeypatch):
    """JSON 前后带解释文本时,共享 helper 正则兜底应能提取出对象(不降级)。

    覆盖 LLM「结果如下:{...}。以上是分析」这种常见输出。
    """
    _setup_judge(monkeypatch)
    mixed = (
        "比对结果如下:\n"
        '{"hunks": [{"tag": "insert", "word_lines": [], "pdf_lines": ["新增条款"], '
        '"context_before": [], "context_after": []}], "similarity": 0.8}\n'
        "以上为分析。"
    )
    monkeypatch.setattr(llm_diff_mod, "_post_judge_chat", lambda *a, **k: mixed)
    report = llm_diff_mod.llm_text_diff("原文", "待核")
    assert report.recognition_status == "reliable"
    assert len(report.hunks) == 1
    assert report.hunks[0].tag == "insert"


def test_extract_llm_json_unit():
    """共享 helper 单元测试:围栏去除 / 裸对象 / 裸数组归一 / 失败返回 {}。"""
    from document_comparison._llm_json import (
        LLMJsonParseError,
        extract_llm_json,
        extract_llm_json_strict,
    )

    # ① 干净对象
    assert extract_llm_json('{"a": 1}') == {"a": 1}
    # ② markdown 围栏(带或不带 json 标记)
    assert extract_llm_json("```json\n{\"a\": 1}\n```") == {"a": 1}
    assert extract_llm_json("```\n{\"a\": 1}\n```") == {"a": 1}
    # ③ 裸数组:expect=dict 归一为 {}(llm_diff/judge 契约);expect=(dict,list) 保留
    assert extract_llm_json("[1, 2, 3]") == {}
    assert extract_llm_json("[1, 2, 3]", expect=(dict, list)) == [1, 2, 3]
    # ④ JSON 前后带叙述 → 正则兜底
    assert extract_llm_json('结果: {"risk_level": "high"} 以上。') == {"risk_level": "high"}
    # ⑤ 纯文本 → {}(优雅),strict 抛 LLMJsonParseError
    assert extract_llm_json("无法解析的纯文本") == {}
    with pytest.raises(LLMJsonParseError):
        extract_llm_json_strict("无法解析的纯文本")
    # ⑥ 顶层非对象/非数组(裸数字)→ expect=dict 归一 {}
    assert extract_llm_json("42") == {}
    # ⑦ 非字符串输入 → {}
    assert extract_llm_json(None) == {}  # type: ignore[arg-type]


def test_judge_uses_shared_helper_on_invalid_json(monkeypatch):
    """judge.py 迁移到共享 helper 后,无效 JSON 仍优雅降级为空 dict → 沿用规则结论。

    通过 mock httpx.Client 让 LLM「返回」纯叙述文本(非 JSON),验证 judge 的降级路径:
    extract_llm_json 返回 {} → risk 为空 → 不在合法级 → 沿用 rule_risk 并附降级说明。
    """
    _setup_judge(monkeypatch)
    from document_comparison.config import settings
    monkeypatch.setattr(settings, "llm_max_retries", 0)

    def _respond(request):
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": "这不是 JSON,只是一句话。"}}]},
        )

    original_client = httpx.Client
    monkeypatch.setattr(
        judge_mod.httpx,
        "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(_respond), **kwargs),
    )

    risk, reasons = judge_mod.llm_judge_diff(
        word_text="金额100万元",
        pdf_text="金额200万元",
        segments=[DiffSegment(op="delete", text="100"), DiffSegment(op="insert", text="200")],
        rule_risk="high",
    )
    # 无效输出应沿用规则结论 high,并给出降级说明
    assert risk == "high"
    assert reasons  # 非空,含「沿用规则」类提示
