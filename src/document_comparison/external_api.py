"""外部合同比对接口的鉴权、结果文本与高亮图片产物。"""
from __future__ import annotations

import hmac
from pathlib import Path
from urllib.parse import urlsplit

import pymupdf
from fastapi import Header, HTTPException

from .config import settings
from .models import Diff, TamperReport
from .report.builder import burn_pdf


async def require_external_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """只保护 `/api/v1/external/*` 的独立 API Key 校验。"""
    expected = settings.external_api_key
    if not external_config_enabled():
        raise HTTPException(503, "外部 API 未配置")
    if x_api_key is None or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(401, "invalid external API key")


def external_config_enabled() -> bool:
    """运行时外部 API 配置是否完整且可用。"""
    try:
        validate_public_base_url(settings.external_public_base_url)
    except ValueError:
        return False
    return bool(
        settings.external_api_key
        and settings.external_max_upload_mb > 0
        and settings.external_image_dpi > 0
    )


def validate_callback_url(value: str) -> str:
    """允许内外网 HTTP(S) 回调，但拒绝缺少 scheme/host 的地址。"""
    candidate = value.strip()
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("callback_url 必须是有效的 HTTP/HTTPS 地址")
    if parsed.username or parsed.password:
        raise ValueError("callback_url 不允许包含用户名或密码")
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


def external_images_dir(task_id: str) -> Path:
    return settings.reports_dir / f"{task_id}_images"


def external_image_path(task_id: str, page_number: int) -> Path:
    return external_images_dir(task_id) / f"page-{page_number:04d}.png"


def _highlighted_page_indexes(report: TamperReport) -> set[int]:
    highlighted: set[int] = set()
    targets = [
        *report.diffs,
        *(
            diff
            for diff in report.unmatched_clauses
            if diff.status == "added" and diff.page_regions
        ),
    ]
    for diff in targets:
        highlighted.update(region.page_index for region in diff.page_regions)
    return highlighted


def render_external_highlight_images(
    task_id: str,
    pdf_path: str | Path,
    report: TamperReport,
) -> list[Path]:
    """烧录现有标注后，将回收件所有页面渲染为 PNG。"""
    settings.ensure_dirs()
    annotated_pdf = settings.reports_dir / f"{task_id}_external_annotated.pdf"
    burn_pdf(pdf_path, report, annotated_pdf)

    output_dir = external_images_dir(task_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    zoom = settings.external_image_dpi / 72.0
    matrix = pymupdf.Matrix(zoom, zoom)
    paths: list[Path] = []
    with pymupdf.open(str(annotated_pdf)) as doc:
        for page_index, page in enumerate(doc):
            output = external_image_path(task_id, page_index + 1)
            page.get_pixmap(matrix=matrix, alpha=False, annots=True).save(str(output))
            paths.append(output)
    return paths


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
    """构造查询响应与完成回调共享的稳定结果结构。"""
    highlighted = _highlighted_page_indexes(report)
    images = []
    page_number = 1
    while external_image_path(task_id, page_number).is_file():
        images.append(
            {
                "page_number": page_number,
                "has_highlight": page_number - 1 in highlighted,
                "url": _absolute_external_url(
                    f"/api/v1/external/compare/{task_id}/images/{page_number}"
                ),
            }
        )
        page_number += 1
    return {
        "change_status": report.change_status,
        "recognition_status": report.recognition_status,
        "location_status": report.location_status,
        "summary": report.summary,
        "result_text": build_result_text(document_no, report),
        "highlight_images": images,
        "result_url": _absolute_external_url(f"/api/v1/external/compare/{task_id}"),
    }
