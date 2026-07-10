"""PaddleOCR-VL 引擎(通过 vLLM 部署)。

PaddleOCR-VL 在生产环境以 vLLM(或 SGLang / FastDeploy)部署,
暴露 OpenAI 兼容的 Chat Completions API。因此本引擎与 llm.py 走同一套
调用逻辑,只是默认指向 PaddleOCR-VL 的 vLLM 端点与模型名。

配置复用 config.Settings 的 llm_* 字段(api_base / api_key / model / …)。
如需与"通用 LLM"端点区分,可在 UI 里切换模型名,例如:
  - PaddleOCR-VL:  model = PaddleOCR-VL-1.5,api_base = http://vllm-paddle:8000/v1
  - 通义千问 VL:    model = qwen-vl-max,      api_base = https://dashscope...
两者共用同一套配置位,按当前选择的模型路由到对应 vLLM 服务。
"""
from __future__ import annotations

from .llm import LLMOCREngine


class PaddleOCRVLEngine(LLMOCREngine):
    """PaddleOCR-VL(vLLM 部署)引擎。

    与 LLMOCREngine 完全一致——都是调用 OpenAI 兼容的多模态 API。
    保留此类名仅为向后兼容(旧配置 DC_OCR_BACKEND=paddle 仍可用)。
    """

    pass
