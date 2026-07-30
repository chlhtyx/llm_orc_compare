"""漏洞扫描防护的纯单元测试，无需 Postgres。"""
import pytest

from document_comparison.http_security import scan_probe_reason


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("GET", "/.env", "sensitive_file"),
        ("GET", "/%252e%252e/.git/config", "path_traversal"),
        ("GET", "/wp-login.php", "scanner_endpoint"),
        ("GET", "/actuator/env", "scanner_endpoint"),
        ("TRACE", "/health", "blocked_method"),
        ("GET", "/api/v1/compare", None),
        ("POST", "/api/v1/compare", None),
    ],
)
def test_scan_probe_reason(method, path, expected):
    assert scan_probe_reason(method=method, raw_path=path) == expected
