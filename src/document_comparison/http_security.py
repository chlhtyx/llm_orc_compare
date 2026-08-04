"""HTTP 层的低误伤扫描探测防护。

这里不试图替代网关/WAF：只拒绝本服务永远不会合法使用的请求目标，避免
漏洞扫描器探测敏感文件或其他产品的管理入口时进入应用路由和日志链路。
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote


_BLOCKED_METHODS = frozenset({"CONNECT", "TRACE"})
_SENSITIVE_SEGMENTS = frozenset(
    {
        ".aws",
        ".ds_store",
        ".env",
        ".git",
        ".hg",
        ".htaccess",
        ".htpasswd",
        ".ssh",
        ".svn",
        "authorized_keys",
        "composer.json",
        "composer.lock",
        "docker-compose.yaml",
        "docker-compose.yml",
        "id_rsa",
        "web.config",
    }
)
_SCANNER_PATH_SEGMENTS = frozenset(
    {
        "actuator",
        "cgi-bin",
        "phpmyadmin",
        "pma",
        "server-status",
        "vendor",
        "wp-admin",
        "wp-content",
        "wp-includes",
    }
)
_SCANNER_FILENAMES = frozenset({"phpinfo.php", "wp-login.php", "xmlrpc.php"})


def _decode_path(raw_path: str) -> str:
    """有限次解码，覆盖双重编码的路径穿越而避免无界处理。"""
    decoded = raw_path
    for _ in range(2):
        next_value = unquote(decoded, errors="replace")
        if next_value == decoded:
            break
        decoded = next_value
    return decoded.replace("\\", "/").lower()


def scan_probe_reason(*, method: str, raw_path: str) -> str | None:
    """返回可安全记录的探测类别；正常请求返回 ``None``。

    不返回原始路径，避免将攻击载荷写入日志。调用方应统一返回 404，以免帮助
    扫描器区分资源是否存在。
    """
    if method.upper() in _BLOCKED_METHODS:
        return "blocked_method"

    path = _decode_path(raw_path)
    if path.startswith("//"):
        return "absolute_path"
    if "\x00" in path or ".." in path.split("/"):
        return "path_traversal"

    segments = tuple(segment for segment in path.split("/") if segment)
    if any(segment in _SENSITIVE_SEGMENTS for segment in segments):
        return "sensitive_file"
    if any(segment in _SCANNER_PATH_SEGMENTS for segment in segments):
        return "scanner_endpoint"
    if segments and segments[-1] in _SCANNER_FILENAMES:
        return "scanner_endpoint"
    return None


def safe_static_file_path(static_dir: Path, requested_path: str) -> Path | None:
    """Resolve an SPA asset only when it remains below ``static_dir``.

    A request path beginning with a second slash can otherwise become an
    absolute ``Path`` when joined to the static directory.  Resolving before
    the containment check also prevents a symlink inside the static tree from
    exposing a file outside it.
    """
    root = static_dir.resolve()
    relative_path = requested_path.replace("\\", "/").lstrip("/")
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def apply_security_headers(headers) -> None:
    """添加与既有 SPA 兼容的基础响应安全头。"""
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Referrer-Policy", "same-origin")
    headers.setdefault(
        "Permissions-Policy", "camera=(), geolocation=(), microphone=()"
    )
