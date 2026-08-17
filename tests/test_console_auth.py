"""控制台口令鉴权:令牌与路径判定纯单元测试(无 DB)+ API 集成测试(需 Postgres)。

覆盖 DC_CONSOLE_PASSWORD 启用后的核心行为:
- 业务接口未登录 401、登录后放行;豁免路径(health/version/external/auth)不受影响;
- 口令错误 401、失败限速 429、登出后 Cookie 失效;
- 会话令牌签名/过期/篡改/换口令失效。
"""
import pytest
from fastapi.testclient import TestClient

from document_comparison import console_auth
from document_comparison.api.app import create_app
from document_comparison.config import settings

SECRET = "s3cret-pass"


# —— 纯单元测试(无需 DB)——


@pytest.fixture()
def token_settings(monkeypatch):
    monkeypatch.setattr(settings, "console_password", SECRET)
    monkeypatch.setattr(settings, "console_session_ttl_hours", 1.0)


def test_session_token_roundtrip(token_settings):
    assert console_auth.verify_session_token(console_auth.issue_session_token())


def test_session_token_expired(token_settings):
    token = console_auth.issue_session_token(now=1_000_000.0)
    assert not console_auth.verify_session_token(token, now=1_000_000.0 + 3601)


def test_session_token_tampered(token_settings):
    issued_hex, _, digest = console_auth.issue_session_token().partition(".")
    # 确定性翻转末位(替换成固定字符可能碰巧与原值相同,产生偶发失败)
    flip = "0" if digest[-1] != "0" else "1"
    assert not console_auth.verify_session_token(f"{issued_hex}.{digest[:-1]}{flip}")
    assert not console_auth.verify_session_token("not-a-token")
    assert not console_auth.verify_session_token("")


def test_session_token_invalidated_by_password_change(token_settings):
    """修改口令即更换派生密钥,所有已发会话立即失效。"""
    token = console_auth.issue_session_token()
    settings.console_password = "another-pass"
    assert not console_auth.verify_session_token(token)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        # 控制台业务接口:受保护
        ("/api/v1/tasks", True),
        ("/api/v1/config/llm", True),
        ("/api/v1/compare/abc/report", True),
        ("/api/v1/statement/abc/source-file", True),
        # 外部接口审计查询是控制台端点,必须受保护(与 /api/v1/external/ 前缀区分)
        ("/api/v1/external-calls", True),
        # 豁免:外部接口(独立 X-API-Key)、探活/版本、鉴权端点、SPA 静态资源
        ("/api/v1/external/contractCompare/abc", False),
        ("/api/v1/compare/api-test", False),
        ("/health", False),
        ("/api/v1/version", False),
        ("/api/v1/auth/login", False),
        ("/api/v1/auth/status", False),
        ("/", False),
        ("/assets/app.js", False),
        # FastAPI 自带文档页同样受保护
        ("/docs", True),
        ("/openapi.json", True),
    ],
)
def test_console_auth_required_path_matrix(path, expected):
    assert console_auth.console_auth_required(path) is expected


def test_verify_console_password_empty_config(monkeypatch):
    monkeypatch.setattr(settings, "console_password", "")
    assert not console_auth.verify_console_password("")
    assert not console_auth.verify_console_password(SECRET)


# —— API 集成测试(需 Postgres)——


@pytest.fixture()
def client(db_isolated, monkeypatch):
    monkeypatch.setattr(settings, "console_password", SECRET)
    monkeypatch.setattr(settings, "console_session_ttl_hours", 1.0)
    console_auth.clear_all_login_failures()
    yield TestClient(create_app())
    console_auth.clear_all_login_failures()


def _login(c: TestClient, password: str = SECRET):
    return c.post("/api/v1/auth/login", json={"password": password})


def test_api_requires_session(client):
    assert client.get("/api/v1/tasks").status_code == 401
    assert client.get("/api/v1/config/llm").status_code == 401
    assert client.get("/docs").status_code == 401


def test_api_exempt_paths_open(client):
    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/version").status_code == 200
    status = client.get("/api/v1/auth/status").json()
    assert status == {"auth_required": True, "authenticated": False}


def test_login_wrong_password(client):
    r = _login(client, "wrong")
    assert r.status_code == 401
    assert not r.cookies.get(console_auth.COOKIE_NAME)
    assert client.get("/api/v1/tasks").status_code == 401


def test_login_success_grants_access(client):
    r = _login(client)
    assert r.status_code == 200
    assert r.json()["authenticated"] is True
    assert r.cookies.get(console_auth.COOKIE_NAME)
    # TestClient 自动携带会话 Cookie
    assert client.get("/api/v1/tasks").status_code == 200
    assert client.get("/api/v1/auth/status").json()["authenticated"] is True


def test_logout_invalidates_cookie(client):
    _login(client)
    assert client.get("/api/v1/tasks").status_code == 200
    assert client.post("/api/v1/auth/logout").status_code == 200
    assert client.get("/api/v1/tasks").status_code == 401


def test_login_rate_limited_after_failures(client):
    for _ in range(console_auth._LOGIN_MAX_FAILURES):
        assert _login(client, "wrong").status_code == 401
    # 达到阈值后同 IP 一律 429(即使口令正确也拒绝,直到窗口滑过)
    assert _login(client, "wrong").status_code == 429
    assert _login(client, SECRET).status_code == 429


def test_console_auth_disabled_keeps_open(db_isolated, monkeypatch):
    monkeypatch.setattr(settings, "console_password", "")
    console_auth.clear_all_login_failures()
    c = TestClient(create_app())
    assert c.get("/api/v1/tasks").status_code == 200
    assert c.get("/api/v1/auth/status").json() == {
        "auth_required": False,
        "authenticated": True,
    }
    # 未配置口令时登录端点直接拒绝,避免出现"永远登录成功"的假端点
    assert c.post("/api/v1/auth/login", json={"password": "x"}).status_code == 400


def test_rejected_401_still_gets_security_headers(client):
    """鉴权 401 由 scan_protection 外层补安全头,access log 也能看到该请求。"""
    r = client.get("/api/v1/tasks")
    assert r.status_code == 401
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.json()["message"] == "console authentication required"
