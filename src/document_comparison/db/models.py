"""ORM 表定义(SQLAlchemy 2.0 Mapped 风格)。

四张表:
- `task_records`:每次比对/统计任务的轻量元数据 + 完整报告 JSONB。
- `task_events`:任务里程碑事件(start / 各阶段 done / done / failed),用于
  历史时间线查看;不入全量进度事件(高频写入),只入里程碑。
- `llm_config`:LLM/OCR 模型配置单行 JSONB(UI 设置页持久化,不再走文件)。
- `task_llm_calls`:对话型 LLM 调用的输入/输出记录(OCR/judge/llm-diff/statement-column,
  含失败/重试),供「对比记录」页面查看模型调用明细。embedding 不入。

报告 JSONB 三选一(report_compare / report_raw / report_statement),按任务类型写入;
查询端点和前端按 kind 判读。JSONB 同时支持后期按结构化字段查询。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
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
    """单次对话型 LLM HTTP 调用记录(含成功/失败/重试中间态)。

    由 observability.log_model_request/response/failure 经 contextvar 收集器产生,
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
    # ocr | ocr-whole | paddleocr | judge | llm-diff | statement-column
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
    # 响应体(截断 64KB);失败为 None。
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )

    __table_args__ = (
        # 单任务调用明细:按 task_id + id 升序(调用发生顺序)
        Index("ix_task_llm_calls_task_id_id", "task_id", "id"),
    )
