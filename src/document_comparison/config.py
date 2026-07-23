"""运行时配置。

基础设施(服务、认证、存储)从环境变量读取;
LLM / OCR 相关配置统一持久化于 Postgres(llm_config 表),不使用环境变量。
首次启动会从旧 llm_config.json 文件一次性导入并保留文件作为备份,之后不再读取。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

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

    # —— OCR 引擎默认选择:llm | paddleocr ——
    # 不再持久化、不在设置页暴露。仅作为代码兜底默认值:
    # 既有比对 API 未传 ocr_backend 时的兜底。
    ocr_backend: str = "llm"

    # —— 远端多模态推理(llm 引擎用:通用 VL 模型)——
    # 兼容 OpenAI Chat Completions 协议(base_url 指向 vLLM / SGLang / 云端 API)。
    # 适配能返回结构化 JSON 的通用对话 VL 模型(Qwen-VL-Max / Qwen3-VL / GPT-4o 等)。
    llm_api_base: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    # 请求超时(秒)。单页识别可能较慢,默认 120s(仅作用于读取;连接超时固定 10s)。
    llm_timeout: float = 120.0
    # 最大并发页数(逐页并行,受 LLM 端限流约束)
    llm_max_concurrency: int = 4
    # 单页瞬态失败(超时 / 429 / 5xx)重试次数
    llm_max_retries: int = 2

    # —— 远端多模态推理(paddleocr 引擎用:专用 OCR 模型)——
    # 与 llm_* 完全独立配置。api_mode=vllm 走 OpenAI 兼容
    # /chat/completions;api_mode=official_sdk 走 PaddleOCR 官方 Python SDK。
    # 两种方式各自保留配置,切换时不覆盖另一套凭据。
    paddleocr_api_mode: str = "vllm"
    # vLLM / OpenAI 兼容方式(原有配置)
    paddleocr_api_base: str = ""
    paddleocr_api_key: str = ""
    paddleocr_model: str = ""
    # PaddleOCR 官方 Python SDK / AI Studio 托管 API
    paddleocr_official_api_base: str = ""
    paddleocr_official_access_token: str = ""
    paddleocr_official_model: str = "PaddleOCR-VL-1.6"
    paddleocr_timeout: float = 300.0
    paddleocr_max_concurrency: int = 4
    paddleocr_max_retries: int = 2

    # —— LLM 辅助说明服务(对 modified 条款补充严重度建议)——
    # 与 OCR 服务独立配置:OCR 需多模态 VL 模型(看图),说明需纯文本 LLM。
    # 走 OpenAI 兼容 Chat Completions 协议。未配置时复核自动回退规则判定。
    judge_api_base: str = ""
    judge_api_key: str = ""
    judge_model: str = ""
    # 无标注版整篇比对是重推理(超长输入+结构化输出),需要比 OCR 单页更长的超时。
    judge_timeout: float = 300.0

    # —— 向量引擎选择:mock | qwen | bge ——
    # 默认 qwen;若未配置 embed_api_base/model,get_embed_engine 会自动回退 mock。
    embed_backend: str = "qwen"

    # —— 远端向量服务(qwen backend 用;vLLM / SGLang 自建 Qwen3-Embedding 等)——
    # 与 OCR 服务独立配置(两者通常不在同一推理服务)。
    embed_api_base: str = ""
    embed_api_key: str = ""
    embed_model: str = ""
    embed_timeout: float = 60.0

    # —— 相似度配置 ——
    # identical 保留供旧调用方兼容，零容忍裁决不再用相似度产生 clean。
    # modified 仅用于非关键变化的严重度排序，不决定是否变化。
    similarity_identical: float = 0.98
    similarity_modified: float = 0.85
    align_similarity: float = 0.85  # 对齐配对确认阈值(语义向量后端)
    # mock 字符袋相似度系统性偏低(中文同义改写打不到 0.85),单独给一个较低阈值。
    align_similarity_mock: float = 0.70

    # —— PDF 渲染 ——
    # 200 是 OCR 速度/质量的甜点(实测较 300 提速约 27%、token 减半,质量无损);
    # 过低(如 100)会让密集文字识别变差甚至更慢。
    pdf_render_dpi: int = 200

    # —— PDF 页数上限 ——
    # 提交时若扫描件 PDF 页数超过此值,直接拒绝(返回 400「暂不支持」),不进入流水线。
    # 0 表示不限制。OCR 单页成本较高,长文档易拖垮队列,故设置一个软上限。
    max_pdf_pages: int = 0

    # —— 任务 ——
    # 全局并发上限。多 worker 部署下由 PG task_records 计数强制(见 api/app.py),
    # 单进程下另有 asyncio.Semaphore 第二道保险(见 tasks.py)。
    max_concurrent_tasks: int = field(
        default_factory=lambda: int(_env("DC_MAX_CONCURRENT_TASKS", "4"))
    )

    # —— Webhook ——
    webhook_max_retries: int = 3
    webhook_timeout_seconds: float = 10.0

    # —— 外部合同比对 API ——
    # 两项均配置后 `/api/v1/external/*` 才可用；缺失时端点返回 503，
    # 不影响内部页面和既有 API。
    external_api_key: str = field(
        default_factory=lambda: _env("DC_EXTERNAL_API_KEY", "")
    )
    external_public_base_url: str = field(
        default_factory=lambda: _env("DC_EXTERNAL_PUBLIC_BASE_URL", "")
    )
    external_max_upload_mb: int = field(
        default_factory=lambda: int(_env("DC_EXTERNAL_MAX_UPLOAD_MB", "50"))
    )
    external_image_dpi: int = field(
        default_factory=lambda: int(_env("DC_EXTERNAL_IMAGE_DPI", "144"))
    )
    # 外部正式调用和管理端管线测试共用同一组默认比对选项。
    external_ocr_backend: str = field(
        default_factory=lambda: _env("DC_EXTERNAL_OCR_BACKEND", "paddleocr")
    )
    external_enable_llm_judge: bool = field(
        default_factory=lambda: _env(
            "DC_EXTERNAL_ENABLE_LLM_JUDGE", "0"
        ).lower() in ("1", "true", "yes")
    )
    external_enable_risk_assessment: bool = field(
        default_factory=lambda: _env(
            "DC_EXTERNAL_ENABLE_RISK_ASSESSMENT", "0"
        ).lower() in ("1", "true", "yes")
    )
    # 外部接口默认:回收件页数截取(回收 PDF 超过原始合同时,截取到原始页数再比对)。
    external_truncate_to_original_pages: bool = field(
        default_factory=lambda: _env(
            "DC_EXTERNAL_TRUNCATE_TO_ORIGINAL_PAGES", "0"
        ).lower() in ("1", "true", "yes")
    )

    # —— Postgres(SQLAlchemy 引擎;硬依赖,未配置时启动失败)——
    # 连接串示例:postgresql+psycopg://dc:dcpass@localhost:5432/doc_compare
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", ""))
    # 启动时自动跑 alembic upgrade head(默认开启,保证 docker compose up 即用)。
    # 设为 0 可关闭,改由 CI/运维手动 `alembic upgrade head`。
    db_auto_migrate: bool = field(
        default_factory=lambda: _env("DC_DB_AUTO_MIGRATE", "1").lower() not in ("0", "false", "no")
    )
    # 是否启动时直接 CREATE TABLE IF NOT EXISTS(不走 alembic;仅测试场景用,
    # 与 db_auto_migrate 二选一,此开关优先级更高)。
    db_auto_create: bool = field(
        default_factory=lambda: _env("DC_DB_AUTO_CREATE", "").lower() in ("1", "true", "yes")
    )
    # 连接池参数。注意:每个 worker 独立持有一份连接池(N workers × (pool+overflow)),
    # 启用多 worker 时需确认 PG max_connections 足够。
    db_pool_size: int = field(
        default_factory=lambda: int(_env("DC_DB_POOL_SIZE", "5"))
    )
    db_max_overflow: int = field(
        default_factory=lambda: int(_env("DC_DB_MAX_OVERFLOW", "10"))
    )
    db_pool_timeout: float = field(
        default_factory=lambda: float(_env("DC_DB_POOL_TIMEOUT", "30.0"))
    )

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

# —— LLM 配置持久化(Postgres llm_config 表;旧 llm_config.json 仅供一次性导入)——
# 启动顺序:db 引擎由 api.app.create_app() 初始化,之后在那里显式调用
# _maybe_import_legacy_llm_config_file() + apply_llm_overrides()。
# 本模块 import 时不读 PG(此时引擎尚未初始化),避免在 settings 创建期触发 DB 访问。
import json as _json

_LLM_CONFIG_FIELDS = (
    "llm_api_base",
    "llm_api_key",
    "llm_model",
    "llm_timeout",
    "llm_max_concurrency",
    "llm_max_retries",
    "paddleocr_api_mode",
    "paddleocr_api_base",
    "paddleocr_api_key",
    "paddleocr_model",
    "paddleocr_official_api_base",
    "paddleocr_official_access_token",
    "paddleocr_official_model",
    "paddleocr_timeout",
    "paddleocr_max_concurrency",
    "paddleocr_max_retries",
    "judge_api_base",
    "judge_api_key",
    "judge_model",
    "judge_timeout",
    "embed_backend",
    "embed_api_base",
    "embed_api_key",
    "embed_model",
    "embed_timeout",
    "pdf_render_dpi",
    "max_pdf_pages",
    "external_api_key",
    "external_public_base_url",
    "external_max_upload_mb",
    "external_image_dpi",
    "external_ocr_backend",
    "external_enable_llm_judge",
    "external_enable_risk_assessment",
    "external_truncate_to_original_pages",
)

# dataclass 字段默认值,供 PG 无记录时合并使用(不再写入种子配置)。
_LLM_DEFAULTS: dict = {
    "llm_api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "llm_api_key": "",
    "llm_model": "qwen-vl-max",
    "llm_timeout": 120,
    "llm_max_concurrency": 4,
    "llm_max_retries": 2,
    "paddleocr_api_mode": "vllm",
    "paddleocr_api_base": "",
    "paddleocr_api_key": "",
    "paddleocr_model": "",
    "paddleocr_official_api_base": "",
    "paddleocr_official_access_token": "",
    "paddleocr_official_model": "PaddleOCR-VL-1.6",
    "paddleocr_timeout": 300,
    "paddleocr_max_concurrency": 4,
    "paddleocr_max_retries": 2,
    "judge_api_base": "",
    "judge_api_key": "",
    "judge_model": "",
    "judge_timeout": 300,
    "embed_backend": "qwen",
    "embed_api_base": "",
    "embed_api_key": "",
    "embed_model": "",
    "embed_timeout": 60,
    "pdf_render_dpi": 200,
    "max_pdf_pages": 0,
    # 服务管理端可持久化覆盖；环境变量仍作为首次启动/灾备默认值。
    "external_api_key": settings.external_api_key,
    "external_public_base_url": settings.external_public_base_url,
    "external_max_upload_mb": settings.external_max_upload_mb,
    "external_image_dpi": settings.external_image_dpi,
    "external_ocr_backend": settings.external_ocr_backend,
    "external_enable_llm_judge": settings.external_enable_llm_judge,
    "external_enable_risk_assessment": settings.external_enable_risk_assessment,
    "external_truncate_to_original_pages": settings.external_truncate_to_original_pages,
}

def _legacy_llm_config_path() -> Path:
    """旧文件路径(仅供首次启动一次性导入使用,代码不再读写此文件)。"""
    return Path(_env("DC_STORAGE_DIR", "./.dc_data")) / "llm_config.json"

def load_llm_overrides() -> dict:
    """从 Postgres llm_config 表读取已持久化的 LLM 配置。

    无记录或 DB 未就位时返回内置默认值字典的拷贝,保证调用方总能拿到完整字段。
    PG 读取异常(引擎未初始化 / 连接失败)只记日志并回退默认值,不抛错。
    """
    try:
        from .db import repository as _repo  # 函数内懒导入,避免循环依赖
        persisted = _repo.get_llm_config()
    except Exception as exc:  # noqa: BLE001
        logger.warning("load_llm_overrides: read from PG failed, fallback to defaults: %s", exc)
        return dict(_LLM_DEFAULTS)
    # 与默认值合并,缺失字段回退到默认,保证下游 apply 字段齐全。
    merged = dict(_LLM_DEFAULTS)
    for k in _LLM_CONFIG_FIELDS:
        if k in persisted:
            merged[k] = persisted[k]
    return merged

def save_llm_overrides(overrides: dict) -> None:
    """写入 LLM 配置(合并已有 PG 记录)。空字符串/None 字段会被剔除,回退到默认。

    白名单过滤在此完成;repository.save_llm_config 只做 upsert。
    """
    merged = load_llm_overrides()
    for k in _LLM_CONFIG_FIELDS:
        if k in overrides:
            v = overrides[k]
            if v is None or v == "":
                merged.pop(k, None)
            else:
                merged[k] = v
    from .db import repository as _repo  # 懒导入
    _repo.save_llm_config(merged)
    apply_llm_overrides()

def apply_llm_overrides() -> None:
    """把 PG llm_config 应用到运行时 settings 单例。

    由 api.app.create_app() 在引擎初始化 + schema 就位后调用一次;
    save_llm_overrides 写入后也会调用一次。模块 import 期不再自动调用。
    """
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
    if cfg.get("paddleocr_api_mode") in {"vllm", "official_sdk"}:
        settings.paddleocr_api_mode = str(cfg["paddleocr_api_mode"])
    if "paddleocr_api_base" in cfg:
        settings.paddleocr_api_base = cfg["paddleocr_api_base"]
    if "paddleocr_api_key" in cfg:
        settings.paddleocr_api_key = cfg["paddleocr_api_key"]
    if "paddleocr_model" in cfg:
        settings.paddleocr_model = cfg["paddleocr_model"]
    if "paddleocr_official_api_base" in cfg:
        settings.paddleocr_official_api_base = cfg["paddleocr_official_api_base"]
    if "paddleocr_official_access_token" in cfg:
        settings.paddleocr_official_access_token = cfg[
            "paddleocr_official_access_token"
        ]
    if "paddleocr_official_model" in cfg:
        settings.paddleocr_official_model = cfg["paddleocr_official_model"]
    if "paddleocr_timeout" in cfg:
        settings.paddleocr_timeout = float(cfg["paddleocr_timeout"])
    if "paddleocr_max_concurrency" in cfg:
        settings.paddleocr_max_concurrency = int(cfg["paddleocr_max_concurrency"])
    if "paddleocr_max_retries" in cfg:
        settings.paddleocr_max_retries = int(cfg["paddleocr_max_retries"])
    if "judge_api_base" in cfg:
        settings.judge_api_base = cfg["judge_api_base"]
    if "judge_api_key" in cfg:
        settings.judge_api_key = cfg["judge_api_key"]
    if "judge_model" in cfg:
        settings.judge_model = cfg["judge_model"]
    if "judge_timeout" in cfg:
        settings.judge_timeout = float(cfg["judge_timeout"])
    if "embed_backend" in cfg:
        settings.embed_backend = cfg["embed_backend"]
    if "embed_api_base" in cfg:
        settings.embed_api_base = cfg["embed_api_base"]
    if "embed_api_key" in cfg:
        settings.embed_api_key = cfg["embed_api_key"]
    if "embed_model" in cfg:
        settings.embed_model = cfg["embed_model"]
    if "embed_timeout" in cfg:
        settings.embed_timeout = float(cfg["embed_timeout"])
    if "pdf_render_dpi" in cfg:
        try:
            settings.pdf_render_dpi = int(cfg["pdf_render_dpi"])
        except (TypeError, ValueError):
            pass
    if "max_pdf_pages" in cfg:
        try:
            settings.max_pdf_pages = int(cfg["max_pdf_pages"])
        except (TypeError, ValueError):
            pass
    if "external_api_key" in cfg:
        settings.external_api_key = str(cfg["external_api_key"])
    if "external_public_base_url" in cfg:
        settings.external_public_base_url = str(cfg["external_public_base_url"])
    if "external_max_upload_mb" in cfg:
        try:
            settings.external_max_upload_mb = int(cfg["external_max_upload_mb"])
        except (TypeError, ValueError):
            pass
    if "external_image_dpi" in cfg:
        try:
            settings.external_image_dpi = int(cfg["external_image_dpi"])
        except (TypeError, ValueError):
            pass
    if cfg.get("external_ocr_backend") in {"llm", "paddleocr"}:
        settings.external_ocr_backend = str(cfg["external_ocr_backend"])
    if "external_enable_llm_judge" in cfg:
        settings.external_enable_llm_judge = bool(cfg["external_enable_llm_judge"])
    if "external_enable_risk_assessment" in cfg:
        settings.external_enable_risk_assessment = bool(
            cfg["external_enable_risk_assessment"]
        )
    if "external_truncate_to_original_pages" in cfg:
        settings.external_truncate_to_original_pages = bool(
            cfg["external_truncate_to_original_pages"]
        )


def _maybe_import_legacy_llm_config_file() -> None:
    """一次性数据迁移:PG 表空 且 本地旧 llm_config.json 存在时,把文件内容导入 PG。

    幂等:PG 已有记录则直接返回,不再读文件。文件导入失败只记 warning,不抛、
    不影响启动。文件保留不删,作为人工可恢复的备份;之后代码不再读取它。
    """
    try:
        from .db import repository as _repo
        existing = _repo.get_llm_config()
    except Exception as exc:  # noqa: BLE001
        logger.warning("legacy llm_config import skipped: cannot read PG: %s", exc)
        return

    if existing:
        # PG 已有配置(可能来自更早迁移),尊重现状,不动文件。
        return

    path = _legacy_llm_config_path()
    if not path.exists():
        logger.info("legacy llm_config.json not found; starting fresh with defaults")
        return

    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "legacy llm_config.json at %s is unreadable, skipped import: %s", path, exc,
        )
        return

    # 仅导入白名单字段且非空值(空值回退默认,不必存)
    imported: dict = {}
    for k in _LLM_CONFIG_FIELDS:
        v = data.get(k)
        if v is not None and v != "":
            imported[k] = v
    if not imported:
        logger.info("legacy llm_config.json at %s had no usable fields, skipped import", path)
        return

    try:
        _repo.save_llm_config(imported)
        logger.info(
            "imported %d llm config field(s) from legacy %s into Postgres; "
            "file kept as backup (no longer read)",
            len(imported), path,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("legacy llm_config import failed to write PG: %s", exc)
