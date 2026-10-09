"""Router：校验发送方与拓扑边，用固定模板生成消息并持久入队。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from .audit import AuditLog
from .identity import AgentIdentity, IdentityError
from .messages import QUEUED, REJECTED, Message, new_msg_id, now_iso
from .registry import AgentNotRegisteredError, AgentRecord, Registry
from .spool import Spool
from .topology import Topology, TopologyStore

REJECT_IDENTITY = "identity"
REJECT_UNKNOWN_EDGE = "unknown_edge"
REJECT_WRONG_DIRECTION = "wrong_direction"
REJECT_TARGET_NOT_IN_TOPOLOGY = "target_not_in_topology"
REJECT_TARGET_MISSING = "target_missing"
REJECT_TARGET_NOT_RUNNING = "target_not_running"
REJECT_BAD_MESSAGE = "bad_message"
MAX_MESSAGE_CHARS = 1000


class SendRejected(RuntimeError):
    def __init__(self, code: str, reason: str, *, msg_id: str,
                 cause: Optional[BaseException] = None) -> None:
        super().__init__(f"[{code}] {reason}")
        self.code = code
        self.reason = reason
        self.msg_id = msg_id
        self.cause = cause


@dataclass(frozen=True)
class SendReceipt:
    msg_id: str
    edge_id: str
    src: str
    dst: str
    text: str
    state: str
    topology_revision: str


def session_from_env(env: Mapping[str, str]) -> str:
    return env.get("HERDR_SESSION") or "default"


class Router:
    """纯业务路由层；不调用 Herdr、不等待目标状态、不执行 prompt。"""

    def __init__(self, topology: TopologyStore | Topology, registry: Registry,
                 spool: Spool, audit: AuditLog, *, session: Optional[str] = None,
                 max_chars: int = MAX_MESSAGE_CHARS) -> None:
        if max_chars <= 0:
            raise ValueError("max_chars 必须为正数")
        self._topology_source = topology
        self.registry = registry
        self.spool = spool
        self.audit = audit
        self.session = session
        self.max_chars = max_chars

    def _current_topology(self) -> tuple[Topology, str]:
        if isinstance(self._topology_source, TopologyStore):
            topology = self._topology_source.current
            return topology, self._topology_source.revision
        return self._topology_source, "static"

    def _reject(self, *, msg_id: str, edge_id: str, session: str, revision: str,
                code: str, reason: str, src: Optional[str] = None, dst: Optional[str] = None,
                env: Mapping[str, str], cause: Optional[BaseException] = None) -> SendRejected:
        event: Dict[str, Any] = {
            "msg_id": msg_id, "edge_id": edge_id, "state": REJECTED,
            "reject_code": code, "detail": reason, "src": src, "dst": dst,
            "session": session, "topology_revision": revision,
        }
        if src is None:
            event.update({
                "claimed_project": env.get("A2A_PROJECT_ID"),
                "claimed_role": env.get("A2A_ROLE"),
                "claimed_ip": env.get("A2A_IP"),
                "claimed_pane": env.get("HERDR_PANE_ID"),
            })
        self.audit.record(event)
        return SendRejected(code, reason, msg_id=msg_id, cause=cause)

    def send(self, edge_id: str, env: Optional[Mapping[str, str]] = None) -> SendReceipt:
        """只接受 edge ID；发送方身份由进程环境 + Registry 推导。"""
        current_env = os.environ if env is None else env
        msg_id = new_msg_id()
        session = self.session or session_from_env(current_env)
        try:
            topology, revision = self._current_topology()
        except Exception as exc:
            # 配置不可用不是授权成功；记审计并拒绝，本层不触发任何 Herdr 操作。
            raise self._reject(msg_id=msg_id, edge_id=str(edge_id), session=session, revision="unavailable",
                               code=REJECT_IDENTITY, reason=f"拓扑不可用: {exc}", env=current_env,
                               cause=exc) from exc

        try:
            identity = AgentIdentity.from_environment(current_env, topology)
            sender = self.registry.resolve_sender(current_env, session=session)
            if sender.identity != identity:
                raise IdentityError("Registry 与当前拓扑解析出的发送方身份不一致")
            if sender.lifecycle != "running":
                raise IdentityError(f"发送方 lifecycle={sender.lifecycle},不是 running")
        except Exception as exc:
            raise self._reject(msg_id=msg_id, edge_id=str(edge_id), session=session, revision=revision,
                               code=REJECT_IDENTITY, reason=f"{type(exc).__name__}: {exc}",
                               env=current_env, cause=exc) from exc

        edge = topology.edges.get(edge_id) if isinstance(edge_id, str) else None
        if edge is None:
            raise self._reject(msg_id=msg_id, edge_id=str(edge_id), session=session, revision=revision,
                               code=REJECT_UNKNOWN_EDGE, reason=f"拓扑里没有通信边 {edge_id!r}",
                               src=sender.agent_id, env=current_env)
        if edge.source_role != sender.role:
            raise self._reject(msg_id=msg_id, edge_id=edge_id, session=session, revision=revision,
                               code=REJECT_WRONG_DIRECTION,
                               reason=f"边 {edge_id} 要求发送角色 {edge.source_role}，实际为 {sender.role}",
                               src=sender.agent_id, env=current_env)

        target_role = edge.target_role
        target_ip = sender.ip_id  # 目标 IP 不可由调用方指定，强制同 IP。
        target_id = f"{target_role}_{target_ip}"
        if not topology.has_node(target_role, target_ip):
            raise self._reject(msg_id=msg_id, edge_id=edge_id, session=session, revision=revision,
                               code=REJECT_TARGET_NOT_IN_TOPOLOGY,
                               reason=f"目标节点 ({target_role}, {target_ip}) 不在拓扑中",
                               src=sender.agent_id, dst=target_id, env=current_env)

        try:
            target: AgentRecord = self.registry.get(target_id)
        except AgentNotRegisteredError as exc:
            raise self._reject(msg_id=msg_id, edge_id=edge_id, session=session, revision=revision,
                               code=REJECT_TARGET_MISSING, reason=f"目标 {target_id} 未登记",
                               src=sender.agent_id, dst=target_id, env=current_env, cause=exc) from exc
        if ((target.project_id, target.ip_id, target.role, target.session) !=
                (sender.project_id, sender.ip_id, target_role, session)):
            raise self._reject(msg_id=msg_id, edge_id=edge_id, session=session, revision=revision,
                               code=REJECT_TARGET_MISSING,
                               reason=f"目标 {target_id} 的 Registry 身份/会话与发送方路由不匹配",
                               src=sender.agent_id, dst=target_id, env=current_env)
        if target.lifecycle != "running":
            raise self._reject(msg_id=msg_id, edge_id=edge_id, session=session, revision=revision,
                               code=REJECT_TARGET_NOT_RUNNING,
                               reason=f"目标 {target_id} lifecycle={target.lifecycle}",
                               src=sender.agent_id, dst=target_id, env=current_env)

        text = edge.template.format(ip=sender.ip_id)
        problem = self._check_text(text)
        if problem:
            raise self._reject(msg_id=msg_id, edge_id=edge_id, session=session, revision=revision,
                               code=REJECT_BAD_MESSAGE, reason=problem, src=sender.agent_id,
                               dst=target_id, env=current_env)

        now = now_iso()
        message = Message(msg_id=msg_id, created_at=now, edge_id=edge_id, src=sender.agent_id,
                          dst=target_id, project_id=sender.project_id, ip_id=sender.ip_id,
                          session=session, text=text, state=QUEUED, topology_revision=revision,
                          updated_at=now)
        self.spool.enqueue(message)
        self.audit.record({"msg_id": msg_id, "edge_id": edge_id, "src": sender.agent_id,
                           "dst": target_id, "state": QUEUED, "detail": "鉴权通过，已入队",
                           "session": session, "topology_revision": revision})
        return SendReceipt(msg_id, edge_id, sender.agent_id, target_id, text, QUEUED, revision)

    def _check_text(self, text: str) -> Optional[str]:
        if not text.strip():
            return "渲染后的消息为空"
        if len(text) > self.max_chars:
            return f"渲染后的消息长度 {len(text)} 超过上限 {self.max_chars}"
        bad = sorted({char for char in text if ord(char) < 32 and char not in "\n\t"})
        if bad:
            return "渲染后的消息包含控制字符: " + ", ".join(repr(char) for char in bad)
        return None

    def status(self, msg_id: str) -> Message:
        return self.spool.get(msg_id)
