"""Router:发送方鉴权 + 拓扑校验 + 固定模板渲染 + 入队(04 号文档 §3.1)。

Router 是 agent 发消息的唯一入口,不碰 herdr(任何一步失败都**不执行任何 herdr 命令**)。
它只做三件事:判定谁在发、判定这条跳转是否被拓扑允许、把渲染好的固定句式写进持久队列。
真正的"等目标空闲再投递"由阶段 5 的 broker 完成。

接口里刻意没有"目标 IP""目标名字""自由文本"这几个参数:
  * 目标 = (边的 to 角色, 发送方自己的 IP),所以同 IP 铁律在接口形态上就无法违反
  * 消息 = 边上配置的模板,只填入发送方的 IP

校验顺序(任一步失败即拒绝,写审计日志,不入队):
  1. 发送方身份 identity.resolve_sender
  2. 边存在
  3. 边的 from 角色 == 发送方角色(方向正确)
  4. 目标节点 (to 角色, 同 IP) 在当前拓扑里
  5. 目标已登记,且是 running
  6. 渲染模板并检查长度、控制字符
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from .audit import AuditLog
from .identity import (
    ENV_IP,
    ENV_PANE,
    ENV_PROJECT,
    ENV_ROLE,
    IdentityError,
    resolve_sender,
)
from .messages import QUEUED, REJECTED, Message, new_msg_id, now_iso
from .registry import Registry
from .spool import Spool
from .topology import Topology, TopologyStore

# 拒绝原因代码
REJECT_IDENTITY = "identity"
REJECT_UNKNOWN_EDGE = "unknown_edge"
REJECT_WRONG_DIRECTION = "wrong_direction"
REJECT_TARGET_NOT_IN_TOPOLOGY = "target_not_in_topology"
REJECT_TARGET_MISSING = "target_missing"
REJECT_TARGET_NOT_RUNNING = "target_not_running"
REJECT_BAD_MESSAGE = "bad_message"

MAX_MESSAGE_CHARS = 1000


class SendRejected(Exception):
    """发送被拒绝。code 是上面的原因代码;msg_id 用于在审计日志里追踪这次尝试。"""

    def __init__(self, code: str, reason: str, *, msg_id: str, cause: Optional[BaseException] = None) -> None:
        super().__init__(f"[{code}] {reason}")
        self.code = code
        self.reason = reason
        self.msg_id = msg_id
        self.cause = cause


@dataclass(frozen=True)
class SendReceipt:
    """发送成功(已入队)的回执。发完即走:不代表对方已经收到。"""

    msg_id: str
    edge_id: str
    src: str
    dst: str
    text: str
    state: str
    topology_revision: str
    queue_seq: int


def session_from_env(env: Mapping[str, str]) -> str:
    """从 pane 的环境变量得到 herdr 会话名。

    实测:命名会话里的 pane 带有 HERDR_SESSION(例如 rt_probe)。默认会话里的取值没有测过,
    没有该变量时按 "default" 处理。
    """
    return env.get("HERDR_SESSION") or "default"


class Router:
    def __init__(
        self,
        topology: "TopologyStore | Topology",
        registry: Registry,
        spool: Spool,
        audit: AuditLog,
        *,
        session: Optional[str] = None,
        max_chars: int = MAX_MESSAGE_CHARS,
    ) -> None:
        self._topology_source = topology
        self.registry = registry
        self.spool = spool
        self.audit = audit
        self._session = session
        self.max_chars = max_chars

    # ------------------------------------------------------------------
    def _topology(self) -> "tuple[Topology, str]":
        """取当前拓扑(TopologyStore 会先检查文件是否被改动)和它的修订号。"""
        if isinstance(self._topology_source, TopologyStore):
            return self._topology_source.current(), self._topology_source.revision
        return self._topology_source, "static"

    def send(self, edge_id: str, env: Optional[Mapping[str, str]] = None) -> SendReceipt:
        """发送一条固定句式消息。成功返回回执(已入队),失败抛 SendRejected。

        env 是发送方进程的环境变量(默认取 os.environ)。发送方身份只能从它推出,
        调用方无法指定"我是谁"或"发给谁"。
        """
        env = os.environ if env is None else env
        msg_id = new_msg_id()
        topology, revision = self._topology()
        session = self._session or session_from_env(env)
        claimed = {
            "claimed_project": env.get(ENV_PROJECT), "claimed_role": env.get(ENV_ROLE),
            "claimed_ip": env.get(ENV_IP), "claimed_pane": env.get(ENV_PANE),
        }

        def reject(code: str, reason: str, *, src: Optional[str] = None, dst: Optional[str] = None,
                   cause: Optional[BaseException] = None) -> SendRejected:
            event: Dict[str, Any] = {
                "msg_id": msg_id, "edge_id": edge_id, "state": REJECTED, "reject_code": code,
                "detail": reason, "src": src, "dst": dst, "session": session,
                "topology_revision": revision,
            }
            if src is None:
                event.update(claimed)  # 身份未核实时,记录它声称的身份(不可信,仅供排查)
            self.audit.record(event)
            return SendRejected(code, reason, msg_id=msg_id, cause=cause)

        # 1. 发送方身份
        try:
            sender = resolve_sender(env, self.registry, topology, session=session)
        except IdentityError as exc:
            raise reject(REJECT_IDENTITY, f"{type(exc).__name__}: {exc}", cause=exc) from exc

        # 2. 边存在
        edge = topology.edges.get(edge_id)
        if edge is None:
            raise reject(REJECT_UNKNOWN_EDGE, f"拓扑里没有通信边 {edge_id!r}", src=sender.agent_id)

        # 3. 方向正确
        if edge.source_role != sender.role:
            raise reject(
                REJECT_WRONG_DIRECTION,
                f"边 {edge_id} 只允许 {edge.source_role} 作为发送方,而发送方角色是 {sender.role}",
                src=sender.agent_id,
            )

        # 4. 目标 = (边的 to 角色, 发送方自己的 IP)。同 IP 铁律:这里没有任何参数能改变目标 IP。
        target_role, target_ip = edge.target_role, sender.ip_id
        dst = f"{target_role}_{target_ip}"
        if not topology.has_node(target_role, target_ip):
            raise reject(REJECT_TARGET_NOT_IN_TOPOLOGY,
                         f"目标节点 ({target_role}, {target_ip}) 不在当前拓扑里", src=sender.agent_id, dst=dst)

        # 5. 目标已登记且在运行
        target = self.registry.find(target_role, target_ip)
        if target is None:
            raise reject(REJECT_TARGET_MISSING, f"目标 {dst} 没有登记的 agent", src=sender.agent_id, dst=dst)
        if target.session != session:
            # 目标登记在另一个 herdr 会话里:broker 只在一个会话内工作,消息投递不到那里。拒绝比入队后失败更早暴露问题
            raise reject(REJECT_TARGET_MISSING,
                         f"目标 {dst} 登记在会话 {target.session!r},与发送方所在会话 {session!r} 不同",
                         src=sender.agent_id, dst=dst)
        if target.ip_id != sender.ip_id or target.project_id != sender.project_id:
            # 按构造不可能发生;万一注册表被破坏,宁可拒绝也不跨 IP 发送
            raise reject(REJECT_TARGET_MISSING, f"目标 {dst} 的登记信息与发送方不属于同一 IP / 项目",
                         src=sender.agent_id, dst=dst)
        if target.lifecycle != "running":
            raise reject(REJECT_TARGET_NOT_RUNNING, f"目标 {dst} 的状态是 {target.lifecycle},不是 running",
                         src=sender.agent_id, dst=dst)

        # 6. 渲染固定句式。模板在拓扑加载时已校验过,只可能含 {ip}
        text = edge.template.format(ip=sender.ip_id)
        problem = self._check_text(text)
        if problem:
            raise reject(REJECT_BAD_MESSAGE, problem, src=sender.agent_id, dst=dst)

        message = Message(
            msg_id=msg_id, created_at=now_iso(), edge_id=edge_id, src=sender.agent_id, dst=dst,
            project_id=sender.project_id, ip_id=sender.ip_id, session=session, text=text,
            state=QUEUED, topology_revision=revision, attempts=0, detail="", updated_at=now_iso(),
        )
        stored = self.spool.enqueue(message)
        self.audit.record({
            "msg_id": msg_id, "edge_id": edge_id, "src": message.src, "dst": message.dst,
            "state": QUEUED, "detail": "鉴权通过,已入队", "session": session,
            "topology_revision": revision, "queue_seq": stored.queue_seq,
        })
        return SendReceipt(msg_id, edge_id, message.src, message.dst, text, QUEUED, revision,
                           stored.queue_seq)

    def _check_text(self, text: str) -> Optional[str]:
        if not text.strip():
            return "渲染后的消息为空"
        if len(text) > self.max_chars:
            return f"渲染后的消息有 {len(text)} 个字符,超过上限 {self.max_chars}"
        bad = sorted({c for c in text if ord(c) < 32 and c not in "\n\t"})
        if bad:
            return "渲染后的消息含有控制字符: " + ", ".join(repr(c) for c in bad)
        return None

    # ------------------------------------------------------------------
    def status(self, msg_id: str) -> Message:
        """查询一条已入队消息的状态(`a2a status <msg_id>` 的底层)。被拒绝的消息不入队,请查审计日志。"""
        return self.spool.get(msg_id)
