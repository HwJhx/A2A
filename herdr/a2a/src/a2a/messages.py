"""消息模型、状态常量与合法状态迁移表(协议版本 2,见 herdr/claude/08-protocol.md)。

状态迁移表是 Spool.update 强制执行的规则:不在表里的迁移一律拒绝,终态之后不允许任何修改。
"""
from __future__ import annotations

import re
import secrets
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, Mapping, Optional

# ---- 状态 --------------------------------------------------------------
QUEUED = "QUEUED"                        # 鉴权通过,已写入持久队列,等待 broker 投递
WAITING_TARGET = "WAITING_TARGET"        # 目标忙(working / unknown),正在等
DISPATCHING = "DISPATCHING"              # 写前标记:broker 调用 herdr 之前先落盘;调用后结果不明一律转 DELIVERY_UNCERTAIN
RETRYING = "RETRYING"                    # 已证明 prompt 未提交,退避后重试
DELIVERY_UNCERTAIN = "DELIVERY_UNCERTAIN"  # prompt 可能已经送达;默认不自动重发,出口只能由操作员裁定或经验证的机制触发
DELIVERED = "DELIVERED"                  # 已送达,且有证据(accepted_and_observed / operator_confirmed / verified_mechanism)
TARGET_BLOCKED = "TARGET_BLOCKED"        # 目标卡在审批/提问,决策 D8:立即失败,broker 不排队、不自动重试
TARGET_MISSING = "TARGET_MISSING"        # 投递时目标已不存在
TIMEOUT = "TIMEOUT"                      # 等待目标 READY 超时,或目标状态查询持续失败
FAILED = "FAILED"                        # 操作员放弃,或确定不可恢复的错误
REJECTED = "REJECTED"                    # 鉴权失败。只出现在审计日志里,永远不进入队列

TERMINAL_STATES: FrozenSet[str] = frozenset({DELIVERED, TARGET_BLOCKED, TARGET_MISSING, TIMEOUT, FAILED, REJECTED})
NON_TERMINAL_STATES: FrozenSet[str] = frozenset({QUEUED, WAITING_TARGET, DISPATCHING, RETRYING, DELIVERY_UNCERTAIN})
ALL_STATES: FrozenSet[str] = TERMINAL_STATES | NON_TERMINAL_STATES

# ---- 合法迁移表 ---------------------------------------------------------
# 键是当前状态,值是允许迁往的状态。终态没有出口。同一状态内更新 detail / attempts 不算迁移,由 Spool 单独处理。
# FAILED 在表上可以从任何非终态到达;但 DELIVERY_UNCERTAIN -> FAILED 只能由操作员放弃触发(守卫由 broker 负责)。
# 版本 2 相对版本 1:删除 DISPATCHING -> WAITING_TARGET(目标复核放在进入 DISPATCHING 之前);
# 新增 RETRYING -> TARGET_BLOCKED / TARGET_MISSING。
ALLOWED_TRANSITIONS: Mapping[str, FrozenSet[str]] = {
    QUEUED: frozenset({WAITING_TARGET, DISPATCHING, TARGET_BLOCKED, TARGET_MISSING, TIMEOUT, FAILED}),
    WAITING_TARGET: frozenset({DISPATCHING, TARGET_BLOCKED, TARGET_MISSING, TIMEOUT, FAILED}),
    DISPATCHING: frozenset({DELIVERED, DELIVERY_UNCERTAIN, RETRYING, TARGET_BLOCKED, TARGET_MISSING, FAILED}),
    RETRYING: frozenset({DISPATCHING, WAITING_TARGET, TARGET_BLOCKED, TARGET_MISSING, TIMEOUT, FAILED}),
    DELIVERY_UNCERTAIN: frozenset({DELIVERED, RETRYING, FAILED}),
}


def can_transition(old: str, new: str) -> bool:
    """old -> new 是否是合法迁移。同一状态(old == new)不属于迁移,返回 False。"""
    return new in ALLOWED_TRANSITIONS.get(old, frozenset())


def _check_table() -> None:
    """导入时自检:表必须自洽,否则尽早失败。"""
    assert set(ALLOWED_TRANSITIONS) == NON_TERMINAL_STATES, "只有非终态才有出口"
    for old, targets in ALLOWED_TRANSITIONS.items():
        assert targets <= ALL_STATES - {QUEUED, REJECTED}, f"{old} 的出口含非法状态"
        assert FAILED in targets, f"{old} 必须能到达 FAILED"


_check_table()

# ---- 标识 ---------------------------------------------------------------
MSG_ID_RE = re.compile(r"^[0-9a-f]{16}-[0-9a-f]{6}$")
AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_msg_id() -> str:
    """消息 ID:唯一标识。**不作排序键**——同一目标的顺序由 queue_seq 决定(08 §4 规则 10、§7.4)。"""
    return "%016x-%s" % (time.time_ns(), secrets.token_hex(3))


@dataclass(frozen=True)
class Message:
    msg_id: str
    created_at: str
    edge_id: str
    src: str                 # 发送方 agent_id
    dst: str                 # 目标 agent_id
    project_id: str
    ip_id: str               # 发送方与目标共同的 IP(同 IP 铁律)
    session: str
    text: str                # 已渲染的固定句式
    state: str
    topology_revision: str
    attempts: int = 0
    detail: str = ""
    updated_at: str = ""
    queue_seq: Optional[int] = None  # 目标队列中的槽位序号,只由 Spool 在入队时分配(08 §7.4)
    retry_of: Optional[str] = None   # 终态后重试:被重试的原消息 msg_id(继承它的 queue_seq)
    ruling_id: Optional[str] = None  # 最近一次改变本消息状态的操作员裁定;与状态同一次落盘,用于幂等补做

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Message":
        return cls(**data)
