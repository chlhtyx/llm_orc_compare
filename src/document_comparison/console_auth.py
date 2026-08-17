"""Web 控制台访问口令与会话令牌。

保护对象是管理台业务接口(任务、报告、模型配置等);外部接口
`/api/v1/external/*` 与 `/api/v1/compare/api-test` 走各自的 X-API-Key,不经此模块。

会话是无状态 HMAC 签名令牌(签发时间戳 + 签名),多 worker 部署下任意进程都能
独立校验,不依赖共享存储;修改口令即更换派生密钥,所有已发会话立即失效。
登录失败限速为进程内滑动窗口(per-worker),足以挡单机爆破;分布式限速应放网关。
"""
from __future__ import annotations

import hashlib
import hmac
import threading
import time
from collections import defaultdict, deque

from .config import settings

COOKIE_NAME = "dc_console_session"

# 登录失败限速:同一来源 IP 在窗口内失败达到阈值后临时拒绝(429)。
_LOGIN_MAX_FAILURES = 10
_LOGIN_WINDOW_SECONDS = 300.0
_failed_logins: dict[str, deque[float]] = defaultdict(deque)
_failed_logins_lock = threading.Lock()


def console_auth_enabled() -> bool:
    """控制台口令是否启用(DC_CONSOLE_PASSWORD 非空)。"""
    return bool(settings.console_password)


def verify_console_password(password: str) -> bool:
    """恒时比较登录口令;未配置口令时恒为 False(调用方应先查 console_auth_enabled)。"""
    if not settings.console_password:
        return False
    return hmac.compare_digest(password, settings.console_password)


def _session_secret() -> bytes:
    """由口令派生会话签名密钥;换口令即所有已发会话失效。"""
    return hashlib.sha256(
        f"dc-console-session-v1:{settings.console_password}".encode("utf-8")
    ).digest()


def _sign(issued_hex: str) -> str:
    return hmac.new(_session_secret(), issued_hex.encode("ascii"), hashlib.sha256).hexdigest()


def issue_session_token(now: float | None = None) -> str:
    """签发 `签发时间戳.签名` 形式的会话令牌。

    时间戳向下取整(不能四舍五入):进位会产生"未来签发"的令牌,
    在签发后的第一秒内被 verify 的 `current - issued >= 0` 判拒。
    """
    issued_hex = f"{int(time.time() if now is None else now):d}"
    return f"{issued_hex}.{_sign(issued_hex)}"


def verify_session_token(token: str, *, now: float | None = None) -> bool:
    """校验令牌签名与有效期(TTL = settings.console_session_ttl_hours;≤0 视为不过期)。"""
    issued_hex, sep, digest = token.partition(".")
    if not sep or not issued_hex.isdigit():
        return False
    if not hmac.compare_digest(digest, _sign(issued_hex)):
        return False
    ttl = settings.console_session_ttl_hours * 3600.0
    if ttl <= 0:
        return True
    current = time.time() if now is None else now
    return 0 <= current - float(issued_hex) <= ttl


# 控制台鉴权豁免:外部接口(独立 X-API-Key)、健康检查与版本(探活/登录页装饰
# 信息)、鉴权端点自身。`/api/v1/external-calls` 是控制台审计查询,不在豁免之列。
_EXEMPT_PREFIXES = ("/api/v1/external/", "/api/v1/auth/")
_EXEMPT_PATHS = frozenset({
    "/health",
    "/api/v1/version",
    "/api/v1/compare/api-test",
})


def console_auth_required(path: str) -> bool:
    """该请求路径是否需要控制台会话。

    受保护 = /api/ 业务接口(除豁免名单)与 FastAPI 自带文档页;非 /api 路径
    (SPA 壳与静态资源)放行——页面数据仍全部来自受保护接口,公开壳无信息量。
    """
    if path in _EXEMPT_PATHS or path.startswith(_EXEMPT_PREFIXES):
        return False
    return path.startswith("/api/") or path in ("/docs", "/redoc", "/openapi.json")


def login_rate_limited(client_ip: str | None) -> bool:
    """该 IP 在滑动窗口内的登录失败次数是否已达上限。"""
    key = client_ip or "unknown"
    now = time.monotonic()
    with _failed_logins_lock:
        window = _failed_logins[key]
        while window and now - window[0] > _LOGIN_WINDOW_SECONDS:
            window.popleft()
        return len(window) >= _LOGIN_MAX_FAILURES


def record_login_failure(client_ip: str | None) -> None:
    key = client_ip or "unknown"
    with _failed_logins_lock:
        _failed_logins[key].append(time.monotonic())


def clear_login_failures(client_ip: str | None) -> None:
    key = client_ip or "unknown"
    with _failed_logins_lock:
        _failed_logins.pop(key, None)


def clear_all_login_failures() -> None:
    """清空全部失败计数(测试隔离用)。"""
    with _failed_logins_lock:
        _failed_logins.clear()
