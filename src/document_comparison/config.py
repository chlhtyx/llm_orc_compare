"""运行时配置。

基础设施(服务、认证、存储)从环境变量读取;
LLM / OCR 相关配置统一持久化于 llm_config.json,不使用环境变量。
"""
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

    # —— 远端多模态推理(vllm backend 用)——
    # 兼容 OpenAI Chat Completions 协议(base_url 指向 vLLM / SGLang / 云端 API)。
    # 所有多模态模型(PaddleOCR-VL / 通义千问 VL / GPT-4o 等)共用此配置,
    # 按 model 路由到对应服务;无需本地安装任何 OCR 引擎。
    llm_api_base: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    # 请求超时(秒)。单页识别可能较慢,默认 120s(仅作用于读取;连接超时固定 10s)。
    llm_timeout: float = 120.0
    # 最大并发页数(逐页并行,受 LLM 端限流约束)
    llm_max_concurrency: int = 4
    # 单页瞬态失败(超时 / 429 / 5xx)重试次数
    llm_max_retries: int = 2

    # —— 向量引擎选择:mock | bge ——
    embed_backend: str = "mock"

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

# —— LLM 配置持久化(唯一来源,不使用环境变量)——
# 配置文件:.dc_data/llm_config.json,存放所有 LLM / OCR 相关字段。
import json as _json

_LLM_CONFIG_FIELDS = (
    "llm_api_base",
    "llm_api_key",
    "llm_model",
    "llm_timeout",
    "llm_max_concurrency",
    "llm_max_retries",
    "embed_backend",
)

# dataclass 字段默认值,供首次启动(无 json 文件)时写入种子配置。
_LLM_DEFAULTS: dict = {
    "llm_api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "llm_api_key": "",
    "llm_model": "qwen-vl-max",
    "llm_timeout": 120,
    "llm_max_concurrency": 4,
    "llm_max_retries": 2,
    "embed_backend": "mock",
}

def llm_config_path() -> Path:
    return Path(_env("DC_STORAGE_DIR", "./.dc_data")) / "llm_config.json"

def load_llm_overrides() -> dict:
    """从持久化文件读取 LLM 配置。

    首次启动(文件不存在)时,用内置默认值创建种子文件并返回。
    文件损坏时返回内置默认值。
    """
    path = llm_config_path()
    if not path.exists():
        return dict(_LLM_DEFAULTS)
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if k in _LLM_CONFIG_FIELDS}
    except Exception:
        return dict(_LLM_DEFAULTS)

def save_llm_overrides(overrides: dict) -> None:
    """写入 LLM 配置(合并已有值)。空字符串字段会被剔除,回退到默认。"""
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
    """把 llm_config.json 应用到运行时 settings 单例。启动时与保存后各调一次。"""
    cfg = load_llm_overrides()
    if "llm_api_base" in cfg:
        settings.llm_api_base = cfg["llm_api_base"]
    if "llm_api_key" in cfg:
        settings.llm_api_key = cfg["llm_api_key"]
    if "llm_model" in cfg:
        settings.llm_model = cfg["llm_model"]
    if "llm_timeout" in cfg:
        settings.llm_timeout = float(cfg["llm_timeout"])
    if "llm_max_concurrency" in cfg:
        settings.llm_max_concurrency = int(cfg["llm_max_concurrency"])
    if "llm_max_retries" in cfg:
        settings.llm_max_retries = int(cfg["llm_max_retries"])
    if "embed_backend" in cfg:
        settings.embed_backend = cfg["embed_backend"]

# 启动时应用一次持久化覆盖
apply_llm_overrides()
