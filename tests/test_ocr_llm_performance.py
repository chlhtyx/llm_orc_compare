"""LLM OCR 的连接复用和快速失败测试（不访问真实网络）。"""
from __future__ import annotations

import json

import httpx
import pytest

from document_comparison.models import PageMeta
from document_comparison.ocr.llm import LLMOCREngine


def _response(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    assert body["model"] == "vl-test"
    return httpx.Response(
        200,
        request=request,
        json={"choices": [{"message": {"content": '{"blocks": []}'}}]},
    )


def test_empty_key_omits_authorization_header():
    engine = LLMOCREngine(
        api_base="http://ocr.test/v1", api_key="", model="vl-test",
        max_retries=0,
    )
    with httpx.Client(transport=httpx.MockTransport(_response)) as client:
        engine._chat("data:image/png;base64,eA==", client=client)
        # handler 中请求成功即证明没有构造非法的 ``Bearer `` 请求头。


@pytest.mark.parametrize(
    ("api_base", "model", "message"),
    [("", "vl-test", "llm_api_base"), ("http://ocr.test/v1", "", "llm_model")],
)
def test_invalid_config_fails_before_render(api_base, model, message):
    engine = LLMOCREngine(api_base=api_base, model=model)
    with pytest.raises(ValueError, match=message):
        engine.recognize(
            "not-opened.pdf",
            [PageMeta(page_index=0, width_px=1, height_px=1,
                      pdf_width_pt=1, pdf_height_pt=1)],
        )


def test_4xx_is_not_retried():
    attempts = {"count": 0}

    def unauthorized(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(401, request=request, text="unauthorized")

    engine = LLMOCREngine(
        api_base="http://ocr.test/v1", api_key="bad", model="vl-test",
        max_retries=3,
    )
    with httpx.Client(transport=httpx.MockTransport(unauthorized)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            engine._chat("data:image/png;base64,eA==", client=client)
    assert attempts["count"] == 1
