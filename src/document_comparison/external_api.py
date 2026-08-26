"""外部合同比对接口的鉴权、结果文本与高亮图片产物。"""
from __future__ import annotations

import hmac
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import pymupdf
from fastapi import Header, HTTPException

from .config import settings
from .models import Diff, StatementSummaryReport, TamperReport
from .report import render_html_report, render_pdf_report
from .report.builder import burn_pdf

logger = logging.getLogger(__name__)

# 下载文件名用北京时间(UTC+8)展示完成时刻。
_BEIJING = ZoneInfo("Asia/Shanghai")


async def require_external_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """只保护 `/api/v1/external/*` 的独立 API Key 校验。

    API Key 为**可选项**:配置了 `external_api_key` 时按 Key 鉴权(缺失或不匹配 → 401);
    未配置(空)时跳过鉴权直接放行,便于内网/可信环境免鉴权接入。
    """
    if not external_config_enabled():
        raise HTTPException(503, "外部 API 未配置")
    expected = settings.external_api_key
    if expected and (x_api_key is None or not hmac.compare_digest(x_api_key, expected)):
        raise HTTPException(401, "invalid external API key")


def external_config_enabled() -> bool:
    """运行时外部 API 配置是否完整且可用。

    API Key 不在启用条件内——未配置 Key 时端点仍可用(跳过鉴权)。
    """
    try:
        validate_public_base_url(settings.external_public_base_url)
    except ValueError:
        return False
    return bool(
        settings.external_max_upload_mb > 0
        and settings.external_image_dpi > 0
    )


def validate_callback_url(value: str) -> str:
    """允许内外网 HTTP(S) 回调，但拒绝缺少 scheme/host 的地址。"""
    candidate = value.strip()
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("url 必须是有效的 HTTP/HTTPS 地址")
    if parsed.username or parsed.password:
        raise ValueError("url 不允许包含用户名或密码")
    return candidate


def validate_public_base_url(value: str) -> str:
    """校验生成回调资源链接所用的服务公开根地址。"""
    candidate = value.strip().rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("external_public_base_url 必须是有效的 HTTP/HTTPS 地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("external_public_base_url 不允许包含凭据、查询参数或片段")
    return candidate


def external_images_dir(task_id: str, *, side: str = "target") -> Path:
    """返回外部高亮图片目录；目标侧保持历史路径不变。"""
    root = settings.reports_dir / f"{task_id}_images"
    if side == "target":
        return root
    if side == "source":
        return root / "source"
    raise ValueError("side 必须为 source 或 target")


def external_image_path(task_id: str, page_number: int, *, side: str = "target") -> Path:
    return external_images_dir(task_id, side=side) / f"page-{page_number:04d}.png"


def external_html_report_path(task_id: str) -> Path:
    """外部比对 HTML 报告的落盘路径(on-disk 用 task_id 命名,安全且稳定)。"""
    return settings.reports_dir / f"{task_id}_external_report.html"


def external_pdf_report_path(task_id: str) -> Path:
    """外部比对 PDF 报告的落盘路径(on-disk 用 task_id 命名,安全且稳定)。"""
    return settings.reports_dir / f"{task_id}_external_report.pdf"


def safe_filename_stem(document_no: str) -> str:
    """把单据号清洗为文件名安全片段(去路径分隔符/控制字符,截断长度)。

    仅用于下载 ``Content-Disposition`` 的展示文件名;on-disk 路径仍用 task_id。
    """
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", document_no or "").strip(" .")
    return cleaned[:80] or "report"


def external_report_filename(
    document_no: str,
    finished_at: datetime | None,
    *,
    suffix: str = ".html",
) -> str:
    """生成下载文件名:【单据号】对比YYYYMMDD-HHMM{suffix}。

    ``finished_at`` 为 UTC 时间(来自 PG ``finished_at``);转北京时间到分钟。
    缺失时回退当前时刻。
    """
    moment = (finished_at or datetime.now(timezone.utc)).astimezone(_BEIJING)
    stamp = moment.strftime("%Y%m%d-%H%M")
    return f"【{safe_filename_stem(document_no)}】对比{stamp}{suffix}"


def write_external_html_report(
    task_id: str,
    document_no: str,
    report: TamperReport,
    finished_at: datetime | None = None,
) -> Path:
    """生成自包含 HTML 报告并落盘,返回写入路径。

    ``finished_at`` 用于头部「生成时间」(北京时间);缺失时用当前时刻。
    自动内嵌该任务已生成的原件/回收件高亮标注 PNG(base64 data URI),
    使下载的 HTML 离线即可查看差异标注,不依赖图片端点。
    生成失败只记日志,不抛(调用方在任务主流程中调用,不应因报告渲染失败而中断)。
    """
    import base64

    settings.ensure_dirs()
    generated_at = (finished_at or datetime.now(timezone.utc)).astimezone(_BEIJING)
    def _embedded_images(side: str) -> list[str]:
        images: list[str] = []
        images_dir = external_images_dir(task_id, side=side)
        if not images_dir.is_dir():
            return images
        for png in sorted(images_dir.glob("page-*.png")):
            try:
                data = png.read_bytes()
            except OSError:  # noqa: BLE001
                logger.warning("read highlight image failed task=%s side=%s page=%s", task_id, side, png.name)
                continue
            b64 = base64.b64encode(data).decode("ascii")
            images.append(f"data:image/png;base64,{b64}")
        return images

    highlight_images = _embedded_images("target")
    source_highlight_images = _embedded_images("source")
    try:
        html_text = render_html_report(
            document_no,
            report,
            generated_at,
            highlight_images=highlight_images,
            source_highlight_images=source_highlight_images,
        )
    except Exception:  # noqa: BLE001
        logger.exception("render external html report failed task=%s", task_id)
        raise
    path = external_html_report_path(task_id)
    path.write_text(html_text, encoding="utf-8")
    logger.info(
        "external html report written task=%s path=%s target_pages=%s source_pages=%s",
        task_id, path, len(highlight_images), len(source_highlight_images),
    )
    return path


def _sorted_highlight_pngs(task_id: str, side: str) -> list[Path]:
    """按页号顺序返回指定侧已生成的每页高亮标注 PNG 路径。"""
    images_dir = external_images_dir(task_id, side=side)
    if not images_dir.is_dir():
        return []
    return sorted(images_dir.glob("page-*.png"))


def write_external_pdf_report(
    task_id: str,
    document_no: str,
    report: TamperReport,
    finished_at: datetime | None = None,
) -> Path:
    """生成自包含 PDF 比对报告(概要+差异明细+逐页高亮图)并落盘,返回写入路径。

    与 ``write_external_html_report`` 同约定:``finished_at`` 用于头部「生成时间」
    (北京时间,缺失回退当前时刻);直接内嵌已生成的高亮标注 PNG,单文件离线可看。
    生成失败记日志后仍抛出,由调用方决定是否容忍(任务主流程中只记日志不中断)。
    """
    settings.ensure_dirs()
    generated_at = (finished_at or datetime.now(timezone.utc)).astimezone(_BEIJING)
    try:
        path = render_pdf_report(
            document_no,
            report,
            generated_at,
            output_path=external_pdf_report_path(task_id),
            highlight_images=_sorted_highlight_pngs(task_id, "target"),
            source_highlight_images=_sorted_highlight_pngs(task_id, "source"),
        )
    except Exception:  # noqa: BLE001
        logger.exception("render external pdf report failed task=%s", task_id)
        raise
    logger.info(
        "external pdf report written task=%s path=%s target_pages=%s source_pages=%s",
        task_id,
        path,
        len(_sorted_highlight_pngs(task_id, "target")),
        len(_sorted_highlight_pngs(task_id, "source")),
    )
    return path


def render_external_highlight_images(
    task_id: str,
    pdf_path: str | Path,
    report: TamperReport,
    source_pdf_path: str | Path | None = None,
) -> list[Path]:
    """生成回收件 PNG，并在原件 PDF/派生 PDF 可用时额外生成原件侧 PNG。

    返回值保持既有语义：仅返回回收件图片路径，避免破坏现有调用方。
    """
    settings.ensure_dirs()

    def _render_side(path: str | Path, side: str) -> list[Path]:
        annotated_pdf = settings.reports_dir / f"{task_id}_external_{side}_annotated.pdf"
        burn_pdf(path, report, annotated_pdf, side=side)
        output_dir = external_images_dir(task_id, side=side)
        output_dir.mkdir(parents=True, exist_ok=True)
        zoom = settings.external_image_dpi / 72.0
        matrix = pymupdf.Matrix(zoom, zoom)
        paths: list[Path] = []
        with pymupdf.open(str(annotated_pdf)) as doc:
            for page_index, page in enumerate(doc):
                output = external_image_path(task_id, page_index + 1, side=side)
                page.get_pixmap(matrix=matrix, alpha=False, annots=True).save(str(output))
                paths.append(output)
        return paths

    target_paths = _render_side(pdf_path, "target")
    if source_pdf_path is not None and report.source_annotation_status in {"available", "partial"}:
        try:
            _render_side(source_pdf_path, "source")
        except Exception:  # noqa: BLE001
            # 原件侧是对既有回收件产物的增强；不能因它失败而把已完成的比对改为失败。
            logger.exception("render source highlight images failed task=%s", task_id)
    return target_paths


def _diff_texts(diff: Diff) -> tuple[str, str]:
    original = "".join(
        segment.text for segment in diff.segments if segment.op in {"equal", "delete"}
    )
    recovered = "".join(
        segment.text for segment in diff.segments if segment.op in {"equal", "insert"}
    )
    return original, recovered


def build_result_text(document_no: str, report: TamperReport) -> str:
    """从确定性差异结构生成供外部系统核对的中文文本。"""
    conclusion = {
        "changed": "发现确认内容变化",
        "needs_review": "存在待人工复核内容",
        "clean": "未发现内容变化",
    }.get(report.change_status, report.change_status)
    recognition = {
        "reliable": "可靠",
        "needs_review": "待人工复核",
    }.get(report.recognition_status, report.recognition_status)
    location = {
        "complete": "完整",
        "partial": "部分缺失",
        "missing": "缺失",
    }.get(report.location_status, report.location_status)
    diffs = [*report.diffs, *report.unmatched_clauses]
    lines = [
        f"单据号：{document_no}",
        f"结论：{conclusion}",
        f"识别状态：{recognition}",
        f"高亮定位：{location}",
        f"差异数量：{len(diffs)}",
    ]
    status_names = {
        "modified": "修改",
        "added": "新增",
        "deleted": "删除",
        "identical": "一致",
    }
    for index, diff in enumerate(diffs, start=1):
        original, recovered = _diff_texts(diff)
        label = " ".join(value for value in (diff.number, diff.title) if value).strip()
        lines.extend(
            [
                f"[{index}] {label or diff.alignment_id}（{status_names.get(diff.status, diff.status)}）",
                f"原始合同：{original or '（无）'}",
                f"回收件：{recovered or '（无）'}",
            ]
        )
    return "\n".join(lines)


def _absolute_external_url(path: str) -> str:
    return f"{settings.external_public_base_url.rstrip('/')}{path}"


def build_external_result(
    task_id: str,
    document_no: str,
    report: TamperReport,
) -> dict:
    """构造查询响应与完成回调共享的稳定结果结构(扁平:key 全在顶层)。"""
    images: list[str] = []
    page_number = 1
    while external_image_path(task_id, page_number).is_file():
        images.append(
            _absolute_external_url(
                f"/api/v1/external/contractCompare/{task_id}/images/{page_number}"
            )
        )
        page_number += 1
    source_images: list[str] = []
    page_number = 1
    while external_image_path(task_id, page_number, side="source").is_file():
        source_images.append(
            _absolute_external_url(
                f"/api/v1/external/contractCompare/{task_id}/source-images/{page_number}"
            )
        )
        page_number += 1
    return {
        "change_status": report.change_status,
        "result_text": build_result_text(document_no, report),
        "highlight_images": images,
        "source_highlight_images": source_images,
        # result_url 指向自包含 PDF 报告下载(attachment),html_url 指向同内容
        # 的 HTML 版本;JSON 查询仍用 GET /api/v1/external/contractCompare/{task_id}。
        "result_url": _absolute_external_url(
            f"/api/v1/external/contractCompare/{task_id}/report.pdf"
        ),
        "html_url": _absolute_external_url(
            f"/api/v1/external/contractCompare/{task_id}/report.html"
        ),
    }


def build_external_statement_result(
    task_id: str,
    document_no: str,
    report: StatementSummaryReport,
    *,
    document_type: str | None = None,
) -> dict:
    """构造金额统计查询响应与完成回调共享的稳定结果结构。

    顶层放业务最关心的核心汇总字段(总金额/判定/每文件合计/计数),便于外部系统直接消费;
    与 `build_external_result` 风格一致:查询响应与回调 payload 共用同一结构。
    document_type:单据类型细分("1"=发票 | "2"=对帐单),供外部系统
    区分统计对象;未提供时为 None。
    """
    return {
        "document_no": document_no,
        "document_type": document_type,
        "grand_total": report.grand_total,
        "verdict": report.verdict,
        "file_totals": [
            {
                "file_index": file.file_index,
                "file_name": file.file_name,
                "total_amount": file.total_amount,
                "error": file.error,
            }
            for file in report.files
        ],
        "total_files": report.total_files,
        "total_tables": report.total_tables,
        "total_items": report.total_items,
        "reasons": list(report.reasons),
        "result_url": _absolute_external_url(f"/api/v1/external/amountStat/{task_id}"),
    }
