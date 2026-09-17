"""发布控制的事务边界。控制数据失败时 fail closed；不改变业务双写规则。"""
from __future__ import annotations

from datetime import datetime, timezone
import re
import uuid

from sqlalchemy import func, select, text

from .engine import session_scope
from .models import ReleaseActivity, ReleaseDeployment, TaskRecord

# 单服务内部标识，不再从部署环境读取。保留 main 以兼容已有发布记录。
DEPLOYMENT_ID = "main"

MODES = {"SERVING", "DRAINING", "STOPPED"}
ACTIVE = {"SERVING", "DRAINING"}


class ReleaseConflict(RuntimeError):
    pass


def lock(session):
    # 必须先取得发布锁，再取得任务队列锁 92134017，避免相反顺序死锁。
    session.execute(text("SELECT pg_advisory_xact_lock(92134018)"))


def _deployment(session, deployment_id):
    row = session.get(ReleaseDeployment, deployment_id)
    if row is None:
        raise ReleaseConflict("部署未注册，请先启动对应版本的服务")
    return row


def _other_active(session, deployment_id):
    return session.scalar(select(ReleaseDeployment.deployment_id).where(
        ReleaseDeployment.deployment_id != deployment_id,
        ReleaseDeployment.mode.in_(ACTIVE),
    ).limit(1))


def register(deployment_id: str, version: str, initial_mode: str):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", deployment_id):
        raise ValueError("部署标识必须为 1-100 位字母、数字、点、下划线或短横线")
    if initial_mode != "SERVING":
        raise ValueError("初始状态只允许 SERVING")
    if not version or len(version) > 100:
        raise ValueError("发布版本长度必须为 1-100")
    with session_scope() as s:
        lock(s)
        row = s.get(ReleaseDeployment, deployment_id)
        if row is not None:
            if row.version != version:
                # 单服务由 Compose 替换旧容器；版本登记不要求额外的停机状态切换。
                # 保留显式维护状态，也不修改任务、回调和操作记录。
                row.version = version
                row.updated_at = datetime.now(timezone.utc)
            return  # 同版本重启不得覆盖已持久化的维护状态。
        if _other_active(s, deployment_id):
            raise ReleaseConflict("正式库已有活动部署，不允许重复接单")
        s.add(ReleaseDeployment(deployment_id=deployment_id, version=version,
                                mode=initial_mode, updated_at=datetime.now(timezone.utc)))


def _counts(s):
    return {
        # 包括没有 queue_payload 的 pending 孤儿记录，不能悄悄判定排空。
        "pending_tasks": s.scalar(select(func.count()).select_from(TaskRecord).where(
            TaskRecord.status == "pending")) or 0,
        "running_tasks": s.scalar(select(func.count()).select_from(TaskRecord).where(
            TaskRecord.status == "running")) or 0,
        "pending_callbacks": s.scalar(select(func.count()).select_from(TaskRecord).where(
            TaskRecord.callback_status == "pending")) or 0,
        "activities": s.scalar(select(func.count()).select_from(ReleaseActivity)) or 0,
    }


def status(deployment_id):
    with session_scope() as s:
        lock(s)
        row = _deployment(s, deployment_id)
        counts = _counts(s)
        activities = s.scalars(select(ReleaseActivity).order_by(
            ReleaseActivity.created_at).limit(100)).all()
        return {
            "deployment_id": row.deployment_id, "version": row.version, "mode": row.mode,
            "updated_at": row.updated_at.isoformat(), "counts": counts,
            "drained": not any(counts.values()),
            "activities": [{"activity_id": a.activity_id, "deployment_id": a.deployment_id,
                            "owner": a.owner, "kind": a.kind, "detail": a.detail,
                            "created_at": a.created_at.isoformat()} for a in activities],
            "other_active_deployment": _other_active(s, deployment_id),
        }


def set_mode(deployment_id, mode):
    if mode not in MODES:
        raise ValueError("未知部署状态")
    with session_scope() as s:
        lock(s)
        row = _deployment(s, deployment_id)
        if mode == row.mode:
            return
        if mode == "DRAINING" and row.mode != "SERVING":
            raise ReleaseConflict("只有 SERVING 可以进入 DRAINING")
        if mode == "SERVING":
            if _other_active(s, deployment_id):
                raise ReleaseConflict("另一个部署仍在接单或排空")
            # 取消自身排空可以继续原任务；从停机启动则必须全局排空。
            if row.mode != "DRAINING" and any(_counts(s).values()):
                raise ReleaseConflict("正式库仍有任务、回调或操作未排空")
        if mode == "STOPPED":
            if row.mode == "SERVING":
                raise ReleaseConflict("请先 DRAINING，避免直接停领造成队列积压")
            if row.mode == "DRAINING" and any(_counts(s).values()):
                raise ReleaseConflict("任务、回调或操作尚未排空")
        row.mode = mode
        row.updated_at = datetime.now(timezone.utc)


def begin_request(deployment_id, owner, detail, activity_id):
    with session_scope() as s:
        lock(s)
        row = _deployment(s, deployment_id)
        if row.mode != "SERVING":
            return False
        add_activity(s, deployment_id, owner, "request", detail, activity_id)
        return True


def claim_allowed(s, deployment_id):
    lock(s)
    return _deployment(s, deployment_id).mode in ACTIVE


def add_activity(s, deployment_id, owner, kind, detail, activity_id=None):
    activity_id = activity_id or uuid.uuid4().hex
    s.add(ReleaseActivity(activity_id=activity_id, deployment_id=deployment_id,
                          owner=owner, kind=kind, detail=detail[:200],
                          created_at=datetime.now(timezone.utc)))
    return activity_id


def finish_activity(activity_id):
    with session_scope() as s:
        s.query(ReleaseActivity).filter_by(activity_id=activity_id).delete()


def resolve_activity(deployment_id, activity_id, stopped_owner):
    """仅运维核实 owner 已停止、完成对账后使用；不自动回收未知操作。"""
    with session_scope() as s:
        lock(s)
        row = _deployment(s, deployment_id)
        if row.mode == "SERVING":
            raise ReleaseConflict("先暂停接单，再核实异常操作")
        activity = s.get(ReleaseActivity, activity_id)
        if not activity or activity.deployment_id != deployment_id or activity.owner != stopped_owner:
            raise ReleaseConflict("操作 ID、部署 ID 或已停止的 owner 不匹配")
        s.delete(activity)
