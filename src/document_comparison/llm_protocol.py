"""LLM 对话协议适配:OpenAI Chat Completions / OpenAI Responses / Anthropic Messages。

各调用点(ocr/llm.py、judge.py、llm_diff.py、llm_resolver.py)统一以 OpenAI Chat
Completions 的规范形态构造 payload(messages + temperature + response_format +
chat_template_kwargs),本模块在发送前按所选协议转换 URL / 认证头 / 请求体,
并在响应到达后按协议提取 assistant 文本。

设计约束:
- 不 import 包内其它模块(config / observability 等),保持零依赖避免循环引用;
  协议值与超时/重试等由调用方注入。
- openai 协议路径保持与历史行为逐字节一致(包括 Bearer 头与 payload 原样透传)。
- anthropic / openai_responses 丢弃 response_format 与 chat_template_kwargs:
  前者 Anthropic 无对应字段(JSON 由提示词约束 + _llm_json 容错兜底),后者是
  vLLM 专属参数,OpenAI 官方端点会拒绝未知字段。随之丢掉 Qwen3 的
  enable_thinking=False 开关,故新协议解析时统一剥离 <think> 块兜底。
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

VALID_PROTOCOLS = ("openai", "openai_responses", "anthropic")

# Anthropic Messages API 的 max_tokens 为必填字段;OpenAI Responses 的
# max_output_tokens 同样给一个确定上限。8192 是各代 Claude 普遍接受的输出上限,
# 足够覆盖单页 OCR 的 JSON 版面块。
DEFAULT_MAX_OUTPUT_TOKENS = 8192

_ANTHROPIC_VERSION = "2023-06-01"

# <think>…</think> 思考链块;未闭合形态只删到行尾——思考链后通常换行接正文,
# 整段删到结尾会把模型输出一起丢掉。仅 anthropic / responses 协议剥离:这两条
# 通道没有 vLLM 的 enable_thinking 开关,Qwen3 类模型可能输出思考链。
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_THINK_TAIL_RE = re.compile(r"<think>[^\n]*")


def _strip_think_blocks(text: str) -> str:
    if "</think>" in text:
        text = _THINK_BLOCK_RE.sub("", text)
    if "<think>" in text:
        text = _THINK_TAIL_RE.sub("", text)
    return text.strip()


def _split_data_url(url: str) -> tuple[str, str] | None:
    """拆解 ``data:<media_type>;base64,<b64>``;非 base64 data URL 返回 None。"""
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    header, _, encoded = url.partition(",")
    media_type = header[5:].split(";")[0].strip() or "image/png"
    if "base64" not in header:
        return None
    return media_type, encoded


def _to_anthropic_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """把 OpenAI Chat Completions 形态转换为 Anthropic Messages 形态。

    - system 消息上提为顶层 ``system``(多条以空行拼接);
    - ``image_url`` data URL 转为 base64 source 块(媒体类型取自 data URL 头);
    - 丢弃 response_format / chat_template_kwargs / 其它 OpenAI 专属字段;
    - max_tokens 必填,缺省用 DEFAULT_MAX_OUTPUT_TOKENS。
    """
    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []
    for msg in payload.get("messages") or []:
        role = msg.get("role")
        content = msg.get("content")
        if role == "system":
            if isinstance(content, list):
                system_parts.extend(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            elif content:
                system_parts.append(str(content))
            continue
        messages.append({"role": role or "user", "content": _anthropic_content(content)})

    out: dict[str, Any] = {
        "model": payload.get("model"),
        "messages": messages,
        "max_tokens": payload.get("max_tokens") or DEFAULT_MAX_OUTPUT_TOKENS,
    }
    if system_parts:
        out["system"] = "\n\n".join(p for p in system_parts if p)
    if payload.get("temperature") is not None:
        out["temperature"] = payload["temperature"]
    return out


def _anthropic_content(content: Any) -> Any:
    if not isinstance(content, list):
        return content
    blocks: list[Any] = []
    for part in content:
        if not isinstance(part, dict):
            blocks.append(part)
            continue
        if part.get("type") == "text":
            blocks.append({"type": "text", "text": part.get("text", "")})
        elif part.get("type") == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            split = _split_data_url(url)
            if split is not None:
                media_type, encoded = split
                blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": encoded,
                        },
                    }
                )
            elif isinstance(url, str) and url:
                blocks.append(
                    {"type": "image", "source": {"type": "url", "url": url}}
                )
        else:
            blocks.append(part)
    return blocks


def _to_responses_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """把 OpenAI Chat Completions 形态转换为 OpenAI Responses 形态。

    - system 消息上提为顶层 ``instructions``;
    - text 块改 ``input_text``,image_url 块改 ``input_image``(url 字符串原样);
    - ``response_format json_object`` 改写为 ``text.format``;
    - max_tokens 改名 ``max_output_tokens``,缺省用 DEFAULT_MAX_OUTPUT_TOKENS;
    - 丢弃 chat_template_kwargs(vLLM 专属,官方端点拒绝未知字段)。
    """
    instructions_parts: list[str] = []
    inputs: list[dict[str, Any]] = []
    for msg in payload.get("messages") or []:
        role = msg.get("role")
        content = msg.get("content")
        if role == "system":
            if isinstance(content, list):
                instructions_parts.extend(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            elif content:
                instructions_parts.append(str(content))
            continue
        inputs.append(
            {"role": role or "user", "content": _responses_content(content)}
        )

    out: dict[str, Any] = {
        "model": payload.get("model"),
        "input": inputs,
    }
    if instructions_parts:
        out["instructions"] = "\n\n".join(p for p in instructions_parts if p)
    if payload.get("temperature") is not None:
        out["temperature"] = payload["temperature"]
    response_format = payload.get("response_format")
    if isinstance(response_format, dict) and response_format.get("type"):
        out["text"] = {"format": {"type": response_format["type"]}}
    if payload.get("max_tokens"):
        out["max_output_tokens"] = payload["max_tokens"]
    else:
        out["max_output_tokens"] = DEFAULT_MAX_OUTPUT_TOKENS
    return out


def _responses_content(content: Any) -> Any:
    if not isinstance(content, list):
        return content
    parts_out: list[Any] = []
    for part in content:
        if not isinstance(part, dict):
            parts_out.append(part)
            continue
        if part.get("type") == "text":
            parts_out.append({"type": "input_text", "text": part.get("text", "")})
        elif part.get("type") == "image_url":
            image = part.get("image_url") or {}
            block: dict[str, Any] = {"type": "input_image"}
            if isinstance(image.get("url"), str):
                block["image_url"] = image["url"]
            if isinstance(image.get("detail"), str):
                block["detail"] = image["detail"]
            parts_out.append(block)
        else:
            parts_out.append(part)
    return parts_out


def build_chat_request(
    *,
    api_base: str,
    api_key: str,
    payload: dict[str, Any],
    protocol: str = "openai",
) -> tuple[str, dict[str, str], dict[str, Any]]:
    """按协议构造 (url, headers, wire_payload)。

    ``payload`` 为 OpenAI Chat Completions 规范形态(调用点现有构造方式);
    openai 协议下 headers/payload 原样透传,仅拼 URL。空 api_key 不构造认证头
    (本地 vLLM / 无鉴权网关)。
    """
    base = (api_base or "").rstrip("/")
    if protocol == "anthropic":
        url = base + "/messages"
        headers = {"anthropic-version": _ANTHROPIC_VERSION}
        if api_key:
            headers["x-api-key"] = api_key
        return url, headers, _to_anthropic_payload(payload)
    if protocol == "openai_responses":
        url = base + "/responses"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        return url, headers, _to_responses_payload(payload)
    url = base + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    return url, headers, payload


def parse_chat_content(data: dict[str, Any], *, protocol: str = "openai") -> str:
    """按协议从响应 JSON 提取 assistant 文本。

    输出被截断(Anthropic stop_reason=max_tokens / Responses status=incomplete)
    时记 warning,内容照常返回——截断容忍策略与 chat completions 通道一致,
    由各消费端的 JSON 容错解析兜底。
    """
    if protocol == "anthropic":
        blocks = data.get("content") or []
        texts = [
            b.get("text", "")
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        if data.get("stop_reason") == "max_tokens":
            logger.warning(
                "anthropic response truncated (stop_reason=max_tokens); "
                "考虑上调服务端模型输出上限"
            )
        return _strip_think_blocks("".join(texts))
    if protocol == "openai_responses":
        outputs = data.get("output") or []
        texts: list[str] = []
        for item in outputs:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue  # 跳过 reasoning 等非消息项
            for block in item.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "output_text":
                    texts.append(block.get("text", ""))
        if data.get("status") == "incomplete":
            logger.warning(
                "responses api output incomplete (reason=%s); "
                "考虑上调服务端模型输出上限",
                (data.get("incomplete_details") or {}).get("reason"),
            )
        return _strip_think_blocks("".join(texts))
    return data["choices"][0]["message"]["content"]
