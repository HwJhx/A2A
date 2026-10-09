"""Router 和后续 Broker 共用的消息模型与状态。"""
from __future__ import annotations

import secrets
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict

QUEUED = "QUEUED"
DISPATCHING = "DISPATCHING"
DELIVERED = "DELIVERED"
WAITING_TARGET = "WAITING_TARGET"
RETRYING = "RETRYING"
TARGET_BLOCKED = "TARGET_BLOCKED"
TARGET_MISSING = "TARGET_MISSING"
TIMEOUT = "TIMEOUT"
FAILED = "FAILED"
REJECTED = "REJECTED"

TERMINAL_STATES = frozenset({DELIVERED, TARGET_BLOCKED, TARGET_MISSING, TIMEOUT, FAILED, REJECTED})
ALL_STATES = TERMINAL_STATES | {QUEUED, DISPATCHING, WAITING_TARGET, RETRYING}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_msg_id() -> str:
    """时间前缀保证同目标消息按 ID 排序时保持入队顺序。"""
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

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Message":
        return cls(**data)
