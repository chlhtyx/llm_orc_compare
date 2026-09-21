"""文件存储(本地文件系统,§11.2)。

职责仅限于**上传的原始文件**(source docx / target pdf)落盘与查找;
上传件按 task_id 归集到 uploads/{task_id}/ 子目录(旧平铺命名仍可读取,不迁移)。
比对报告 JSON 不再写文件,统一持久化到 Postgres(见 db/repository.py)。
生产可替换为对象存储。
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx
from fastapi import UploadFile

from .config import settings

logger = logging.getLogger(__name__)


def save_upload(upload: UploadFile, task_id: str, role: str, index: int | None = None) -> Path:
    """保存上传文件。role ∈ {source, target}。归集到 uploads/{task_id}/ 子目录。

    index 非 None 时(对帐单多文件场景),命名为 f"{role}-{index}{suffix}",
    避免同 task 多 PDF 互相覆盖。index 为 None 时命名 f"{role}{suffix}"(单文件)。
    目录名承载 task_id,文件名不再重复前缀。
    """
    settings.ensure_dirs()
    suffix = Path(upload.filename or "").suffix or ".bin"
    task_dir = _task_upload_dir(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    if index is None:
        path = task_dir / f"{role}{suffix}"
    else:
        path = task_dir / f"{role}-{index}{suffix}"
    with path.open("wb") as f:
        f.write(upload.file.read())
    return path


async def download_to_upload(
    url: str, *, max_bytes: int, role: str
) -> tuple[Path, str]:
    """从 URL 流式下载到本地临时文件,供 URL 提交模式(source_url/target_url)使用。

    返回 (临时 Path, 推断文件名)。调用方负责后续 `finalize_temp_file` 落定或清理临时文件。

    - scheme 仅 http/https;拒绝 userinfo(与 callback_url 一致)。
    - 流式累计字节数,**超 `max_bytes` 立即中断并删除半成品**(与文件上传大小口径一致,
      复用 `external_max_upload_mb`)。
    - 非 2xx 响应 → ValueError,错误信息只带状态码 + hostname,**不回显完整 URL**(URL 可能含 token)。
    - 文件名优先 Content-Disposition: filename= → URL path 末段 → role 兜底。
    """
    candidate = url.strip()
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("source_url/target_url 必须是有效的 HTTP/HTTPS 地址")
    if parsed.username or parsed.password:
        raise ValueError("source_url/target_url 不允许包含用户名或密码")

    settings.ensure_dirs()
    suffix = _suffix_from_url_path(parsed.path)
    temp_path = settings.uploads_dir / f".pending-{role}-{uuid.uuid4().hex}{suffix}"

    written = 0
    try:
        async with httpx.AsyncClient(
            timeout=settings.download_timeout_seconds,
            follow_redirects=True,
            max_redirects=settings.download_max_redirects,
        ) as client:
            async with client.stream("GET", candidate) as response:
                if response.status_code >= 400:
                    raise ValueError(
                        f"下载 {role} 失败:HTTP {response.status_code} ({parsed.hostname})"
                    )
                filename = _filename_from_response(
                    response.headers.get("content-disposition"), parsed.path
                ) or f"{role}{suffix or '.bin'}"
                with temp_path.open("wb") as f:
                    async for chunk in response.aiter_bytes():
                        written += len(chunk)
                        if written > max_bytes:
                            raise ValueError(
                                f"{role} 超过 {max_bytes // (1024 * 1024)} MiB 下载上限"
                            )
                        f.write(chunk)
        if written == 0:
            raise ValueError(f"下载 {role} 失败:响应体为空 ({parsed.hostname})")
    except httpx.TimeoutException as exc:
        _safe_unlink(temp_path)
        raise ValueError(f"下载 {role} 超时 ({parsed.hostname})") from exc
    except httpx.HTTPError as exc:
        _safe_unlink(temp_path)
        raise ValueError(f"下载 {role} 失败:无法连接 ({parsed.hostname})") from exc
    except Exception:
        _safe_unlink(temp_path)
        raise
    return temp_path, filename


def finalize_temp_file(
    temp_path: Path, task_id: str, role: str, filename: str,
    index: int | None = None,
) -> Path:
    """把 `download_to_upload` 的临时产物 rename 到与 `save_upload` 一致的最终位置。

    命名规则与 `save_upload` 对齐:uploads/{task_id}/ 下,index=None 时
    `{role}{suffix}`(单文件分支),index 非 None 时 `{role}-{index}{suffix}`
    (对帐单多文件场景,避免互相覆盖)。同文件系统 rename 原子安全;
    幂等性由 task_id 唯一性保证。
    """
    suffix = Path(filename).suffix or ".bin"
    task_dir = _task_upload_dir(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    if index is None:
        final_path = task_dir / f"{role}{suffix}"
    else:
        final_path = task_dir / f"{role}-{index}{suffix}"
    temp_path.replace(final_path)
    return final_path


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("failed to clean up temp file %s", path)


def _suffix_from_url_path(path: str) -> str:
    """从 URL path 推断扩展名(含点),无则返回空串。"""
    last = path.rsplit("/", 1)[-1]
    if "." in last:
        return "." + last.rsplit(".", 1)[-1].lower()
    return ""


def _filename_from_response(content_disposition: str | None, url_path: str) -> str | None:
    """优先取 Content-Disposition 的 filename*,其次 filename,再回退 URL path 末段。"""
    if content_disposition:
        lower = content_disposition.lower()
        if "filename*=" in lower:
            raw = content_disposition.split("filename*=", 1)[1].split(";", 1)[0].strip()
            # RFC 5987: charset'lang'value
            if "'" in raw:
                raw = raw.split("'", 2)[-1]
            name = unquote(raw.strip().strip('"'))
            if name:
                return name
        if "filename=" in lower:
            raw = content_disposition.split("filename=", 1)[1].split(";", 1)[0].strip()
            name = unquote(raw.strip().strip('"'))
            if name:
                return name
    last = unquote(url_path.rsplit("/", 1)[-1])
    return last or None


def _task_upload_dir(task_id: str) -> Path:
    """任务上传文件的归集目录:uploads/{task_id}/。"""
    return settings.uploads_dir / task_id


def upload_path(task_id: str, role: str, index: int | None = None) -> Path | None:
    """查找已上传文件路径。role ∈ {source, target}。

    优先在归集目录 uploads/{task_id}/ 内匹配(单文件 `{role}{suffix}` 优先于
    对帐单多文件的 `{role}-{index}{suffix}`);目录不存在或未命中时,回退扫描
    uploads/ 平铺的旧命名 `{task_id}-{role}*`,历史任务数据无需迁移。
    index 非 None 时仅匹配该序号的原始附件,不会回退到其它序号。
    返回匹配的第一个文件,不存在时返回 None。
    """
    task_dir = _task_upload_dir(task_id)
    if index is not None:
        if index < 0:
            return None
        for directory, prefix in (
            (task_dir, f"{role}-{index}."),
            (settings.uploads_dir, f"{task_id}-{role}-{index}."),
        ):
            if directory.is_dir():
                for path in sorted(directory.iterdir()):
                    if path.is_file() and path.name.startswith(prefix):
                        return path
        return None
    if task_dir.is_dir():
        files = [f for f in task_dir.iterdir() if f.is_file()]
        for f in sorted(files):
            if f.name.startswith(f"{role}."):
                return f
        for f in sorted(files):
            if f.name.startswith(f"{role}-"):
                return f
    for f in settings.uploads_dir.iterdir():
        if f.is_file() and f.name.startswith(f"{task_id}-{role}"):
            return f
    return None


def compared_pdf_path(task_id: str) -> Path:
    """返回任务截断后实际参与比对的 PDF 固定产物路径。"""
    return settings.reports_dir / f"{task_id}_compared.pdf"


def rendered_source_pdf_path(task_id: str) -> Path:
    """返回 DOCX 原件为标注而生成的派生 PDF 固定路径。

    该文件不是上传原件，也不参与文本比对；仅供原件侧坐标、高亮和预览使用。
    """
    return settings.reports_dir / f"{task_id}_source_rendered.pdf"


def effective_target_path(task_id: str) -> Path | None:
    """返回实际比对的回收件；未截取时回退到上传的原始 PDF。

    页数截取发生时，OCR、报告和高亮必须使用同一份物理 PDF，不能再回读
    上传的全页回收件，否则预览或导出产物会重新出现被排除的页面。
    """
    compared = compared_pdf_path(task_id)
    if compared.is_file():
        return compared
    return upload_path(task_id, "target")
