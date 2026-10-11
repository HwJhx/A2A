"""按 IP 增删(方案 herdr/claude/17-stage8-ip-add-remove-plan.md §3)。

  add      持 IP 操作锁;排空中拒绝;IP 不在拓扑就加入;按拓扑角色顺序为配置了启动脚本的角色 spawn。
           已 running 的跳过(可重复执行补齐);已登记但 stopped / closed / failed 的只报出,不自动 restore;
           中途失败停下、不回滚。写 IP_ADDED。
  remove   持 IP 操作锁。① 写排空标记(已有则沿用其操作 ID 续做)② 等该 IP 没有 DISPATCHING、agent 都空闲
           (超时 fail-closed:标记保留)③ 检查队列(不满足则撤销标记、恢复正常)④ 逐个 purge(失败停下、标记保留)
           ⑤ IP 还在拓扑才删除 ⑥ 没有该操作 ID 的 IP_REMOVED 才写,然后撤销标记。每一步可重复执行。
  undrain  持 IP 操作锁与排空锁独占,撤销标记(放弃一次删除)。
"""
from __future__ import annotations

import getpass
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import ipdrain
from .audit import AuditLog
from .errors import HerdrNotFound
from .messages import DISPATCHING
from .registry import AgentRecord, Registry
from .spool import Spool, SpoolError
from .topology import TopologyStore

READY = ("idle", "done")


class IpOpError(Exception):
    def __init__(self, message: str, detail: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


def _actor() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return "unknown"


def _shell_only(info: Dict[str, Any]) -> Optional[bool]:
    """pane 是否只剩 shell。herdr 0.9.3 实测:只剩 shell 时 foreground_process_group_id == shell_pid,
    foreground_processes 只有 shell 自己(pid == shell_pid)。字段缺失、类型不对或彼此矛盾返回 None(调用方 fail-closed)。"""
    pgid, shell, foreground = (info.get("foreground_process_group_id"), info.get("shell_pid"),
                               info.get("foreground_processes"))
    if not (isinstance(pgid, int) and isinstance(shell, int) and isinstance(foreground, list) and foreground
            and all(isinstance(p, dict) and isinstance(p.get("pid"), int) for p in foreground)):
        return None
    pids = {p["pid"] for p in foreground}
    if pgid == shell:
        return True if pids == {shell} else None     # 进程组是 shell,列表里却有别的进程:矛盾
    return False if shell not in pids else None      # 前台是别的进程组;列表里还出现 shell 也算矛盾


class IpOps:
    def __init__(self, *, lifecycle: Any, topology: TopologyStore, registry: Registry, spool: Spool,
                 audit: AuditLog, client: Any, state_dir: "str | Path",
                 dispatch_timeout_s: float = 60.0, idle_timeout_s: float = 60.0, poll_s: float = 0.5,
                 sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic,
                 checkpoint: Callable[[str], None] = lambda name: None) -> None:
        self.life = lifecycle
        self.topology = topology
        self.registry = registry
        self.spool = spool
        self.audit = audit
        self.client = client
        self.state_dir = Path(state_dir)
        self.dispatch_timeout_s = dispatch_timeout_s
        self.idle_timeout_s = idle_timeout_s
        self.poll_s = poll_s
        self._sleep = sleep
        self._monotonic = monotonic
        self._checkpoint = checkpoint   # 测试用:在各步之间注入中断

    # ---- 工具 -----------------------------------------------------------
    def _agents(self, ip_id: str) -> List[AgentRecord]:
        return [r for r in self.registry.list() if r.ip_id == ip_id]

    def _dsts(self, ip_id: str) -> List[str]:
        roles = list(self.topology.current().roles)
        return sorted({f"{role}_{ip_id}" for role in roles} | {r.agent_id for r in self._agents(ip_id)})

    def _topology_changed(self, change: Dict[str, Any], before: str) -> None:
        self.topology.reload_if_changed()
        self.audit.record({"state": "TOPOLOGY_CHANGED", "actor": _actor(), "change": change,
                           "previous_revision": before, "topology_revision": self.topology.revision,
                           "detail": "拓扑变更不改写已入队消息(08 §7.2)"})

    def _wait(self, what: str, done: Callable[[], Optional[List[str]]], timeout_s: float) -> None:
        """等 done() 返回空列表;超时抛 IpOpError(列出仍未满足的项)。"""
        deadline = self._monotonic() + timeout_s
        while True:
            pending = done()
            if not pending:
                return
            if self._monotonic() >= deadline:
                raise IpOpError(f"{timeout_s:g} 秒内{what}没有结束:{pending}", {"waiting_for": pending})
            self._sleep(self.poll_s)

    def _named(self, record: AgentRecord) -> Optional[Dict[str, Any]]:
        """按登记的名字找 agent;明确不存在(HerdrNotFound)返回 None,其他 herdr 错误照常抛出。"""
        if not record.agent_name:
            return None
        try:
            return self.client.agent_get(record.agent_name)
        except HerdrNotFound:
            return None

    def _identity_problems(self, ip_id: str) -> List[str]:
        """purge 会关闭每个非 closed 记录的整个 pane。只有下面两种情况关 pane 不会误杀别的进程:
          * pane 已经不在,或 pane 的前台只有 shell(前台进程组就是 shell,或没有前台进程);
          * 前台正是登记的 agent:记录是 running,按登记名字找到的 agent 就在这个 pane 上,pane 上的 agent 也是这个名字。
        其他情况(stopped 记录的 pane 被别的进程占用、herdr 认不出的前台进程、换成了别的 agent)都列为不符。
        查进程信息时 pane 不存在以外的 herdr 错误照常抛出(fail-closed)。"""
        problems = []
        for r in self._agents(ip_id):
            if r.lifecycle == "closed":
                continue
            try:
                info = self.client.pane_process_info(r.pane_id)
            except HerdrNotFound:
                continue
            shell_only = _shell_only(info)
            if shell_only is None:
                problems.append(f"{r.agent_id}(pane {r.pane_id} 的进程信息不完整或自相矛盾,无法确认关 pane 是否安全:{info})")
                continue
            if shell_only:
                continue
            argv = [(p.get("argv") or ["?"])[0] for p in info["foreground_processes"]]
            on_pane = self.client.find_agent(r.pane_id)
            named = self._named(r) if r.lifecycle == "running" else None
            if (r.lifecycle == "running" and named is not None and named.get("pane_id") == r.pane_id
                    and on_pane is not None and on_pane.get("name") == r.agent_name):
                continue
            problems.append(f"{r.agent_id}(lifecycle={r.lifecycle}、登记名字 {r.agent_name}、pane {r.pane_id};"
                            f"前台进程 {argv},pane 上的 agent {on_pane.get('name') if on_pane else None},"
                            f"该名字在 {named.get('pane_id') if named else None})")
        return problems

    # ---- add ------------------------------------------------------------
    def add(self, ip_id: str, *, roles: Optional[List[str]] = None, cwd: Optional[str] = None) -> Dict[str, Any]:
        with ipdrain.op_lock(self.state_dir, ip_id):
            if ipdrain.is_draining(self.state_dir, ip_id):
                raise IpOpError(f"IP {ip_id} 正在排空(ip remove 未完成):先完成 ip remove,或执行 a2a ip undrain {ip_id}")
            topology = self.topology.current()
            all_roles = list(topology.roles)
            unknown = [r for r in roles or [] if r not in topology.roles]
            if unknown:
                raise IpOpError(f"拓扑里没有这些角色:{unknown}")
            if ip_id not in topology.ips:
                before = self.topology.revision
                self.topology.add_ip(ip_id)
                self._topology_changed({"op": "add-ip", "ip": ip_id}, before)
                topology = self.topology.current()
            results: Dict[str, str] = {}
            try:
                for role in roles or all_roles:
                    record = self.registry.find(role, ip_id)
                    if record is not None:
                        if record.lifecycle != "running":
                            results[role] = f"registered_{record.lifecycle}(需要 a2a agent restore)"
                        else:
                            # 按登记的名字找 agent,并核对它在登记的 pane 上(pane 里可能换成了别的 agent)
                            named = self._named(record)   # 只有 HerdrNotFound 算不存在,其他 herdr 错误照常上报
                            if named is None:
                                results[role] = "registered_running_but_missing(需要 a2a agent restore)"
                            elif named.get("pane_id") != record.pane_id:
                                results[role] = (f"registered_running_but_mismatched(名字 {record.agent_name} 在 "
                                                 f"{named.get('pane_id')},登记的是 {record.pane_id};需要人工确认)")
                            else:
                                results[role] = "already_running"
                        continue
                    if not topology.roles[role].launcher:
                        results[role] = "no_launcher"
                        continue
                    role_cwd = None
                    if cwd:
                        role_cwd = cwd.replace("{ip}", ip_id).replace("{role}", role)
                        Path(role_cwd).mkdir(parents=True, exist_ok=True)
                    try:
                        self.life.spawn(role, ip_id, cwd=role_cwd)
                    except Exception as exc:  # noqa: BLE001 —— 停下、不回滚,修好后再执行一次补齐
                        results[role] = f"failed: {type(exc).__name__}: {exc}"
                        raise IpOpError(f"{role}_{ip_id} 启动失败,已停止;修好后再执行 ip add 补齐", {"roles": results})
                    results[role] = "spawned"
            finally:
                self.audit.record({"state": "IP_ADDED", "actor": _actor(), "ip": ip_id, "roles": results,
                                   "detail": "全部完成" if all(not v.startswith("failed") for v in results.values())
                                   else "部分失败,未回滚"})
            return {"ip": ip_id, "roles": results}

    # ---- remove ---------------------------------------------------------
    def remove(self, ip_id: str, *, force: bool = False) -> Dict[str, Any]:
        with ipdrain.op_lock(self.state_dir, ip_id):
            if (ip_id not in self.topology.current().ips and not self._agents(ip_id)
                    and not ipdrain.is_draining(self.state_dir, ip_id)):
                raise IpOpError(f"IP {ip_id} 不存在(不在拓扑里、没有登记的 agent、也没有未完成的删除)")
            # ① 排空标记
            with ipdrain.drain_exclusive(self.state_dir, ip_id):
                marker = ipdrain.read_marker(self.state_dir, ip_id)
                op_id = marker.get("op_id") if marker else None
                if not op_id:
                    op_id = uuid.uuid4().hex[:12]
                    ipdrain.write_marker(self.state_dir, ip_id, op_id, "ip remove")
                    self.audit.record({"state": "IP_DRAIN_STARTED", "actor": _actor(), "ip": ip_id, "op_id": op_id,
                                       "detail": "续做时标记读不出,重写" if marker else "开始排空"})
            self._checkpoint("drained")
            dsts = self._dsts(ip_id)

            # ② 等在途结束(超时 fail-closed:标记保留)
            def dispatching() -> List[str]:
                return [m.msg_id for d in dsts for m in self.spool.pending(d) if m.state == DISPATCHING]
            self._wait("DISPATCHING 的投递", dispatching, self.dispatch_timeout_s)
            # 核对身份:pane 上的 agent 必须是登记的那个(或 pane 上已经没有 agent)。对不上就拒绝,
            # 标记保留(IP 保持隔离)、等人工确认;--force 不跳过这一项,否则 purge 关 pane 会误杀替代的 agent
            mismatched = self._identity_problems(ip_id)
            if mismatched:
                raise IpOpError(f"IP {ip_id} 有 pane 上的 agent 与登记不符,不能删除;排空标记保留,"
                                f"人工确认后再执行 ip remove,或 a2a ip undrain {ip_id} 放弃",
                                {"mismatched": mismatched})
            if not force:
                def busy() -> List[str]:
                    out = []
                    for r in self._agents(ip_id):
                        if r.lifecycle != "running":
                            continue
                        found = self.client.find_agent(r.pane_id)
                        # 只有明确 idle / done 才算空闲;查不到(未识别、暂时查不到、进程已退出)一律按忙处理(fail-closed)
                        if found is None:
                            out.append(f"{r.agent_id}(herdr 查不到;进程已退出时用 --force)")
                        elif found.get("agent_status") not in READY:
                            out.append(f"{r.agent_id}({found.get('agent_status')})")
                    return out
                self._wait("agent 的工作", busy, self.idle_timeout_s)
            self._checkpoint("quiet")

            # ③ 检查队列;不满足就撤销标记、恢复正常
            problems: Dict[str, Any] = {}
            try:
                targets = set(self.spool.queue_targets())
                for d in dsts:
                    pending = [f"{m.msg_id}({m.state})" for m in self.spool.pending(d)]
                    if pending or d in targets:
                        problems[d] = pending or ["有未放行的槽位或队列状态读不出(见 a2a queue)"]
            except SpoolError as exc:
                problems["spool"] = [str(exc)]
            if problems:
                with ipdrain.drain_exclusive(self.state_dir, ip_id):
                    ipdrain.clear_marker(self.state_dir, ip_id)
                self.audit.record({"state": "IP_DRAIN_CANCELLED", "actor": _actor(), "ip": ip_id, "op_id": op_id,
                                   "problems": problems, "detail": "队列里还有未处理的消息,已撤销排空、恢复正常"})
                raise IpOpError(f"IP {ip_id} 的队列里还有未处理的消息,已撤销排空;先用 a2a resolve 处理后再删除",
                                {"problems": problems})

            # ④ purge(失败停下、标记保留)
            for record in self._agents(ip_id):
                self.life.purge(record.role, record.ip_id)
                self._checkpoint(f"purged:{record.agent_id}")

            # ⑤ 改拓扑(已不在就跳过)
            if ip_id in self.topology.current().ips:
                before = self.topology.revision
                self.topology.remove_ip(ip_id)
                self._topology_changed({"op": "remove-ip", "ip": ip_id}, before)
            self._checkpoint("topology_removed")

            # ⑥ 收尾:先审计(按操作 ID 去重),再撤销标记
            if not any(e.get("state") == "IP_REMOVED" and e.get("op_id") == op_id for e in self.audit.read()):
                self.audit.record({"state": "IP_REMOVED", "actor": _actor(), "ip": ip_id, "op_id": op_id,
                                   "detail": "已清除该 IP 的 agent 并从拓扑删除;spool 历史保留"})
            self._checkpoint("audited")
            with ipdrain.drain_exclusive(self.state_dir, ip_id):
                ipdrain.clear_marker(self.state_dir, ip_id)
            return {"ip": ip_id, "op_id": op_id, "removed": True}

    # ---- undrain --------------------------------------------------------
    def undrain(self, ip_id: str) -> Dict[str, Any]:
        with ipdrain.op_lock(self.state_dir, ip_id):
            with ipdrain.drain_exclusive(self.state_dir, ip_id):
                marker = ipdrain.read_marker(self.state_dir, ip_id)
                if marker is None:
                    return {"ip": ip_id, "undrained": False, "detail": "没有排空标记"}
                ipdrain.clear_marker(self.state_dir, ip_id)
            self.audit.record({"state": "IP_UNDRAINED", "actor": _actor(), "ip": ip_id,
                               "op_id": marker.get("op_id"), "detail": "撤销排空,恢复正常"})
            return {"ip": ip_id, "undrained": True, "op_id": marker.get("op_id")}
