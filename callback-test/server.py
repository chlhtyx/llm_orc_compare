#!/usr/bin/env python3
"""最小 callback 接收服务。

用于验证主服务(`llm_orc_compare`)的 webhook 回调:把收到的每个 POST 报文
(请求行、来源、X-Event-Id、JSON body)原样写入日志,便于人工核对;并回 200
让主服务判定交付成功(主服务对 status < 300 视为成功)。

只依赖 Python 标准库,无需 pip 安装。

用法:
    python server.py [host] [port]        # 默认 0.0.0.0:9000

环境变量:
    CALLBACK_LOG_FILE  额外把报文追加到该文件(默认 /app/logs/callbacks.log);
                        设为空字符串则只写 stdout。

验证:
    docker logs -f callback-test
    # 或挂载了日志目录时:
    tail -f callback-test/logs/callbacks.log
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG_FILE = os.environ.get("CALLBACK_LOG_FILE", "/app/logs/callbacks.log")


def _setup_logging() -> logging.Logger:
    log = logging.getLogger("callback-test")
    log.setLevel(logging.INFO)
    log.propagate = False
    fmt = logging.Formatter("%(message)s")
    sh = logging.StreamHandler(sys.stdout)  # stdout → docker logs 可见
    sh.setFormatter(fmt)
    log.addHandler(sh)
    if LOG_FILE:
        try:
            os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
            fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
            fh.setFormatter(fmt)
            log.addHandler(fh)
        except OSError:
            # 目录不可写时只写 stdout,不阻断启动
            pass
    return log


logger = _setup_logging()


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 + keep-alive,需保证每个响应都带正确 Content-Length(_send_json 已处理)
    protocol_version = "HTTP/1.1"
    server_version = "callback-test/1.0"

    # —— 工具方法 ——
    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length) if length > 0 else b""

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self, method: str) -> None:
        raw = self._read_body()
        event_id = self.headers.get("X-Event-Id", "")
        ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S%z")

        # 尽量美化 JSON;解析失败就原样输出
        try:
            pretty = json.dumps(
                json.loads(raw), ensure_ascii=False, indent=2, sort_keys=True
            )
        except (ValueError, TypeError):
            pretty = raw.decode("utf-8", errors="replace")

        block = (
            f"\n{'=' * 70}\n"
            f"[{ts}] {method} {self.path}\n"
            f"from      : {self.client_address[0]}:{self.client_address[1]}\n"
            f"X-Event-Id: {event_id}\n"
            f"body ({len(raw)} bytes):\n{pretty}\n"
            f"{'=' * 70}"
        )
        logger.info(block)

        # 主服务 webhook.deliver 把 < 300 视为成功;统一回 200
        self._send_json(200, {"received": True, "event_id": event_id})

    # —— 路由 ——
    def do_POST(self) -> None:
        self._record("POST")

    def do_PUT(self) -> None:
        self._record("PUT")

    def do_GET(self) -> None:
        # 任意路径都回存活标记,便于 curl 验证服务起来
        self._send_json(200, {"alive": True, "service": "callback-test"})

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *args):  # 抑制默认 stderr 访问日志(已在 block 里记录)
        pass


def main() -> None:
    host = sys.argv[1] if len(sys.argv) > 1 else "0.0.0.0"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 9000
    httpd = ThreadingHTTPServer((host, port), Handler)  # 多线程,避免并发回调互相阻塞
    logger.info(
        "callback-test listening on %s:%s (log file: %s)",
        host, port, LOG_FILE or "<none>",
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
