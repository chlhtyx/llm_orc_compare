"""真实 PG 事务与 ASGI 排空边界的回归测试。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from document_comparison.config import settings, _LLM_CONFIG_FIELDS
from document_comparison.db import releases, repository
from document_comparison.deployment import DeploymentAdmissionMiddleware, record_persistence_error


@pytest.fixture
def deployment(db_isolated, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "deployment_id", "a-v1")
    monkeypatch.setattr(settings, "deployment_initial_mode", "SERVING")
    monkeypatch.setattr(settings, "version", "v1")
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    monkeypatch.setattr(settings, "console_password", "")
    releases.register("a-v1", "v1", "SERVING")
    return "a-v1"


@pytest.mark.parametrize("mode", ["SERVING", "DRAINING", "STOPPED"])
@pytest.mark.parametrize("version", ["v1", "v2", "v0"])
def test_restart_upgrade_and_rollback_preserve_mode(deployment, mode, version):
    if mode != "SERVING":
        releases.set_mode(deployment, "DRAINING")
    if mode == "STOPPED":
        releases.set_mode(deployment, "STOPPED")
    releases.register(deployment, version, "SERVING")
    state = releases.status(deployment)
    assert state["version"] == version
    assert state["mode"] == mode
    assert releases.begin_request(deployment, "new-worker", "upload", uuid.uuid4().hex) == (mode == "SERVING")
    if mode != "STOPPED":
        with pytest.raises(releases.ReleaseConflict):
            releases.register("other-deployment", version, "SERVING")


def test_version_update_keeps_queued_tasks_callbacks_and_activities(deployment):
    repository.create_task("queued", "compare", callback_url="https://test.invalid/cb")
    repository.set_task_queue_payload("queued", {"runner": "compare"})
    releases.begin_request(deployment, "old-worker", "upload", uuid.uuid4().hex)
    before = releases.status(deployment)["counts"]
    releases.register(deployment, "v2", "SERVING")
    assert releases.status(deployment)["counts"] == before
    rec = repository.claim_next_queued_task("new-worker", 4, 120, deployment_id=deployment)
    assert rec.task_id == "queued"


def test_concurrent_activation_has_exactly_one_winner(deployment):
    releases.set_mode(deployment, "DRAINING")
    releases.set_mode(deployment, "STOPPED")

    def activate(name):
        try:
            if name == deployment:
                releases.set_mode(name, "SERVING")
            else:
                releases.register(name, "v2", "SERVING")
            return True
        except releases.ReleaseConflict:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(activate, [deployment, "a-v2"])) == [False, True]


def test_admitted_request_blocks_stop_until_finished(deployment):
    token = uuid.uuid4().hex
    assert releases.begin_request(deployment, "worker", "upload", token)
    releases.set_mode(deployment, "DRAINING")
    assert not releases.begin_request(deployment, "worker", "upload", uuid.uuid4().hex)
    with pytest.raises(releases.ReleaseConflict):
        releases.set_mode(deployment, "STOPPED")
    releases.finish_activity(token)
    releases.set_mode(deployment, "STOPPED")


def test_pending_orphan_blocks_drain(deployment):
    repository.create_task("orphan", "compare")
    releases.set_mode(deployment, "DRAINING")
    assert releases.status(deployment)["counts"]["pending_tasks"] == 1
    with pytest.raises(releases.ReleaseConflict):
        releases.set_mode(deployment, "STOPPED")


def test_stopped_cannot_claim_and_draining_can_finish_queue(deployment):
    repository.create_task("queued", "compare")
    repository.set_task_queue_payload("queued", {"runner": "compare"})
    releases.set_mode(deployment, "DRAINING")
    # DRAINING 拒绝新请求但允许领取存量任务，直到回调收尾。
    rec = repository.claim_next_queued_task("w1", 4, 120, deployment_id=deployment)
    assert rec.task_id == "queued"
    assert releases.status(deployment)["counts"]["activities"] == 1
    repository.update_task_status("queued", "done")
    # done 之后 job/callback 持续持有屏障。
    assert not releases.status(deployment)["drained"]
    releases.finish_activity(rec._release_activity_id)
    releases.set_mode(deployment, "STOPPED")
    repository.create_task("queued2", "compare")
    repository.set_task_queue_payload("queued2", {"runner": "compare"})
    assert repository.claim_next_queued_task("w2", 4, 120, deployment_id=deployment) is None


def test_pending_callback_blocks_cutover(deployment):
    repository.create_task("callback", "compare", callback_url="https://test.invalid/cb")
    repository.update_task_status("callback", "done")
    releases.set_mode(deployment, "DRAINING")
    assert releases.status(deployment)["counts"]["pending_callbacks"] == 1
    with pytest.raises(releases.ReleaseConflict):
        releases.set_mode(deployment, "STOPPED")
    repository.update_callback_result("callback", success=False, error="exhausted")
    assert releases.status(deployment)["drained"]


async def test_middleware_tracks_upload_and_background_after_response(deployment):
    entered, finish, audit_finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    sent = []

    async def inner(scope, receive, send):
        entered.set()
        await finish.wait()
        scope["release_background"].append(asyncio.create_task(audit_finish.wait()))
        await send({"type": "http.response.start", "status": 202, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(message):
        sent.append(message)

    app = DeploymentAdmissionMiddleware(inner, "w1")
    scope = {"type": "http", "method": "POST", "path": "/api/v1/compare"}
    task = asyncio.create_task(app(scope, None, send))
    await entered.wait()
    releases.set_mode(deployment, "DRAINING")
    assert not releases.status(deployment)["drained"]
    finish.set()
    # 响应已送出也必须等审计/后台收尾。
    while not sent:
        await asyncio.sleep(0)
    assert not releases.status(deployment)["drained"]
    audit_finish.set()
    await task
    assert releases.status(deployment)["drained"]


async def test_middleware_503_never_reads_upload_and_db_failure_fails_closed(deployment, monkeypatch):
    sent = []

    async def forbidden(*args):
        pytest.fail("不得读取请求内容或进入业务路由")

    async def send(message):
        sent.append(message)

    releases.set_mode(deployment, "DRAINING")
    app = DeploymentAdmissionMiddleware(forbidden, "w1")
    await app({"type": "http", "method": "POST", "path": "/api/v1/compare"}, forbidden, send)
    assert sent[0]["status"] == 503
    assert b"retry-after" in dict(sent[0]["headers"])
    monkeypatch.setattr(releases, "begin_request", lambda *a: (_ for _ in ()).throw(RuntimeError()))
    sent.clear()
    await app({"type": "http", "method": "PUT", "path": "/api/v1/config/llm"}, forbidden, send)
    assert sent[0]["status"] == 503


async def test_cancelled_and_failed_writes_retain_activity(deployment):
    async def cancelled(*args):
        raise asyncio.CancelledError
    app = DeploymentAdmissionMiddleware(cancelled, "w1")
    with pytest.raises(asyncio.CancelledError):
        await app({"type": "http", "method": "POST", "path": "/api/v1/compare"}, None, None)
    async def failed_write(*args):
        record_persistence_error()
    await DeploymentAdmissionMiddleware(failed_write, "w1")(
        {"type": "http", "method": "POST", "path": "/api/v1/compare"}, None, None)
    assert releases.status(deployment)["counts"]["activities"] == 2


async def test_job_barrier_waits_for_callback_and_shutdown(deployment, monkeypatch):
    from document_comparison.tasks import TaskManager
    manager = TaskManager()
    callback_done, pipeline_done = asyncio.Event(), asyncio.Event()
    repository.create_task("job", "compare")
    repository.set_task_queue_payload("job", {"runner": "compare"})
    rec = repository.claim_next_queued_task("w1", 1, 120, deployment_id=deployment)

    async def run(rec):
        manager._tasks[rec.task_id] = SimpleNamespace(
            pending_callbacks=[asyncio.create_task(callback_done.wait())])
        repository.update_task_status(rec.task_id, "done")
        pipeline_done.set()
    monkeypatch.setattr(manager, "_run_claimed_queue_task", run)
    work = asyncio.create_task(manager._run_release_job(rec))
    manager._queue_running.add(work)
    await pipeline_done.wait()
    shutdown = asyncio.create_task(manager.stop_queue_dispatcher())
    await asyncio.sleep(0)
    assert not shutdown.done()
    assert not releases.status(deployment)["drained"]
    callback_done.set()
    await shutdown
    assert releases.status(deployment)["drained"]


def test_snapshots_are_private_and_restore_only_while_quiet(deployment):
    from document_comparison.release import snapshot, restore
    repository.save_llm_config({"llm_api_key": "test-private-key", "llm_model": "model-v1"})
    with pytest.raises(releases.ReleaseConflict):
        snapshot("before")
    releases.set_mode(deployment, "DRAINING")
    result = snapshot("before")
    path = Path(result["path"])
    assert path.stat().st_mode & 0o777 == 0o600
    assert "test-private-key" not in json.dumps(result)
    assert set(_LLM_CONFIG_FIELDS).issubset(json.loads(path.read_text())["llm_config"])
    repository.save_llm_config({"llm_api_key": "different", "llm_model": "model-v2"})
    with pytest.raises(ValueError):
        restore(path, "bad-hash", deployment)
    restore(path, result["sha256"], deployment)
    assert repository.get_llm_config()["llm_model"] == "model-v1"
    assert repository.get_llm_config()["llm_api_key"] == "test-private-key"


def test_maintenance_api_keeps_read_auth_and_external_audit(deployment):
    from fastapi.testclient import TestClient
    from document_comparison.api.app import create_app
    app = create_app()
    client = TestClient(app)
    releases.set_mode(deployment, "DRAINING")
    for path in ("/api/v1/compare", "/api/v1/compare/api-test", "/api/v1/raw-compare",
                 "/api/v1/statement", "/api/v1/statement/api-test",
                 "/api/v1/tasks/x/recover", "/api/v1/tasks/x/stop",
                 "/api/v1/tasks/x/redeliver-callback", "/api/v1/external/contractCompare",
                 "/api/v1/external/amountStat"):
        response = client.post(path)
        assert response.status_code == 503, path
        assert response.headers["Retry-After"] == "60"
    assert client.put("/api/v1/config/llm", json={}).status_code == 503
    cors = client.post("/api/v1/compare", headers={"Origin": "https://client.example.test"})
    assert cors.status_code == 503
    assert cors.headers["access-control-allow-origin"] == "*"
    assert "Retry-After" in cors.headers["access-control-expose-headers"]
    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/deployment").json()["mode"] == "DRAINING"
    assert client.get("/api/v1/auth/status").status_code == 200
    assert client.get("/api/v1/version").status_code == 200
    from sqlalchemy import select, func
    from document_comparison.db.engine import session_scope
    from document_comparison.db.models import ExternalApiCall
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(ExternalApiCall).where(
            ExternalApiCall.status_code == 503)) == 2


def test_wait_timeout_does_not_activate_or_clear_tasks(deployment):
    from document_comparison.release import wait_drained
    repository.create_task("pending", "compare")
    releases.set_mode(deployment, "DRAINING")
    with pytest.raises(releases.ReleaseConflict, match="超时"):
        wait_drained(0)
    assert releases.status(deployment)["mode"] == "DRAINING"


def test_abandoned_activity_requires_matching_stopped_owner(deployment):
    token = uuid.uuid4().hex
    releases.begin_request(deployment, "old-worker", "request", token)
    releases.set_mode(deployment, "DRAINING")
    with pytest.raises(releases.ReleaseConflict):
        releases.resolve_activity(deployment, token, "another-worker")
    releases.resolve_activity(deployment, token, "old-worker")
    assert releases.status(deployment)["drained"]


def test_readiness_rejects_missing_schema_and_storage_failure(deployment, monkeypatch):
    from document_comparison.deployment import check_readiness
    from document_comparison.db.engine import get_engine
    from sqlalchemy import text
    # db_isolated 使用 metadata.create_all；显式移除可能来自迁移测试的 head。
    with get_engine().begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    result = check_readiness()
    assert result["checks"]["database"]
    assert not result["checks"]["schema"]
    assert not result["prepared"]
    monkeypatch.setattr("document_comparison.deployment.tempfile.TemporaryFile",
                        lambda **kw: (_ for _ in ()).throw(OSError("read-only")))
    result = check_readiness()
    assert not result["checks"]["uploads"]
    assert not result["checks"]["reports"]


def test_concurrent_admission_and_drain_share_transaction_barrier(deployment):
    def enter(_):
        token = uuid.uuid4().hex
        return token if releases.begin_request(deployment, "w", "upload", token) else None
    with ThreadPoolExecutor(max_workers=8) as pool:
        pending = [pool.submit(enter, n) for n in range(20)]
        draining = pool.submit(releases.set_mode, deployment, "DRAINING")
        pending.extend(pool.submit(enter, n) for n in range(20))
        draining.result()
        accepted = [f.result() for f in pending if f.result() is not None]
    assert releases.status(deployment)["counts"]["activities"] == len(accepted)
    assert not releases.begin_request(deployment, "w", "late", uuid.uuid4().hex)
    for token in accepted:
        releases.finish_activity(token)
    releases.set_mode(deployment, "STOPPED")


def test_prepare_script_stops_after_drain_timeout(tmp_path):
    import os
    import subprocess
    fake = tmp_path / "docker"
    log = tmp_path / "calls.txt"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$FAKE_DOCKER_LOG"\n'
                    'case "$*" in *"release wait"*) exit 1;; esac\n')
    fake.chmod(0o700)
    script = Path(__file__).resolve().parents[1] / "scripts/release.sh"
    result = subprocess.run(["bash", str(script), "prepare", "a", "before-v2", "0"],
                            env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
                                 "FAKE_DOCKER_LOG": str(log)}, capture_output=True, text=True)
    assert result.returncode == 1
    calls = log.read_text()
    assert "release drain" in calls and "release wait" in calls
    assert "snapshot" not in calls and "stop" not in calls
