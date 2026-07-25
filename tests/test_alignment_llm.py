"""受限 LLM 条款候选裁决测试。"""
from __future__ import annotations

import json

import httpx

from document_comparison.align.llm_resolver import (
    AlignmentCandidate,
    resolve_alignment_candidates,
    resolve_raw_alignment_plan,
)
from document_comparison.config import settings
from document_comparison.models import RawAlignmentBlock


def test_resolver_accepts_only_candidate_ids_supplied_by_code(monkeypatch):
    candidate = AlignmentCandidate(
        candidate_id="w:w1|p:p1+p2",
        word_clause_ids=("w1",),
        pdf_clause_ids=("p1", "p2"),
        similarity=0.80,
        relation="one_to_many",
        word_text="交付后付款并在验收后结清",
        pdf_text="交付后付款\n验收后结清",
        word_parent_paths=(("付款方式",),),
        pdf_parent_paths=(("付款方式",), ("付款方式",)),
    )
    captured: dict = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        content = json.dumps(
            {
                "selected": [
                    {
                        "candidate_id": candidate.candidate_id,
                        "confidence": 0.91,
                        "reason": "相邻两段共同组成付款约定",
                    },
                    {
                        "candidate_id": "not-generated-by-code",
                        "confidence": 1,
                        "reason": "必须忽略",
                    },
                ]
            },
            ensure_ascii=False,
        )
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": content}}]},
        )

    original_client = httpx.Client
    monkeypatch.setattr(
        "document_comparison.align.llm_resolver.httpx.Client",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(respond),
            **kwargs,
        ),
    )
    monkeypatch.setattr(settings, "judge_api_base", "https://judge.example/v1")
    monkeypatch.setattr(settings, "judge_api_key", "secret")
    monkeypatch.setattr(settings, "judge_model", "judge-model")
    monkeypatch.setattr(settings, "llm_max_retries", 0)

    decisions = resolve_alignment_candidates([candidate])

    assert [(item.candidate_id, item.confidence) for item in decisions] == [
        (candidate.candidate_id, 0.91)
    ]
    assert captured["temperature"] == 0
    assert captured["response_format"] == {"type": "json_object"}


def test_raw_plan_resolver_returns_block_character_spans(monkeypatch):
    word_blocks = [
        RawAlignmentBlock(block_id="w-1", text="一 、 合同标的"),
    ]
    pdf_blocks = [
        RawAlignmentBlock(block_id="p-1", text="一、合同标的", bbox=[10, 10, 120, 30]),
    ]
    captured: dict = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        content = json.dumps(
            {
                "groups": [
                    {
                        "word_spans": [
                            {"block_id": "w-1", "start": 0, "end": len(word_blocks[0].text)}
                        ],
                        "pdf_spans": [
                            {"block_id": "p-1", "start": 0, "end": len(pdf_blocks[0].text)}
                        ],
                        "confidence": 0.98,
                        "reason": "同一章节标题",
                    }
                ]
            },
            ensure_ascii=False,
        )
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": content}}]},
        )

    original_client = httpx.Client
    monkeypatch.setattr(
        "document_comparison.align.llm_resolver.httpx.Client",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(respond),
            **kwargs,
        ),
    )
    monkeypatch.setattr(settings, "judge_api_base", "https://judge.example/v1")
    monkeypatch.setattr(settings, "judge_api_key", "secret")
    monkeypatch.setattr(settings, "judge_model", "judge-model")
    monkeypatch.setattr(settings, "llm_max_retries", 0)

    plan = resolve_raw_alignment_plan(word_blocks, pdf_blocks)

    assert plan is not None
    assert plan.groups[0].word_spans[0].block_id == "w-1"
    assert plan.groups[0].pdf_spans[0].end == len(pdf_blocks[0].text)
    prompt = json.loads(captured["messages"][1]["content"])
    assert prompt["word_blocks"][0]["block_id"] == "w-1"
    assert prompt["pdf_blocks"][0]["bbox"] == [10.0, 10.0, 120.0, 30.0]
    assert captured["response_format"] == {"type": "json_object"}


def test_raw_plan_timeout_is_not_retried(monkeypatch):
    calls = 0

    def timeout(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("slow planner", request=request)

    original_client = httpx.Client
    monkeypatch.setattr(
        "document_comparison.align.llm_resolver.httpx.Client",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(timeout),
            **kwargs,
        ),
    )
    monkeypatch.setattr(
        "document_comparison.align.llm_resolver.time.sleep",
        lambda _seconds: None,
    )
    monkeypatch.setattr(settings, "judge_api_base", "https://judge.example/v1")
    monkeypatch.setattr(settings, "judge_model", "judge-model")
    monkeypatch.setattr(settings, "llm_max_retries", 2)

    plan = resolve_raw_alignment_plan(
        [RawAlignmentBlock(block_id="w-1", text="合同标的")],
        [RawAlignmentBlock(block_id="p-1", text="合同标的")],
    )

    assert plan is None
    assert calls == 1
