"""漏洞扫描防护的纯单元测试，无需 Postgres。"""
from pathlib import Path

import pytest

from document_comparison.http_security import safe_static_file_path, scan_probe_reason


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("GET", "/.env", "sensitive_file"),
        ("GET", "//etc/passwd", "absolute_path"),
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


def test_safe_static_file_path_rejects_absolute_escape(tmp_path: Path):
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (static_dir / "link.txt").symlink_to(outside)

    assert safe_static_file_path(static_dir, "//etc/passwd") == static_dir / "etc/passwd"
    assert safe_static_file_path(static_dir, "../outside.txt") is None
    assert safe_static_file_path(static_dir, "link.txt") is None


def test_safe_static_file_path_keeps_normal_asset_inside_root(tmp_path: Path):
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    asset = static_dir / "assets" / "app.js"
    asset.parent.mkdir()
    asset.write_text("ok", encoding="utf-8")

    assert safe_static_file_path(static_dir, "/assets/app.js") == asset
