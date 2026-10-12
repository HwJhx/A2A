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

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from . import ipdrain
from ._fsutil import atomic_write_text, exclusive_lock
from .audit import AuditLog
from .identity import (
    ENV_IP,
    ENV_PANE,
    ENV_PROJECT,
    ENV_ROLE,
    AgentIdentity,
    IdentityError,
    resolve_sender,
)
from .messages import QUEUED, REJECTED, Message, new_msg_id, now_iso
from .registry import Registry
from .spool import MessageNotFoundError, Spool
from .topology import Topology, TopologyStore

# 拒绝原因代码
REJECT_IDENTITY = "identity"
REJECT_UNKNOWN_EDGE = "unknown_edge"
REJECT_WRONG_DIRECTION = "wrong_direction"
REJECT_TARGET_NOT_IN_TOPOLOGY = "target_not_in_topology"
REJECT_TARGET_MISSING = "target_missing"
REJECT_TARGET_NOT_RUNNING = "target_not_running"
REJECT_BAD_MESSAGE = "bad_message"
REJECT_IP_DRAINING = "ip_draining"
REJECT_BAD_KEY = "bad_idempotency_key"
REJECT_IDEMPOTENCY_EXPIRED = "idempotency_expired"
REJECT_IDEMPOTENCY_CONFLICT = "idempotency_conflict"

# 幂等键(路线图 336):插件每次开始处理新输入时生成 `<UTC 秒>-<UUID>`。超过有效期的键被拒绝;
# 幂等记录就是判重的"墓碑",只有键已过期的记录才可以清理(清理后同键再来会因过期被拒)
IDEMPOTENCY_TTL_S = 7 * 24 * 3600
_KEY = re.compile(r"^(\d{10})-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_KEY_FUTURE_SKEW_S = 300
SEND_DEDUPLICATED = "SEND_DEDUPLICATED"

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
    duplicate: bool = False     # 同一幂等键的重复调用:没有入队,返回的是第一次的消息


def session_from_env(env: Mapping[str, str]) -> str:
    """从 pane 的环境变量得到 herdr 会话名。

    实测:命名会话里的 pane 带有 HERDR_SESSION(例如 rt_probe)。默认会话里的取值没有测过,
    没有该变量时按 "default" 处理。
    """
    return env.get("HERDR_SESSION") or "default"


class _SendContext:
    """一次 send 调用的上下文:拓扑快照、会话、msg_id,以及写拒绝审计的 reject()。"""

    def __init__(self, router: "Router", edge_id: str, env: Mapping[str, str]) -> None:
        self.router, self.edge_id, self.env = router, edge_id, env
        self.msg_id = new_msg_id()
        self.topology, self.revision = router._topology()
        self.session = router._session or session_from_env(env)

    def reject(self, code: str, reason: str, *, src: Optional[str] = None, dst: Optional[str] = None,
               cause: Optional[BaseException] = None) -> SendRejected:
        event: Dict[str, Any] = {
            "msg_id": self.msg_id, "edge_id": self.edge_id, "state": REJECTED, "reject_code": code,
            "detail": reason, "src": src, "dst": dst, "session": self.session,
            "topology_revision": self.revision,
        }
        if src is None:
            # 身份未核实时,记录它声称的身份(不可信,仅供排查)
            event.update({"claimed_project": self.env.get(ENV_PROJECT), "claimed_role": self.env.get(ENV_ROLE),
                          "claimed_ip": self.env.get(ENV_IP), "claimed_pane": self.env.get(ENV_PANE)})
        self.router.audit.record(event)
        return SendRejected(code, reason, msg_id=self.msg_id, cause=cause)

    def resolve_sender(self) -> AgentIdentity:
        """校验 1:发送方身份。"""
        try:
            return resolve_sender(self.env, self.router.registry, self.topology, session=self.session)
        except IdentityError as exc:
            raise self.reject(REJECT_IDENTITY, f"{type(exc).__name__}: {exc}", cause=exc) from exc


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
        state_dir: "Optional[str | Path]" = None,
    ) -> None:
        self._topology_source = topology
        # 排空标记与 IP 排空锁所在的状态目录;spool 固定在 <状态目录>/spool,默认取它的上一级
        self.state_dir = Path(state_dir) if state_dir is not None else spool.root.parent
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

    def send(self, edge_id: str, env: Optional[Mapping[str, str]] = None, *,
             idempotency_key: Optional[str] = None) -> SendReceipt:
        """发送一条固定句式消息。成功返回回执(已入队),失败抛 SendRejected。

        env 是发送方进程的环境变量(默认取 os.environ)。发送方身份只能从它推出,
        调用方无法指定"我是谁"或"发给谁"。

        idempotency_key(路线图 336,pi 插件每次处理新输入时生成):同一发送方、同一条边、同一个键
        只入队一次;重复调用返回第一次的 msg_id,duplicate=True。不带键时行为不变。
        """
        env = os.environ if env is None else env
        ctx = _SendContext(self, edge_id, env)
        sender = ctx.resolve_sender()
        if idempotency_key is None:
            dst, text = self._authorize(ctx, sender)
            return self._enqueue(ctx, sender, ctx.msg_id, dst, text)
        return self._send_idempotent(ctx, sender, idempotency_key)

    # ---- 鉴权与入队 ------------------------------------------------------
    def _authorize(self, ctx: "_SendContext", sender: AgentIdentity) -> "tuple[str, str]":
        """校验 2–6(边、方向、目标、模板);返回 (dst, text)。失败写审计并抛 SendRejected。"""
        topology, edge_id, reject = ctx.topology, ctx.edge_id, ctx.reject

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
        if target.session != ctx.session:
            # 目标登记在另一个 herdr 会话里:broker 只在一个会话内工作,消息投递不到那里。拒绝比入队后失败更早暴露问题
            raise reject(REJECT_TARGET_MISSING,
                         f"目标 {dst} 登记在会话 {target.session!r},与发送方所在会话 {ctx.session!r} 不同",
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
        return dst, text

    def _enqueue(self, ctx: "_SendContext", sender: AgentIdentity, msg_id: str, dst: str, text: str, *,
                 revision: Optional[str] = None, on_enqueued: Optional[Callable[[Any], None]] = None) -> SendReceipt:
        revision = ctx.revision if revision is None else revision
        message = Message(
            msg_id=msg_id, created_at=now_iso(), edge_id=ctx.edge_id, src=sender.agent_id, dst=dst,
            project_id=sender.project_id, ip_id=sender.ip_id, session=ctx.session, text=text,
            state=QUEUED, topology_revision=revision, attempts=0, detail="", updated_at=now_iso(),
        )
        # 检查排空标记与入队在 IP 排空锁(共享)里完成:标记写入方持独占锁,所以两者不会交错(17 号方案 §3.2)
        with ipdrain.drain_shared(self.state_dir, sender.ip_id):
            if ipdrain.is_draining(self.state_dir, sender.ip_id):
                raise ctx.reject(REJECT_IP_DRAINING,
                                 f"IP {sender.ip_id} 正在排空(ip remove 进行中),暂不接受该 IP 的消息",
                                 src=sender.agent_id, dst=dst)
            stored = self.spool.enqueue(message)
        if on_enqueued is not None:
            on_enqueued(stored)
        self._record_queued(ctx, stored)
        return SendReceipt(msg_id, ctx.edge_id, message.src, message.dst, text, QUEUED, revision,
                           stored.queue_seq)

    def _record_queued(self, ctx: "_SendContext", stored: Message) -> None:
        self.audit.record({
            "msg_id": stored.msg_id, "edge_id": stored.edge_id, "src": stored.src, "dst": stored.dst,
            "state": QUEUED, "detail": "鉴权通过,已入队", "session": stored.session,
            "topology_revision": stored.topology_revision, "queue_seq": stored.queue_seq,
        })

    # ---- 幂等发送(路线图 336) --------------------------------------------
    def _send_idempotent(self, ctx: "_SendContext", sender: AgentIdentity, key: str) -> SendReceipt:
        """先写意图、再补齐的可恢复流程(19 号方案 D 节 3):
          ① 没有记录 → 鉴权,把已鉴权的消息快照(含 msg_id)写成 intent;
          ② intent → 按 msg_id 查 spool:不在就按当前拓扑重新鉴权,与快照一致才用快照入队,否则
             idempotency_conflict;已在就不入队。之后改为 enqueued;
          ③ 入队审计缺失就补记;重复调用另记 SEND_DEDUPLICATED。
        任何一步崩溃后用同一个键重试,都只会有一条消息、msg_id 不变。"""
        self._check_key(ctx, sender, key)
        record_dir = self.state_dir / "idempotency" / sender.agent_id
        path = record_dir / (hashlib.sha256(f"{ctx.edge_id}\0{key}".encode()).hexdigest() + ".json")
        with exclusive_lock(self.state_dir / "idempotency" / f"{sender.agent_id}.lock"):
            record = json.loads(path.read_text()) if path.exists() else None
            if record is not None and record.get("state") == "enqueued":
                return self._duplicate(ctx, record)
            fresh = record is None              # 刚鉴权并写下意图:入队前不必再鉴权一次
            if record is None:
                self._checkpoint("before_intent")
                dst, text = self._authorize(ctx, sender)
                record = {"state": "intent", "key": key, "created_at": now_iso(),
                          "message": {"msg_id": ctx.msg_id, "src": sender.agent_id, "dst": dst,
                                      "edge_id": ctx.edge_id, "text": text, "session": ctx.session,
                                      "project_id": sender.project_id, "ip_id": sender.ip_id,
                                      "topology_revision": ctx.revision}}
                record_dir.mkdir(parents=True, exist_ok=True)
                atomic_write_text(path, json.dumps(record, ensure_ascii=False, indent=2))
                self._checkpoint("after_intent")
            snap = record["message"]
            try:
                stored = self.spool.get(snap["msg_id"])
            except MessageNotFoundError:
                stored = None
            if stored is None:
                ctx.msg_id = snap["msg_id"]     # 审计里的拒绝记录也用这条消息的 msg_id
                dst, text = (snap["dst"], snap["text"]) if fresh else self._authorize(ctx, sender)
                if (dst, text) != (snap["dst"], snap["text"]):
                    raise ctx.reject(REJECT_IDEMPOTENCY_CONFLICT,
                                     f"补齐入队时内容与第一次鉴权的不一致(第一次:{snap['dst']}「{snap['text']}」;"
                                     f"现在:{dst}「{text}」),不按任何一方发送,请操作员处理",
                                     src=sender.agent_id, dst=dst)
                receipt = self._enqueue(ctx, sender, snap["msg_id"], snap["dst"], snap["text"],
                                        revision=snap["topology_revision"],
                                        on_enqueued=lambda _: self._checkpoint("after_enqueue"))
                queue_seq = receipt.queue_seq
            else:
                self._ensure_queued_audit(ctx, stored)
                queue_seq = stored.queue_seq
                receipt = None
            record.update(state="enqueued", queue_seq=queue_seq, enqueued_at=now_iso())
            atomic_write_text(path, json.dumps(record, ensure_ascii=False, indent=2))
            self._checkpoint("after_mark")
            if receipt is not None:
                return receipt
            return SendReceipt(snap["msg_id"], ctx.edge_id, snap["src"], snap["dst"], snap["text"], QUEUED,
                               snap["topology_revision"], queue_seq)

    def _duplicate(self, ctx: "_SendContext", record: Dict[str, Any]) -> SendReceipt:
        """同键的重复调用:不入队(不再查 spool,消息可能已被归档),补记缺失的入队审计,记 SEND_DEDUPLICATED。"""
        snap = record["message"]
        try:
            self._ensure_queued_audit(ctx, self.spool.get(snap["msg_id"]))
        except MessageNotFoundError:
            pass                                # 已被归档:入队审计早已写过(归档前消息走完了投递)
        self.audit.record({"msg_id": snap["msg_id"], "edge_id": ctx.edge_id, "src": snap["src"], "dst": snap["dst"],
                           "state": SEND_DEDUPLICATED, "detail": "同一幂等键的重复调用,没有入队",
                           "session": ctx.session, "topology_revision": snap["topology_revision"]})
        return SendReceipt(snap["msg_id"], ctx.edge_id, snap["src"], snap["dst"], snap["text"], QUEUED,
                           snap["topology_revision"], record["queue_seq"], duplicate=True)

    def _ensure_queued_audit(self, ctx: "_SendContext", stored: Message) -> None:
        """入队后、写审计前崩溃:补记入队审计(按 msg_id 查;只在补齐 / 重复时才读审计)。"""
        if not any(e.get("msg_id") == stored.msg_id and e.get("state") == QUEUED for e in self.audit.read()):
            self._record_queued(ctx, stored)

    def _check_key(self, ctx: "_SendContext", sender: AgentIdentity, key: str) -> None:
        m = _KEY.match(key)
        if not m:
            raise ctx.reject(REJECT_BAD_KEY, f"幂等键格式不对(应为 <UTC 秒>-<UUID>):{key[:80]!r}",
                             src=sender.agent_id)
        age = time.time() - int(m.group(1))
        if age > IDEMPOTENCY_TTL_S:
            raise ctx.reject(REJECT_IDEMPOTENCY_EXPIRED,
                             f"幂等键已超过有效期({IDEMPOTENCY_TTL_S // 86400} 天),不入队", src=sender.agent_id)
        if age < -_KEY_FUTURE_SKEW_S:
            raise ctx.reject(REJECT_BAD_KEY, "幂等键的时间在未来", src=sender.agent_id)

    def _checkpoint(self, name: str) -> None:
        """测试用:在幂等流程的各步之间注入崩溃。"""
        hook = getattr(self, "crash_hook", None)
        if hook is not None:
            hook(name)

    def edges(self, env: Optional[Mapping[str, str]] = None) -> List[Dict[str, str]]:
        """env 所声称的角色可以使用的边,以及每条边按其 IP 渲染后的完整文字。

        只读,不入队、不写审计;供 pi 插件生成 a2a_send 的取值与说明。插件在 agent 启动时加载,
        那时 spawn 还没登记注册表,所以这里只要求环境变量里的 (项目, 角色, IP) 在当前拓扑里,
        **不**核实注册表与 pane。真正发送时 send() 仍做完整的身份核实,列表本身不授予任何权限。
        目标节点不在拓扑里的边不列出(发了也会被拒绝)。身份不在拓扑里抛 SendRejected。
        """
        env = os.environ if env is None else env
        topology, _ = self._topology()
        try:
            identity = AgentIdentity.from_environment(env, topology)
        except IdentityError as exc:
            raise SendRejected(REJECT_IDENTITY, f"{type(exc).__name__}: {exc}", msg_id="", cause=exc) from exc
        return [{"edge_id": edge.edge_id, "dst": f"{edge.target_role}_{identity.ip_id}",
                 "text": edge.template.format(ip=identity.ip_id)}
                for edge in topology.edges_from(identity.role)
                if topology.has_node(edge.target_role, identity.ip_id)]

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
