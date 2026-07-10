"""运行时配置。从环境变量读取,提供合理默认值。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


@dataclass
class Settings:
    # —— 服务 ——
    host: str = field(default_factory=lambda: _env("DC_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(_env("DC_PORT", "8000")))

    # —— 认证:逗号分隔的合法 API Key。生产应替换为数据库/秘密管理。 ——
    # 默认含一个开发用 key,仅用于本地。
    api_keys: list[str] = field(
        default_factory=lambda: _env(
            "DC_API_KEYS", "dev-key-please-change"
        ).split(",")
    )

    # —— 存储 ——
    storage_dir: Path = field(
        default_factory=lambda: Path(_env("DC_STORAGE_DIR", "./.dc_data"))
    )

    # —— 前端静态文件目录 ——
    # 设置后 FastAPI 会挂载此目录并提供 SPA fallback;留空则不提供静态文件。
    static_dir: Path | None = field(
       default_factory=lambda: (
           Path(p) if (p := _env("DC_STATIC_DIR", "")).strip() else None
      )
   )

    # —— OCR 引擎选择:mock | vllm(llm 为别名)——
    ocr_backend: str = field(default_factory=lambda: _env("DC_OCR_BACKEND", "vllm"))

    # —— 远端多模态推理(vllm backend 用)——
    # 兼容 OpenAI Chat Completions 协议(base_url 指向 vLLM / SGLang / 云端 API)。
    # 所有多模态模型(PaddleOCR-VL / 通义千问 VL / GPT-4o 等)共用此配置,
    # 按 model 路由到对应服务;无需本地安装任何 OCR 引擎。
    llm_api_base: str = field(
        default_factory=lambda: _env("DC_LLM_API_BASE", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    )
    llm_api_key: str = field(default_factory=lambda: _env("DC_LLM_API_KEY", ""))
    llm_model: str = field(
        default_factory=lambda: _env("DC_LLM_MODEL", "qwen-vl-max")
    )
    # 请求超时(秒)。单页识别可能较慢,默认 120s(仅作用于读取;连接超时固定 10s)。
    llm_timeout: float = field(default_factory=lambda: float(_env("DC_LLM_TIMEOUT", "120")))
    # 最大并发页数(逐页并行,受 LLM 端限流约束)
    llm_max_concurrency: int = field(
        default_factory=lambda: int(_env("DC_LLM_MAX_CONCURRENCY", "4"))
    )
    # 单页瞬态失败(超时 / 429 / 5xx)重试次数
    llm_max_retries: int = field(default_factory=lambda: int(_env("DC_LLM_MAX_RETRIES", "2")))

    # —— 向量引擎选择:mock | bge ——
    embed_backend: str = field(
        default_factory=lambda: _env("DC_EMBED_BACKEND", "mock")
    )

    # —— 比对阈值(§9.1 双阈值)——
    similarity_identical: float = 0.98  # >= 视为一致
    similarity_modified: float = 0.85  # < 视为实质修改;之间为疑似
    align_similarity: float = 0.85  # 对齐配对确认阈值

    # —— PDF 渲染 ——
    pdf_render_dpi: int = 300

    # —— 任务 ——
    max_concurrent_tasks: int = 4

    # —— Webhook ——
    webhook_max_retries: int = 3
    webhook_timeout_seconds: float = 10.0

    @property
    def uploads_dir(self) -> Path:
        return self.storage_dir / "uploads"

    @property
    def reports_dir(self) -> Path:
        return self.storage_dir / "reports"

    def ensure_dirs(self) -> None:
        for d in (self.storage_dir, self.uploads_dir, self.reports_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()


# —— LLM 配置持久化(供 UI 维护,覆盖环境变量默认值)——
# 配置文件:.dc_data/llm_config.json,只存被覆盖的字段。
# 优先级:持久化文件 > 环境变量 > dataclass 默认值。
import json as _json

_LLM_CONFIG_FIELDS = (
    "ocr_backend",
    "llm_api_base",
    "llm_api_key",
    "llm_model",
    "llm_timeout",
    "llm_max_concurrency",
)


def llm_config_path() -> Path:
    return Path(_env("DC_STORAGE_DIR", "./.dc_data")) / "llm_config.json"


def load_llm_overrides() -> dict:
    """从持久化文件读取 LLM 配置覆盖。文件不存在或损坏返回空 dict。"""
    path = llm_config_path()
    if not path.exists():
        return {}
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if k in _LLM_CONFIG_FIELDS}
    except Exception:
        return {}


def save_llm_overrides(overrides: dict) -> None:
    """写入 LLM 配置覆盖(合并已有值)。空字符串字段会被剔除,回退到默认。"""
    path = llm_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = load_llm_overrides()
    for k in _LLM_CONFIG_FIELDS:
        if k in overrides:
            v = overrides[k]
            if v is None or v == "":
                merged.pop(k, None)
            else:
                merged[k] = v
    path.write_text(_json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    apply_llm_overrides()


def apply_llm_overrides() -> None:
    """把持久化覆盖应用到运行时 settings 单例。启动时与保存后各调一次。"""
    overrides = load_llm_overrides()
    if "ocr_backend" in overrides:
        settings.ocr_backend = overrides["ocr_backend"]
    if "llm_api_base" in overrides:
        settings.llm_api_base = overrides["llm_api_base"]
    if "llm_api_key" in overrides:
        settings.llm_api_key = overrides["llm_api_key"]
    if "llm_model" in overrides:
        settings.llm_model = overrides["llm_model"]
    if "llm_timeout" in overrides:
        settings.llm_timeout = float(overrides["llm_timeout"])
    if "llm_max_concurrency" in overrides:
        settings.llm_max_concurrency = int(overrides["llm_max_concurrency"])


# 启动时应用一次持久化覆盖
apply_llm_overrides()
