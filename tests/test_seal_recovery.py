from __future__ import annotations

from io import BytesIO
import base64
from types import SimpleNamespace

from PIL import Image, ImageDraw

from document_comparison.config import settings
from document_comparison.models import PageMeta
from document_comparison.ocr.llm import LLMOCREngine
from document_comparison.ocr.paddleocr_http import PaddleOCREngine
from document_comparison.ocr.seal import (
    SealRecoveryDiagnostic,
    prepare_seal_variant,
)
from document_comparison.ocr.trusted import TrustedPDFReader
from document_comparison.ocr.whole_doc import ocr_whole_document
from document_comparison.parsing.pdf import get_page_metas


def _png_with_red_seal() -> bytes:
    image = Image.new("RGB", (800, 1000), "white")
    draw = ImageDraw.Draw(image)
    draw.text((180, 470), "ACCOUNT 10210000001253", fill="black")
    draw.ellipse((260, 360, 650, 750), outline=(205, 35, 35), width=18)
    draw.line((300, 555, 610, 555), fill=(205, 35, 35), width=12)
    draw.rectangle((380, 460, 390, 640), fill="black")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _png_without_seal() -> bytes:
    image = Image.new("RGB", (800, 1000), "white")
    ImageDraw.Draw(image).text((180, 470), "CONTRACT BODY", fill="black")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _pdf_with_image(path, png_bytes: bytes) -> None:
    import pymupdf as fitz

    with fitz.open() as document:
        page = document.new_page(width=576, height=720)
        page.insert_image(page.rect, stream=png_bytes)
        document.save(path)


def _official_result(text: str, *, label: str = "text"):
    return SimpleNamespace(
        pages=[
            SimpleNamespace(
                markdown_text="",
                pruned_result={
                    "width": 1600,
                    "height": 2000,
                    "parsing_res_list": [
                        {
                            "block_label": label,
                            "block_content": text,
                            "block_bbox": [320, 900, 1280, 1100],
                        }
                    ],
                },
            )
        ]
    )


def test_prepare_seal_variant_detects_red_seal_and_preserves_page_size():
    prepared = prepare_seal_variant(_png_with_red_seal())

    assert prepared is not None
    assert prepared.width_px == 800
    assert prepared.height_px == 1000
    assert prepared.regions_px

    with Image.open(BytesIO(prepared.png_bytes)) as image:
        assert image.size == (800, 1000)


def test_prepare_seal_variant_skips_pages_without_a_seal():
    image = Image.new("RGB", (800, 1000), "white")
    draw = ImageDraw.Draw(image)
    draw.text((180, 470), "CONTRACT BODY", fill="black")
    output = BytesIO()
    image.save(output, format="PNG")

    assert prepare_seal_variant(output.getvalue()) is None


def test_prepare_seal_variant_lightens_red_ink_without_erasing_black_ink():
    prepared = prepare_seal_variant(_png_with_red_seal())

    assert prepared is not None
    with Image.open(BytesIO(prepared.png_bytes)).convert("L") as recovered:
        assert recovered.getpixel((310, 555)) >= 190
        assert recovered.getpixel((385, 500)) <= 10


def test_llm_ocr_uses_recovered_text_for_a_page_with_red_seal(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sealed.pdf"
    _pdf_with_image(pdf_path, _png_with_red_seal())

    responses = iter(
        [
            '{"blocks":[{"label":"text","content":"ACCOUNT 1021?","bbox":[0.2,0.45,0.8,0.55]},'
            '{"label":"seal","content":"CONTRACT SEAL","bbox":[0.3,0.36,0.82,0.75]}]}',
            '{"blocks":[{"label":"text","content":"ACCOUNT 10210000001253","bbox":[0.2,0.45,0.8,0.55]}]}',
        ]
    )
    engine = LLMOCREngine(api_base="http://ocr.test/v1", model="vision-test")
    monkeypatch.setattr(engine, "_chat", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(settings, "seal_recovery_enabled", True, raising=False)

    pages = engine.recognize(
        pdf_path,
        [
            PageMeta(
                page_index=0,
                width_px=1600,
                height_px=2000,
                pdf_width_pt=576,
                pdf_height_pt=720,
            )
        ],
    )

    assert [block.content for block in pages[0]] == ["ACCOUNT 10210000001253"]


def test_llm_ocr_does_not_retry_a_page_without_a_red_seal(monkeypatch, tmp_path):
    pdf_path = tmp_path / "plain.pdf"
    _pdf_with_image(pdf_path, _png_without_seal())
    responses = iter(
        [
            '{"blocks":[{"label":"text","content":"CONTRACT BODY","bbox":[0.2,0.45,0.8,0.55]}]}'
        ]
    )
    engine = LLMOCREngine(api_base="http://ocr.test/v1", model="vision-test")
    monkeypatch.setattr(engine, "_chat", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(settings, "seal_recovery_enabled", True, raising=False)

    pages = engine.recognize(
        pdf_path,
        [
            PageMeta(
                page_index=0,
                width_px=1600,
                height_px=2000,
                pdf_width_pt=576,
                pdf_height_pt=720,
            )
        ],
    )

    assert [block.content for block in pages[0]] == ["CONTRACT BODY"]


def test_paddle_vllm_uses_recovered_text_for_a_page_with_red_seal(
    monkeypatch, tmp_path
):
    pdf_path = tmp_path / "sealed.pdf"
    _pdf_with_image(pdf_path, _png_with_red_seal())
    responses = iter(["ACCOUNT 1021?\nCONTRACT SEAL", "ACCOUNT 10210000001253"])
    engine = PaddleOCREngine(
        api_mode="vllm", api_base="http://ocr.test/v1", model="paddle-test"
    )
    monkeypatch.setattr(engine, "_chat", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(settings, "seal_recovery_enabled", True, raising=False)

    pages = engine.recognize(
        pdf_path,
        [
            PageMeta(
                page_index=0,
                width_px=1600,
                height_px=2000,
                pdf_width_pt=576,
                pdf_height_pt=720,
            )
        ],
    )

    assert [block.content for block in pages[0]] == ["ACCOUNT 10210000001253"]


def test_paddle_official_sdk_uses_recovered_text_for_a_page_with_red_seal(
    monkeypatch, tmp_path
):
    pdf_path = tmp_path / "sealed.pdf"
    _pdf_with_image(pdf_path, _png_with_red_seal())
    responses = iter(
        [_official_result("ACCOUNT 1021?"), _official_result("ACCOUNT 10210000001253")]
    )
    engine = PaddleOCREngine(
        api_mode="official_sdk",
        official_access_token="test-token",
        official_model="PaddleOCR-VL-1.6",
    )
    monkeypatch.setattr(
        engine, "_official_parse_document", lambda *_args, **_kwargs: next(responses)
    )
    monkeypatch.setattr(settings, "seal_recovery_enabled", True, raising=False)

    pages = engine.recognize(
        pdf_path,
        [
            PageMeta(
                page_index=0,
                width_px=1600,
                height_px=2000,
                pdf_width_pt=576,
                pdf_height_pt=720,
            )
        ],
    )

    assert [block.content for block in pages[0]] == ["ACCOUNT 10210000001253"]


def test_paddlex_serving_uses_recovered_text_for_a_page_with_red_seal(
    monkeypatch, tmp_path
):
    pdf_path = tmp_path / "sealed.pdf"
    _pdf_with_image(pdf_path, _png_with_red_seal())
    responses = iter(
        [_official_result("ACCOUNT 1021?"), _official_result("ACCOUNT 10210000001253")]
    )
    engine = PaddleOCREngine(
        api_mode="paddlex_serving",
        paddlex_api_base="http://paddlex.test",
        paddlex_endpoint="/layout-parsing",
    )
    monkeypatch.setattr(
        engine, "_paddlex_parse_document", lambda *_args, **_kwargs: next(responses)
    )
    monkeypatch.setattr(settings, "seal_recovery_enabled", True, raising=False)

    pages = engine.recognize(
        pdf_path,
        [
            PageMeta(
                page_index=0,
                width_px=1600,
                height_px=2000,
                pdf_width_pt=576,
                pdf_height_pt=720,
            )
        ],
    )

    assert [block.content for block in pages[0]] == ["ACCOUNT 10210000001253"]


def test_llm_ocr_rerenders_only_the_sealed_page_at_recovery_dpi(
    monkeypatch, tmp_path
):
    pdf_path = tmp_path / "sealed.pdf"
    _pdf_with_image(pdf_path, _png_with_red_seal())
    image_sizes: list[tuple[int, int]] = []
    responses = iter(
        [
            '{"blocks":[{"label":"text","content":"ACCOUNT 1021?","bbox":[0.2,0.45,0.8,0.55]}]}',
            '{"blocks":[{"label":"text","content":"ACCOUNT 10210000001253","bbox":[0.2,0.45,0.8,0.55]}]}',
        ]
    )

    def fake_chat(data_url: str, **_kwargs):
        payload = base64.b64decode(data_url.split(",", 1)[1])
        with Image.open(BytesIO(payload)) as image:
            image_sizes.append(image.size)
        return next(responses)

    engine = LLMOCREngine(api_base="http://ocr.test/v1", model="vision-test")
    monkeypatch.setattr(engine, "_chat", fake_chat)
    monkeypatch.setattr(settings, "seal_recovery_enabled", True, raising=False)
    monkeypatch.setattr(settings, "seal_recovery_dpi", 300, raising=False)

    engine.recognize(
        pdf_path,
        [
            PageMeta(
                page_index=0,
                width_px=1600,
                height_px=2000,
                pdf_width_pt=576,
                pdf_height_pt=720,
            )
        ],
    )

    assert image_sizes == [(1600, 2000), (2400, 3000)]


def test_trusted_reader_marks_conflicting_seal_recovery_as_unreliable(tmp_path):
    from document_comparison.models import Block

    pdf_path = tmp_path / "sealed.pdf"
    _pdf_with_image(pdf_path, _png_with_red_seal())

    class SealAwareFallback:
        last_truncated_pages: set[int] = set()
        last_seal_diagnostics = {
            0: SealRecoveryDiagnostic(
                reliable=False,
                reasons=("检测到印章，覆盖区二次 OCR 与原图结果不一致",),
                region_count=1,
            )
        }

        def recognize(self, _pdf_path, _page_metas, *, on_progress=None):
            return [
                [
                    Block(
                        block_id="recovered",
                        page_index=0,
                        label="text",
                        bbox=[100, 300, 500, 350],
                        content="ACCOUNT 10210000001253",
                    )
                ]
            ]

    reader = TrustedPDFReader(SealAwareFallback())
    reader.recognize(pdf_path, get_page_metas(pdf_path, dpi=200))

    assert reader.last_diagnostics[0].reliable is False
    assert "检测到印章" in "".join(reader.last_diagnostics[0].reasons)


def test_whole_document_ocr_uses_seal_variant_and_marks_conflict_for_review(
    monkeypatch, tmp_path
):
    pdf_path = tmp_path / "sealed.pdf"
    _pdf_with_image(pdf_path, _png_with_red_seal())

    class TextEngine:
        max_concurrency = 1
        timeout = 10

        def __init__(self):
            self.responses = iter(["ACCOUNT 1021?\nCONTRACT SEAL", "ACCOUNT 10210000001253"])

        def recognize_text(self, _image_bytes, *, client=None):
            return next(self.responses)

    monkeypatch.setattr(settings, "seal_recovery_enabled", True, raising=False)
    monkeypatch.setattr(settings, "seal_recovery_dpi", 300, raising=False)

    read = ocr_whole_document(pdf_path, TextEngine())

    assert read.text == "ACCOUNT 10210000001253"
    assert read.reliable is False
    assert "检测到印章" in "".join(read.diagnostic.reasons)


def test_llm_ocr_keeps_original_evidence_when_seal_retry_fails(
    monkeypatch, tmp_path
):
    pdf_path = tmp_path / "sealed.pdf"
    _pdf_with_image(pdf_path, _png_with_red_seal())
    responses = iter(
        [
            '{"blocks":[{"label":"text","content":"ACCOUNT 1021?","bbox":[0.2,0.45,0.8,0.55]}]}'
        ]
    )

    def fake_chat(*_args, **_kwargs):
        try:
            return next(responses)
        except StopIteration as exc:
            raise RuntimeError("seal retry unavailable") from exc

    engine = LLMOCREngine(api_base="http://ocr.test/v1", model="vision-test")
    monkeypatch.setattr(engine, "_chat", fake_chat)
    monkeypatch.setattr(settings, "seal_recovery_enabled", True, raising=False)

    pages = engine.recognize(
        pdf_path,
        [
            PageMeta(
                page_index=0,
                width_px=1600,
                height_px=2000,
                pdf_width_pt=576,
                pdf_height_pt=720,
            )
        ],
    )

    assert [block.content for block in pages[0]] == ["ACCOUNT 1021?"]
    assert engine.last_seal_diagnostics[0].reliable is False
