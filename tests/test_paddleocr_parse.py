"""PaddleOCR-VL 纯文本/Markdown 输出解析测试。

覆盖 PaddleOCR-VL-1.5 在 SiliconFlow 等平台上的实际输出格式:
- Markdown 表格(``|`` 分隔,含 ``---`` 分隔行)→ label=table + TableStructure
- 普通段落/字段行 → label=text
- 含 ``<|LOC_|>`` 标记的早期格式 → LOC 解析分支
- 配置:paddleocr 后端直接复用 llm_*(无独立 paddleocr_* 字段)
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from document_comparison.models import PageMeta
from document_comparison.ocr import paddleocr_http
from document_comparison.ocr.paddleocr_http import (
    PaddleOCREngine,
    _attach_spotting_bboxes,
    _official_bbox_to_pt,
    _parse_official_page,
    _parse_loc_content,
    _parse_plain_content,
    _plain_text_to_table,
)
from document_comparison.report.builder import _normalize_regions


def _meta() -> PageMeta:
    return PageMeta(
        page_index=0,
        width_px=1654, height_px=2339,
        pdf_width_pt=595, pdf_height_pt=842,
    )


# 来自 PaddleOCR-VL-1.5 实测输出(简短「OCR」指令,已截取表格区)
_PAGE0_PLAIN = """采购合同(简易版)

申购单编号：QCSC2607080010 合同编号：SSC2607130114

甲方（买受人）：武汉高德红外股份有限公司

乙方（出卖人）：武汉盈如鑫虹科技有限公司

1. 采购产品明细：

序号 | 物料编码 | 产品名称 | 不含税单价（元） | 含税单价（元） | 数量 | 单位 | 含税总价（元） | 生产厂家 | 交货期 | 备注
--- | --- | --- | --- | --- | --- | --- | --- | --- | --- | ---
1 | YA030000 | 1530A | 1.1062 | 1.25 | 200 | 米 | 250.00 | 凌云公司 | 2026-08-03 |
2 | YA030000 | 1531A | 4.2920 | 4.85 | 100 | 米 | 485.00 | 凌云公司 | 2026-08-03 |

含税总金额合计（RMB） | 人民币小写：2815元（人民币大写：贰仟捌佰壹拾伍元整）

其他说明：以上含税总金额为包含税费、运费等各项费用的到货价"""


def test_plain_parses_markdown_table():
    """Markdown 表格行应归为单个 label=table 的 Block,带 TableStructure。"""
    blocks = _parse_plain_content(_PAGE0_PLAIN, 0, _meta())
    tables = [b for b in blocks if b.label == "table"]
    assert len(tables) == 1
    tbl = tables[0].table
    assert tbl is not None
    # 表头 11 列
    assert len(tbl.headers) == 11
    assert tbl.headers[0] == "序号"
    # 数据行 2 行(明细)+ 2 行(合计/说明,因它们也含 |)
    assert len(tbl.rows) >= 2
    assert tbl.rows[0][0] == "1"
    # 不含税单价在第 3 列(序号0 物料编码1 产品名称2)
    assert tbl.rows[0][3] == "1.1062"


def test_plain_parses_paragraphs():
    """非表格行应归为 label=text 的 Block。"""
    blocks = _parse_plain_content(_PAGE0_PLAIN, 0, _meta())
    texts = [b for b in blocks if b.label == "text"]
    contents = [b.content for b in texts]
    assert any("采购合同" in c for c in contents)
    assert any("甲方" in c and "武汉高德红外" in c for c in contents)
    assert any("乙方" in c and "武汉盈如鑫虹" in c for c in contents)


def test_plain_strips_md_separator():
    """Markdown 分隔行(---|---)应被丢弃,不进入 TableStructure。"""
    md = "a | b | c\n--- | --- | ---\n1 | 2 | 3"
    blocks = _parse_plain_content(md, 0, _meta())
    tables = [b for b in blocks if b.label == "table"]
    assert len(tables) == 1
    tbl = tables[0].table
    assert tbl.headers == ["a", "b", "c"]
    assert tbl.rows == [["1", "2", "3"]]


def test_plain_single_pipe_line_not_table():
    """单行含 | 不应误判为表格(需≥2行)。"""
    md = "地址：xx路 | 电话：123"
    blocks = _parse_plain_content(md, 0, _meta())
    tables = [b for b in blocks if b.label == "table"]
    assert len(tables) == 0
    # 回退为 text
    assert all(b.label == "text" for b in blocks)


def test_loc_fallback_branch():
    """含 <|LOC_|> 标记的内容应走 LOC 解析分支。"""
    loc_content = "标题<|LOC_10|><|LOC_20|><|LOC_30|><|LOC_20|><|LOC_30|><|LOC_40|><|LOC_10|><|LOC_40|>"
    blocks = _parse_loc_content(loc_content, 0, _meta())
    assert len(blocks) == 1
    assert blocks[0].content == "标题"
    # bbox 换算为 PDF pt(LOC 0-1000 空间)
    assert len(blocks[0].bbox) == 4
    assert blocks[0].bbox[0] > 0  # x1


def test_loc_polygon_uses_all_points_for_outer_bbox():
    """Spotting 的异形多边形应取全部顶点外接框，而不是固定前四个点。"""
    content = (
        "弯曲文本"
        "<|LOC_100|><|LOC_200|><|LOC_500|><|LOC_180|>"
        "<|LOC_650|><|LOC_400|><|LOC_300|><|LOC_520|>"
        "<|LOC_80|><|LOC_350|>"
    )
    blocks = _parse_loc_content(content, 0, _meta())
    assert len(blocks) == 1
    assert blocks[0].bbox == [
        80 * 595 / 1000,
        180 * 842 / 1000,
        650 * 595 / 1000,
        520 * 842 / 1000,
    ]


def test_official_sdk_page_keeps_structure_table_and_bbox():
    page = SimpleNamespace(
        markdown_text="fallback markdown",
        pruned_result={
            "width": 1000,
            "height": 2000,
            "parsing_res_list": [
                {
                    "block_label": "text",
                    "block_content": "第一条 合同内容",
                    "block_bbox": [100, 200, 900, 400],
                },
                {
                    "block_label": "table",
                    "block_content": "名称 | 金额\n--- | ---\n设备 | 100",
                    "block_bbox": [[100, 500], [900, 500], [900, 900], [100, 900]],
                },
            ],
        },
    )

    blocks = _parse_official_page(page, 0, _meta())

    assert [block.label for block in blocks] == ["text", "table"]
    assert blocks[0].bbox == [59.5, 84.2, 535.5, 168.4]
    assert blocks[1].table is not None
    assert blocks[1].table.headers == ["名称", "金额"]
    assert blocks[1].table.rows == [["设备", "100"]]


def test_official_bbox_rejects_missing_or_degenerate_coordinates():
    assert _official_bbox_to_pt(None, 100, 100, 50, 50) == []
    assert _official_bbox_to_pt([10, 10, 10, 20], 100, 100, 50, 50) == []


def test_engine_official_sdk_mode_calls_parse_document(monkeypatch, tmp_path):
    captured: dict = {}

    class FakeOptions:
        def __init__(self, **kwargs):
            captured["options"] = kwargs

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def parse_document(self, **kwargs):
            captured["parse"] = kwargs
            assert (tmp_path / "scan.pdf").read_bytes() == b"%PDF-test"
            return SimpleNamespace(
                job_id="job-1",
                pages=[
                    SimpleNamespace(
                        markdown_text="第一条 合同内容",
                        pruned_result={
                            "width": 1000,
                            "height": 2000,
                            "parsing_res_list": [
                                {
                                    "block_label": "text",
                                    "block_content": "第一条 合同内容",
                                    "block_bbox": [100, 200, 900, 400],
                                }
                            ],
                        },
                    )
                ],
            )

    monkeypatch.setattr(
        paddleocr_http, "_load_official_sdk", lambda: (FakeClient, FakeOptions)
    )
    pdf_path = tmp_path / "scan.pdf"
    pdf_path.write_bytes(b"%PDF-test")
    engine = PaddleOCREngine(
        api_mode="official_sdk",
        official_api_base="https://official.example.test",
        official_access_token="access-token",
        official_model="PaddleOCR-VL-1.6",
        timeout=123,
    )

    pages = engine.recognize(pdf_path, [_meta()])

    assert pages[0][0].content == "第一条 合同内容"
    assert captured["client"] == {
        "token": "access-token",
        "request_timeout": 123.0,
        "poll_timeout": 900.0,
        "base_url": "https://official.example.test",
    }
    assert captured["parse"]["file_path"] == str(pdf_path)
    assert captured["parse"]["model"] == "PaddleOCR-VL-1.6"
    assert captured["options"]["use_layout_detection"] is True
    assert captured["options"]["use_doc_unwarping"] is False


def test_engine_official_layout_api_uses_sync_endpoint(monkeypatch, tmp_path):
    captured: dict = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "result": {
                    "layoutParsingResults": [
                        {
                            "markdown": {"text": "第一条 合同内容", "images": {}},
                            "prunedResult": {
                                "width": 1000,
                                "height": 2000,
                                "parsing_res_list": [
                                    {
                                        "block_label": "text",
                                        "block_content": "第一条 合同内容",
                                        "block_bbox": [100, 200, 900, 400],
                                    }
                                ],
                            },
                        }
                    ]
                }
            }

    def fake_post(url, *, json, headers, timeout):
        captured.update(
            {"url": url, "json": json, "headers": headers, "timeout": timeout}
        )
        return FakeResponse()

    monkeypatch.setattr(paddleocr_http.requests, "post", fake_post)
    pdf_path = tmp_path / "scan.pdf"
    pdf_path.write_bytes(b"%PDF-test")
    engine = PaddleOCREngine(
        api_mode="official_sdk",
        official_api_base="https://official.example.test/layout-parsing",
        official_access_token="access-token",
        timeout=123,
    )

    pages = engine.recognize(pdf_path, [_meta()])

    assert pages[0][0].content == "第一条 合同内容"
    assert captured["url"] == "https://official.example.test/layout-parsing"
    assert captured["headers"]["Authorization"] == "token access-token"
    assert captured["timeout"] == 123.0
    assert captured["json"]["fileType"] == 0
    assert captured["json"]["file"] == "JVBERi10ZXN0"
    assert captured["json"]["useRegionDetection"] is True
    assert captured["json"]["useDocUnwarping"] is False


def test_engine_official_sdk_honors_longer_poll_timeout(monkeypatch, tmp_path):
    captured: dict = {}

    class FakeOptions:
        def __init__(self, **_kwargs):
            pass

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def parse_document(self, **_kwargs):
            return SimpleNamespace(job_id="job-long", pages=[])

    monkeypatch.setattr(
        paddleocr_http, "_load_official_sdk", lambda: (FakeClient, FakeOptions)
    )
    pdf_path = tmp_path / "scan.pdf"
    pdf_path.write_bytes(b"%PDF-test")
    engine = PaddleOCREngine(
        api_mode="official_sdk",
        official_access_token="access-token",
        timeout=1200,
    )

    engine.recognize(pdf_path, [])

    assert captured["request_timeout"] == 1200.0
    assert captured["poll_timeout"] == 1200.0


def test_engine_official_sdk_recognize_text_uses_temporary_image(
    monkeypatch,
):
    seen: dict = {}

    class FakeOptions:
        def __init__(self, **_kwargs):
            pass

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def parse_document(self, **kwargs):
            image_path = kwargs["file_path"]
            seen["path"] = image_path
            seen["bytes"] = Path(image_path).read_bytes()
            return SimpleNamespace(
                job_id="job-image",
                pages=[SimpleNamespace(markdown_text="识别文本")],
            )

    monkeypatch.setattr(
        paddleocr_http, "_load_official_sdk", lambda: (FakeClient, FakeOptions)
    )
    engine = PaddleOCREngine(
        api_mode="official_sdk", official_access_token="token"
    )

    assert engine.recognize_text(b"png-data") == "识别文本"
    assert seen["bytes"] == b"png-data"
    assert not Path(seen["path"]).exists()


def test_loc_markdown_table_keeps_structure_and_bbox():
    """LOC 坐标与 Markdown 表格同时出现时不能把表格降级为普通文本。"""
    loc = "<|LOC_10|><|LOC_20|><|LOC_900|><|LOC_20|><|LOC_900|><|LOC_80|><|LOC_10|><|LOC_80|>"
    content = "\n".join([
        f"名称 | 数量 | 金额{loc}",
        f"--- | --- | ---{loc}",
        f"商品A | 2 | 100{loc}",
    ])
    blocks = _parse_loc_content(content, 0, _meta())
    tables = [block for block in blocks if block.label == "table"]
    assert len(tables) == 1
    assert tables[0].table.headers == ["名称", "数量", "金额"]
    assert tables[0].table.rows == [["商品A", "2", "100"]]
    assert len(tables[0].bbox) == 4
    regions = _normalize_regions(tables, {0: _meta()})
    assert len(regions) == 1
    assert all(0 <= value <= 1 for value in regions[0].bbox)


def test_plain_parses_tab_separated_table():
    content = "名称\t数量\t金额\n商品A\t2\t100\n商品B\t3\t200"
    blocks = _parse_plain_content(content, 0, _meta())
    tables = [block for block in blocks if block.label == "table"]
    assert len(tables) == 1
    assert tables[0].table.rows[1] == ["商品B", "3", "200"]


def test_plain_parses_html_table_and_preserves_surrounding_text():
    content = "合同标题\n<table><tr><th>名称</th><th>金额</th></tr><tr><td>A</td><td>100</td></tr></table>\n合同尾部"
    blocks = _parse_plain_content(content, 0, _meta())
    tables = [block for block in blocks if block.label == "table"]
    texts = [block.content for block in blocks if block.label == "text"]
    assert len(tables) == 1
    assert tables[0].table.headers == ["名称", "金额"]
    assert tables[0].table.rows == [["A", "100"]]
    assert texts == ["合同标题", "合同尾部"]


def test_plain_table_allows_blank_lines_between_rows():
    content = "名称 | 金额\n\n--- | ---\n\nA | 100"
    blocks = _parse_plain_content(content, 0, _meta())
    tables = [block for block in blocks if block.label == "table"]
    assert len(tables) == 1
    assert tables[0].table.rows == [["A", "100"]]


def test_plain_table_pads_uneven_rows():
    table = _plain_text_to_table("名称 | 数量 | 金额\nA | 2")
    assert table.rows == [["A", "2", ""]]


def test_plain_collapses_hallucinated_repeated_table_tail():
    """真实 PaddleOCR 输出会重复同一续行直到 token 上限，应清除重复尾部。"""
    repeated = " | | JL-B 军绿色薄型内径 6mm | | "
    content = "\n".join([
        "序号 | 物料编码 | 产品名称 | 数量 | 金额",
        "--- | --- | --- | --- | ---",
        "1 | YA030000 | 绵纶丝编织套管 | 200 | 250.00",
        *([repeated] * 12),
        " |",
    ])
    blocks = _parse_plain_content(content, 0, _meta())
    table = next(block.table for block in blocks if block.label == "table")
    assert table.rows == [
        ["1", "YA030000", "绵纶丝编织套管", "200", "250.00"],
        ["", "", "JL-B 军绿色薄型内径 6mm", "", ""],
    ]


def test_plain_text_to_table():
    """_plain_text_to_table:首行 headers,其余 rows。"""
    norm = "序号 | 名称\n1 | A\n2 | B"
    tbl = _plain_text_to_table(norm)
    assert tbl is not None
    assert tbl.headers == ["序号", "名称"]
    assert tbl.rows == [["1", "A"], ["2", "B"]]


def test_plain_text_to_table_empty():
    """空文本返回 None。"""
    assert _plain_text_to_table("") is None
    assert _plain_text_to_table("   ") is None


def test_attach_spotting_bboxes_merges_consecutive_lines():
    content_blocks = _parse_plain_content(
        "第五条 付款方式 验收合格后30日内支付货款\n合同尾部",
        0,
        _meta(),
    )
    loc_a = "<|LOC_100|><|LOC_100|><|LOC_800|><|LOC_100|><|LOC_800|><|LOC_160|><|LOC_100|><|LOC_160|>"
    loc_b = "<|LOC_100|><|LOC_170|><|LOC_900|><|LOC_170|><|LOC_900|><|LOC_230|><|LOC_100|><|LOC_230|>"
    loc_c = "<|LOC_100|><|LOC_800|><|LOC_500|><|LOC_800|><|LOC_500|><|LOC_850|><|LOC_100|><|LOC_850|>"
    spotting = _parse_loc_content(
        f"第五条 付款方式{loc_a}\n验收合格后30日内支付货款{loc_b}\n合同尾部{loc_c}",
        0,
        _meta(),
    )

    merged = _attach_spotting_bboxes(content_blocks, spotting)

    assert len(merged[0].bbox) == 4
    assert merged[0].bbox[1] == 100 * 842 / 1000
    assert merged[0].bbox[3] == 230 * 842 / 1000
    assert merged[1].bbox == spotting[2].bbox


def test_engine_uses_ocr_content_and_spotting_coordinates(monkeypatch):
    engine = PaddleOCREngine(
        api_base="https://example.test/v1",
        api_key="test",
        model="PaddlePaddle/PaddleOCR-VL-1.5",
        max_retries=0,
        enable_spotting=True,
    )
    loc = "<|LOC_10|><|LOC_20|><|LOC_900|><|LOC_20|><|LOC_900|><|LOC_80|><|LOC_10|><|LOC_80|>"
    prompts: list[str] = []

    def fake_chat(_data_url: str, *, prompt: str = "OCR:", **_kwargs) -> str:
        prompts.append(prompt)
        if prompt == "OCR:":
            return "甲方：示例公司"
        return f"甲方：示例公司{loc}"

    monkeypatch.setattr(engine, "_chat", fake_chat)
    out: list[list] = [None]
    engine._recognize_page(0, b"png", _meta(), out)

    assert prompts == ["OCR:", "Spotting:"]
    assert out[0][0].content == "甲方：示例公司"
    assert len(out[0][0].bbox) == 4


def test_engine_skips_spotting_when_ocr_already_has_coordinates(monkeypatch):
    engine = PaddleOCREngine(
        api_base="https://example.test/v1",
        api_key="test",
        model="PaddlePaddle/PaddleOCR-VL-1.5",
        max_retries=0,
        enable_spotting=True,
    )
    loc = "<|LOC_10|><|LOC_20|><|LOC_900|><|LOC_20|><|LOC_900|><|LOC_80|><|LOC_10|><|LOC_80|>"
    prompts: list[str] = []

    def fake_chat(_data_url: str, *, prompt: str = "OCR:", **_kwargs) -> str:
        prompts.append(prompt)
        return f"甲方：示例公司{loc}"

    monkeypatch.setattr(engine, "_chat", fake_chat)
    out: list[list] = [None]
    engine._recognize_page(0, b"png", _meta(), out)

    assert prompts == ["OCR:"]
    assert len(out[0][0].bbox) == 4


def test_engine_does_not_spot_when_annotation_mode_is_disabled(monkeypatch):
    engine = PaddleOCREngine(
        api_base="https://example.test/v1",
        api_key="test",
        model="PaddlePaddle/PaddleOCR-VL-1.5",
        max_retries=0,
        enable_spotting=False,
    )
    prompts: list[str] = []

    def fake_chat(_data_url: str, *, prompt: str = "OCR:", **_kwargs) -> str:
        prompts.append(prompt)
        return "甲方：示例公司"

    monkeypatch.setattr(engine, "_chat", fake_chat)
    out: list[list] = [None]
    engine._recognize_page(0, b"png", _meta(), out)

    assert prompts == ["OCR:"]
    assert out[0][0].bbox == []


def test_spotting_failure_does_not_discard_ocr_content(monkeypatch):
    engine = PaddleOCREngine(
        api_base="https://example.test/v1",
        api_key="test",
        model="PaddlePaddle/PaddleOCR-VL-1.5",
        max_retries=0,
        enable_spotting=True,
    )

    def fake_chat(_data_url: str, *, prompt: str = "OCR:", **_kwargs) -> str:
        if prompt == "OCR:":
            return "甲方：示例公司"
        raise RuntimeError("spotting unavailable")

    monkeypatch.setattr(engine, "_chat", fake_chat)
    out: list[list] = [None]
    engine._recognize_page(0, b"png", _meta(), out)

    assert out[0][0].content == "甲方：示例公司"
    assert out[0][0].bbox == []


def test_engine_reads_paddleocr_config():
    """paddleocr 后端使用独立的 paddleocr_* 配置(不复用 llm_*)。"""
    eng = PaddleOCREngine()
    # 应与 paddleocr_* 配置一致(settings.paddleocr_api_base / _api_key / _model),
    # 与 llm_* 完全隔离。
    from document_comparison.config import settings
    assert eng.api_mode == settings.paddleocr_api_mode
    assert eng.api_base == settings.paddleocr_api_base
    assert eng.api_key == settings.paddleocr_api_key
    assert eng.model == settings.paddleocr_model
    assert eng.official_api_base == settings.paddleocr_official_api_base
    assert (
        eng.official_access_token
        == settings.paddleocr_official_access_token
    )
    assert eng.official_model == settings.paddleocr_official_model
