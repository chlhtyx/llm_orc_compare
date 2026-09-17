"""运维发布命令：python -m document_comparison.release --help。

通过容器/主机权限执行，不增加公开的切换接口，不打印数据库凭据或配置正文。
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

from sqlalchemy import select

from .config import settings, _LLM_CONFIG_FIELDS, _LLM_DEFAULTS
from .db import engine, releases
from .db.models import LlmConfigRecord, ReleaseDeployment
from .deployment import check_readiness


def _require_quiet(s):
    releases.lock(s)
    if s.scalar(select(ReleaseDeployment.deployment_id).where(
        ReleaseDeployment.mode == "SERVING").limit(1)):
        raise releases.ReleaseConflict("配置快照/恢复需要暂停所有正式接单")
    if any(releases._counts(s).values()):
        raise releases.ReleaseConflict("配置快照/恢复需要先排空任务、回调和操作")


def snapshot(label: str):
    with engine.session_scope() as s:
        _require_quiet(s)
        deployment = releases._deployment(s, releases.DEPLOYMENT_ID)
        row = s.get(LlmConfigRecord, 1)
        effective = dict(_LLM_DEFAULTS)
        effective.update({k: v for k, v in (row.config if row else {}).items()
                          if k in _LLM_CONFIG_FIELDS})
        payload = {
            "format": 1, "deployment_id": deployment.deployment_id,
            "version": deployment.version, "label": label,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "llm_config": effective,
            # 包含启动凭据，仅写到 0600 文件，不返回正文。运行配置由部署清单恢复。
            "runtime": asdict(settings),
        }
        raw = json.dumps(payload, ensure_ascii=False, default=str, indent=2).encode("utf-8")
        directory = settings.storage_dir / "release-snapshots"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / f"{uuid.uuid4().hex}.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest(),
            "deployment_id": payload["deployment_id"], "version": payload["version"]}


def restore(path: Path, expected_sha256: str):
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("配置快照校验失败")
    payload = json.loads(raw)
    if payload.get("format") != 1 or payload.get("deployment_id") != releases.DEPLOYMENT_ID:
        raise ValueError("配置快照格式或来源部署不匹配")
    config = payload.get("llm_config")
    if not isinstance(config, dict) or set(config) - set(_LLM_CONFIG_FIELDS):
        raise ValueError("快照包含不兼容的配置字段，请人工审查迁移")
    if not set(_LLM_CONFIG_FIELDS).issubset(config):
        raise ValueError("快照字段不完整，请使用对应版本进行恢复或显式迁移")
    with engine.session_scope() as s:
        _require_quiet(s)
        releases._deployment(s, releases.DEPLOYMENT_ID)
        row = s.get(LlmConfigRecord, 1)
        if row is None:
            row = LlmConfigRecord(id=1, config=config)
            s.add(row)
        else:
            row.config = config
        row.updated_at = datetime.now(timezone.utc)
    return {"restored": True, "source_deployment": releases.DEPLOYMENT_ID,
            "runtime_restored": False}


def wait_drained(timeout: float, interval: float = 1):
    deadline = time.monotonic() + timeout
    while True:
        result = releases.status(releases.DEPLOYMENT_ID)
        if result["mode"] != "DRAINING":
            raise releases.ReleaseConflict("wait 只允许 DRAINING；先执行 drain")
        if result["drained"]:
            return result
        if time.monotonic() >= deadline:
            raise releases.ReleaseConflict("排空超时，保持 DRAINING；用 status 查看，或 serve 取消本次发布")
        time.sleep(min(interval, max(0, deadline - time.monotonic())))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "drain", "serve", "stop", "ready"):
        commands.add_parser(name)
    wait = commands.add_parser("wait")
    wait.add_argument("--timeout", type=float, default=1800)
    snap = commands.add_parser("snapshot")
    snap.add_argument("--label", required=True)
    rest = commands.add_parser("restore")
    rest.add_argument("--file", type=Path, required=True)
    rest.add_argument("--sha256", required=True)
    resolve = commands.add_parser("resolve-activity", help="仅在停止 owner 并对账后清除异常屏障")
    resolve.add_argument("--activity-id", required=True)
    resolve.add_argument("--confirmed-stopped-owner", required=True)
    args = parser.parse_args(argv)
    try:
        engine.init_engine()  # CLI 不导入 app，不启动任务，不自动执行迁移。
        engine.check_connection()
        if args.command == "status":
            result = releases.status(releases.DEPLOYMENT_ID)
        elif args.command == "wait":
            result = wait_drained(args.timeout)
        elif args.command == "snapshot":
            result = snapshot(args.label)
        elif args.command == "restore":
            result = restore(args.file, args.sha256)
        elif args.command == "resolve-activity":
            releases.resolve_activity(releases.DEPLOYMENT_ID, args.activity_id,
                                      args.confirmed_stopped_owner)
            result = releases.status(releases.DEPLOYMENT_ID)
        elif args.command == "ready":
            result = check_readiness()
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["prepared"] else 1
        else:
            if args.command == "serve" and not check_readiness()["prepared"]:
                raise releases.ReleaseConflict("数据库、schema 或文件存储未就绪")
            mode = {"drain": "DRAINING", "serve": "SERVING", "stop": "STOPPED"}[args.command]
            releases.set_mode(releases.DEPLOYMENT_ID, mode)
            result = releases.status(releases.DEPLOYMENT_ID)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (releases.ReleaseConflict, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        # SQLAlchemy 异常可能带 SQL 参数和配置内容；CLI 只给异常类型。
        print(f"发布操作失败（{type(exc).__name__}），未继续切换", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
