"""ORM 表定义(SQLAlchemy 2.0 Mapped 风格)。

五张表:
- `task_records`:每次比对/统计任务的轻量元数据 + 完整报告 JSONB。
- `task_events`:任务里程碑事件(start / 各阶段 done / done / failed),用于
  历史时间线查看;不入全量进度事件(高频写入),只入里程碑。
- `llm_config`:LLM/OCR 模型配置单行 JSONB(UI 设置页持久化,不再走文件)。
- `task_llm_calls`:对话型 LLM 调用的输入/输出及最终 OCR 解析结果记录
  (OCR/judge/llm-diff/statement-column/statement-amount/ocr-result,含失败/重试),
  供「对比记录」页面查看模型/OCR明细。embedding 不入。
- `external_api_calls`:外部接口(/api/v1/external/*)入站 HTTP 请求审计记录,
  覆盖提交/查询/图片请求以及 401 鉴权失败、422 校验失败等无 task_records 痕迹的调用。

报告 JSONB 三选一(report_compare / report_raw / report_statement),按任务类型写入;
查询端点和前端按 kind 判读。JSONB 同时支持后期按结构化字段查询。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .engine import Base


class TaskRecord(Base):
    """单次比对/统计任务记录(含完整报告 JSONB)。"""

    __tablename__ = "task_records"

    # task_id 与内存 TaskManager 的 task_id 一致(uuid4 hex 前 16 位)
    task_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # 任务类型:compare | raw | statement(决定走哪个报告字段)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    # 任务状态:pending | running | done | failed
    status: Mapped[str] = mapped_column(String(16), index=True)

    # 文件名(供记录页展示;不存文件内容,文件仍在 .dc_data/uploads)
    source_name: Mapped[str] = mapped_column(String(512), default="")
    # target 对帐单多文件用 list;compare/raw 单文件放在 list[0]
    target_names: Mapped[list[str]] = mapped_column(JSONB, default=list)
    ocr_backend: Mapped[str | None] = mapped_column(String(16), nullable=True, default=None)
    # 外部系统核对字段；同一单据号允许多次提交，因此索引不唯一。
    document_no: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True, default=None
    )
    # 单据类型(statement 金额统计任务的细分):"1"=发票 | "2"=对帐单(字符串枚举)。
    # 供「对比记录」标识与后续查询过滤;非 statement 任务为 None。
    document_type: Mapped[str | None] = mapped_column(
        String(16), nullable=True, default=None
    )
    external_request: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )

    # 回调交付记录。
    # callback_url 存在 → callback_status 初始为 "pending";交付后写 "success"/"failed"。
    callback_url: Mapped[str | None] = mapped_column(String(1024), nullable=True, default=None)
    callback_status: Mapped[str | None] = mapped_column(
        String(16), nullable=True, index=True, default=None
    )
    callback_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    callback_error: Mapped[str | None] = mapped_column(String(512), nullable=True, default=None)
    callback_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    # 首次回调交付的业务 payload(重新推送时 100% 还原原始内容)。
    # 仅含业务字段;envelope(event_id/task_id/status)由 webhook.build_event 现拼。
    callback_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, default=None
    )

    # 任务终结结果摘要(供列表页快速判定)
    overall_risk: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True, default=None)
    change_status: Mapped[str | None] = mapped_column(String(16), nullable=True, default=None)
    elapsed: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    stage_timings: Mapped[dict[str, float]] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(String(2048), nullable=True, default=None)

    # 时间戳(UTC,与 docker TZ 解耦;前端按需转本地时区)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    # 完整报告 JSONB(三选一,与 kind 对应)。JSONB 支持后期按字段查询。
    report_compare:   Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True, default=None)
    report_raw:       Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True, default=None)
    report_statement: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True, default=None)

    events: Mapped[list["TaskEvent"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="TaskEvent.id",
    )

    __table_args__ = (
        # 列表页常用筛选+排序:status / kind + created_at desc
        Index("ix_task_records_status_created", "status", "created_at"),
        Index("ix_task_records_kind_created", "kind", "created_at"),
    )


class TaskEvent(Base):
    """任务里程碑事件(仅关键节点,不入全量进度事件)。"""

    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("task_records.task_id", ondelete="CASCADE"), index=True
    )
    # stage 名(与 tasks.py push_event 的 stage 一致,如 word_done / ocr_done / done)
    stage: Mapped[str] = mapped_column(String(32), index=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    stage_timings: Mapped[dict[str, float]] = mapped_column(JSONB, default=dict)
    # 始终 True(本表只存里程碑);保留字段以便未来扩展为混合粒度
    milestone: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )

    task: Mapped[TaskRecord] = relationship(back_populates="events")

    __table_args__ = (
        # 单任务事件回放:按 task_id + id 升序
        Index("ix_task_events_task_id_id", "task_id", "id"),
    )


class LlmConfigRecord(Base):
    """LLM/OCR 模型配置(单行 JSONB,UI 设置页持久化)。

    设计为单行表(id 固定为 1),整份配置作为一个 JSONB blob 存储,镜像原
    llm_config.json 的语义。字段白名单由 `config._LLM_CONFIG_FIELDS` 把控,
    schema 不必枚举 23 个字段。原子 UPDATE 整份覆盖。
    """

    __tablename__ = "llm_config"

    # 固定单行:应用层 upsert 只写 id=1 这一条
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class TaskLlmCall(Base):
    """单次对话型 LLM HTTP 调用或最终 OCR 解析结果记录。

    由 observability 的模型调用拦截器或 record_ocr_result 经 contextvar 收集器产生,
    任务结束时批量入库。embedding 调用不入本表(文本→向量,非对话型)。

    payload 中的图片 base64 已在收集阶段由 _safe_model_value 脱敏为
    {data_url, base64_chars, sha256},避免几 MB base64 撑爆 JSONB。
    response 截断到 64KB;error 截断到 512 字符。
    """

    __tablename__ = "task_llm_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("task_records.task_id", ondelete="CASCADE"), index=True
    )
    # kind 与 observability._COLLECTED_KINDS 对齐:
    # ocr | ocr-whole | ocr-result | paddleocr | judge | alignment | llm-diff
    # | statement-column | statement-amount
    kind: Mapped[str] = mapped_column(String(32), index=True)
    # 第几次尝试(1-based;重试递增)。每次 attempt 各一行,便于看重试模式。
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # HTTP 状态码;连接级失败(超时/网络)为 None。
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    # 失败原因摘要(TransportError / HTTPStatusError / 4xx);成功为 None。
    error: Mapped[str | None] = mapped_column(String(512), nullable=True, default=None)
    # 请求体(图片已脱敏);非空。
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # 响应体(截断 64KB,超限时为带 truncated 标记的字符串);失败为 None。
    response: Mapped[Any | None] = mapped_column(JSONB, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )

    __table_args__ = (
        # 单任务调用明细:按 task_id + id 升序(调用发生顺序)
        Index("ix_task_llm_calls_task_id_id", "task_id", "id"),
    )


class ExternalApiCall(Base):
    """外部接口(`/api/v1/external/*`)单次入站 HTTP 请求审计记录。

    由 `api/app.py` 的审计中间件在每次 `/api/v1/external/` 前缀请求结束时写入,
    覆盖成功(202/200)与失败(400/401/404/413/422/429/500/503)全链路。
    与 `task_records` 解耦:`task_id` 可空且**不加外键**——401 鉴权失败、
    422 参数校验失败发生在任务创建之前,审计记录必须独立留存;任务删除时
    不级联清除审计记录(CASCADE 只在 task_records↔task_llm_calls 之间)。

    敏感数据处理:`api_key_sha256` 只存 X-API-Key 的 sha256 指纹,不存明文 key;
    `client_ip` 取 X-Forwarded-For[0] / X-Real-IP / client.host。
    """

    __tablename__ = "external_api_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 提交成功后关联 task_id;401/422/413/429 等任务创建前的失败为 None。
    task_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True, default=None
    )
    # 语义端点标签:contractCompare.submit | .result | .image
    endpoint: Mapped[str] = mapped_column(String(64), index=True)
    method: Mapped[str] = mapped_column(String(8))
    # 单据号(仅提交类端点经 request.state 传递;查询类为 None)
    document_no: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True, default=None
    )
    client_ip: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    # X-API-Key 的 sha256 十六进制指纹(64 字符),不存明文 key
    api_key_sha256: Mapped[str | None] = mapped_column(
        String(64), nullable=True, default=None
    )
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    # 失败摘要(401 → "invalid external API key";503 → "external api disabled" 等);成功为 None
    error: Mapped[str | None] = mapped_column(String(512), nullable=True, default=None)
    # 与响应体 JSON.request_id / 响应头 X-Request-Id 一致(12 位 hex)
    request_id: Mapped[str] = mapped_column(String(12), index=True)
    # 请求体字节数(排查 413);无 body 为 None
    content_length: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    # 请求参数快照(端点入口写入 request.state.audit_params):提交类为表单字段
    # (文件只记文件名,不记内容)+ callback_url/sync 等;图片类含 page_number。
    # 401 鉴权失败、422 FastAPI 校验失败(端点体未执行)由审计中间件预读 multipart
    # 非文件字段兜底(chunked 无 Content-Length 或 body 超上传上限时为 None)。
    request_params: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )

    __table_args__ = (
        # 全局审计列表常用筛选:按端点 + 时间倒序
        Index("ix_external_api_calls_endpoint_created", "endpoint", "created_at"),
    )
