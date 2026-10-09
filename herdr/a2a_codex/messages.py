"""Router 和后续 Broker 共用的消息模型与协议 v2 状态迁移。"""
from __future__ import annotations

import secrets
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict

QUEUED = "QUEUED"
WAITING_TARGET = "WAITING_TARGET"
DISPATCHING = "DISPATCHING"
RETRYING = "RETRYING"
DELIVERY_UNCERTAIN = "DELIVERY_UNCERTAIN"
DELIVERED = "DELIVERED"
TARGET_BLOCKED = "TARGET_BLOCKED"
TARGET_MISSING = "TARGET_MISSING"
TIMEOUT = "TIMEOUT"
FAILED = "FAILED"
REJECTED = "REJECTED"

TERMINAL_STATES = frozenset({DELIVERED, TARGET_BLOCKED, TARGET_MISSING, TIMEOUT, FAILED, REJECTED})
NON_TERMINAL_STATES = frozenset({QUEUED, WAITING_TARGET, DISPATCHING, RETRYING, DELIVERY_UNCERTAIN})
ALL_STATES = TERMINAL_STATES | NON_TERMINAL_STATES

# 协议 v2；守卫条件(例如 DELIVERY_UNCERTAIN 的操作员裁定)由 Broker 强制。
ALLOWED_TRANSITIONS = {
    QUEUED: frozenset({WAITING_TARGET, DISPATCHING, TARGET_BLOCKED, TARGET_MISSING, TIMEOUT, FAILED}),
    WAITING_TARGET: frozenset({DISPATCHING, TARGET_BLOCKED, TARGET_MISSING, TIMEOUT, FAILED}),
    DISPATCHING: frozenset({DELIVERED, DELIVERY_UNCERTAIN, RETRYING, TARGET_BLOCKED, TARGET_MISSING, FAILED}),
    RETRYING: frozenset({DISPATCHING, WAITING_TARGET, TARGET_BLOCKED, TARGET_MISSING, TIMEOUT, FAILED}),
    DELIVERY_UNCERTAIN: frozenset({DELIVERED, RETRYING, FAILED}),
}


def can_transition(old: str, new: str) -> bool:
    """判断状态迁移是否在协议 v2 表中；同状态更新不是迁移。"""
    return new in ALLOWED_TRANSITIONS.get(old, frozenset())


def _check_transition_table() -> None:
    if set(ALLOWED_TRANSITIONS) != NON_TERMINAL_STATES:
        raise RuntimeError("迁移表必须且只能为非终态定义出口")
    for old, targets in ALLOWED_TRANSITIONS.items():
        if not targets <= ALL_STATES - {QUEUED, REJECTED}:
            raise RuntimeError(f"{old} 的迁移目标非法")
        if FAILED not in targets:
            raise RuntimeError(f"{old} 必须存在到 FAILED 的迁移")


_check_transition_table()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_msg_id() -> str:
    """生成唯一标识；Broker 必须按 queue_seq 排序，不能把 msg_id 当队列序号。"""
    return "%016x-%s" % (time.time_ns(), secrets.token_hex(3))


@dataclass(frozen=True)
class Message:
    msg_id: str
    created_at: str
    edge_id: str
    src: str
    dst: str
    project_id: str
    ip_id: str
    session: str
    text: str
    state: str
    topology_revision: str
    attempts: int = 0
    detail: str = ""
    updated_at: str = ""
    queue_seq: int | None = None
    retry_of: str | None = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Message":
        return cls(**data)
