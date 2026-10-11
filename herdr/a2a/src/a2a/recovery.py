"""注册表自动恢复(方案 herdr/claude/18-stage8-registry-recovery-plan.md §4.1)。

VM / herdr 会话重启后,注册表里记为 running 的 agent 已不在(herdr 保留 pane 与 pane ID,但进程退出、
名字丢失,见 §3.1)。recover 只处理指定会话的记录,分类后逐个 restore:

  ok                 running,按登记名字找到的 agent 就在登记的 pane 上 -> 不动
  restore            running(或 --include-stopped 时 stopped),名字找不到,且原 pane 明确只剩 shell
                     (原 pane 重启,herdr 保留了 pane 的工作目录)或 pane 不在且注册表登记了工作目录(新建 pane)-> 恢复
  uncertain          pane 有别的前台进程、进程信息不完整 / 矛盾、名字在别的 pane 上、
                     或 pane 不在而注册表没有工作目录(旧记录)-> 不动,人工处理(原因在 notes 里)
  classification_failed  查询 herdr 出错 -> 这一个不处理,其他继续
  kept_stopped       stopped(未加 --include-stopped)-> 按原意保留
  kept_closed        closed -> 按原意保留
  failed_record      failed -> 报出,不自动重试
  skipped_draining   所在 IP 正在排空 -> 跳过
  other_session      属于其他会话 -> 不查询、不处理

前提:会话在运行;状态目录没有运行中的 broker(以非阻塞方式拿 broker 单实例锁,全程持有,恢复期间 broker 也启动不了)。
--dry-run 只分类、不拿锁、不改动(结果只是那一刻的快照)。
"""
from __future__ import annotations

import fcntl
import getpass
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import ipdrain
from .audit import AuditLog
from .errors import HerdrNotFound
from .lifecycle import pane_shell_only
from .messages import DELIVERY_UNCERTAIN, TARGET_MISSING
from .registry import AgentRecord, Registry
from .spool import Spool, SpoolError


PROBLEM_KINDS = ("uncertain", "failed_record", "classification_failed")


class BrokerRunning(RuntimeError):
    pass


@contextmanager
def broker_lock(state_dir: Path) -> Iterator[None]:
    """与 `a2a broker run` 相同的单实例锁(<状态目录>/broker.lock);拿不到说明 broker 在运行。"""
    path = Path(state_dir) / "broker.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BrokerRunning(f"状态目录 {state_dir} 有运行中的 broker:请先停止 broker 再执行 recover") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Recovery:
    def __init__(self, *, lifecycle: Any, registry: Registry, spool: Spool, audit: AuditLog, client: Any,
                 state_dir: "str | Path", session: str, role_order: Optional[List[str]] = None) -> None:
        self.life = lifecycle
        self.registry = registry
        self.spool = spool
        self.audit = audit
        self.client = client
        self.state_dir = Path(state_dir)
        self.session = session
        self.role_order = role_order or []

    def _classify(self, r: AgentRecord, include_stopped: bool) -> Tuple[str, Optional[str]]:
        """返回 (分类, 说明)。查询 herdr 出错只影响这一个 agent(Codex 审核)。"""
        try:
            return self._classify_one(r, include_stopped)
        except Exception as exc:  # noqa: BLE001
            return "classification_failed", f"{type(exc).__name__}: {exc}"

    def _classify_one(self, r: AgentRecord, include_stopped: bool) -> Tuple[str, Optional[str]]:
        if r.session != self.session:
            return "other_session", None
        if ipdrain.is_draining(self.state_dir, r.ip_id):
            return "skipped_draining", None
        if r.lifecycle == "closed":
            return "kept_closed", None
        if r.lifecycle == "failed":
            return "failed_record", None
        if r.lifecycle == "stopped" and not include_stopped:
            return "kept_stopped", None
        if r.lifecycle not in ("running", "stopped"):
            return "uncertain", f"未知的 lifecycle {r.lifecycle!r}"
        named = None
        if r.agent_name:
            try:
                named = self.client.agent_get(r.agent_name)
            except HerdrNotFound:
                named = None
        if named is not None:
            if r.lifecycle == "running" and named.get("pane_id") == r.pane_id:
                return "ok", None
            return "uncertain", f"名字 {r.agent_name} 在 pane {named.get('pane_id')} 上仍有 agent"
        try:
            info = self.client.pane_process_info(r.pane_id)
        except HerdrNotFound:
            # pane 不在:restore 会新建 pane,必须知道工作目录,否则可能在错误目录启动却登记为 running
            if r.cwd is None:
                return "uncertain", "pane 已不在,注册表没有登记工作目录;请用 a2a agent restore --cwd 手工恢复"
            return "restore", f"新建 pane,工作目录 {r.cwd}"
        shell_only = pane_shell_only(info)
        if shell_only is True:
            return "restore", f"原 pane {r.pane_id} 重启"
        return "uncertain", (f"pane {r.pane_id} 前台有别的进程" if shell_only is False
                             else f"pane {r.pane_id} 进程信息不完整或自相矛盾")

    def _queue_hints(self, agent_ids: List[str]) -> List[Dict[str, str]]:
        hints = []
        for dst in agent_ids:
            try:
                head = self.spool.head(dst)
            except SpoolError as exc:
                hints.append({"dst": dst, "problem": f"队列读不出:{exc}"})
                continue
            if head is None:
                continue
            message = head.active or head.last_terminal
            if message is not None and message.state in (TARGET_MISSING, DELIVERY_UNCERTAIN):
                action = "retry" if message.state == TARGET_MISSING else "delivered|retry|abandon"
                hints.append({"dst": dst, "msg_id": message.msg_id, "state": message.state,
                              "suggest": f"a2a resolve {message.msg_id} {action} --reason ..."})
        return hints

    def recover(self, *, ip: Optional[str] = None, dry_run: bool = False,
                include_stopped: bool = False) -> Dict[str, Any]:
        order = {role: i for i, role in enumerate(self.role_order)}
        records = sorted((r for r in self.registry.list() if ip is None or r.ip_id == ip),
                         key=lambda r: (r.ip_id, order.get(r.role, len(order)), r.role))
        notes: Dict[str, str] = {}
        if dry_run:
            plan: Dict[str, str] = {}
            for r in records:
                plan[r.agent_id], note = self._classify(r, include_stopped)
                if note:
                    notes[r.agent_id] = note
            return {"dry_run": True, "agents": plan, "notes": notes,
                    "queue_hints": self._queue_hints([a for a, c in plan.items() if c != "other_session"])}
        with broker_lock(self.state_dir):
            results: Dict[str, str] = {}
            for r in records:
                kind, note = self._classify(r, include_stopped)
                if note:
                    notes[r.agent_id] = note
                if kind != "restore":
                    results[r.agent_id] = kind
                    continue
                try:   # 单个失败不影响其他:恢复的目标是尽量多恢复
                    restored = self.life.restore(r.role, r.ip_id)
                    results[r.agent_id] = f"restored({restored.pane_id})"
                except Exception as exc:  # noqa: BLE001
                    results[r.agent_id] = f"restore_failed: {type(exc).__name__}: {exc}"
            hints = self._queue_hints([a for a, c in results.items() if c != "other_session"])
            self.audit.record({"state": "AGENTS_RECOVERED", "actor": _actor(), "session": self.session,
                               "ip": ip, "agents": results, "notes": notes, "queue_hints": hints})
        problems = {a: c for a, c in results.items()
                    if c.startswith("restore_failed") or c in PROBLEM_KINDS}
        return {"agents": results, "notes": notes, "queue_hints": hints, "problems": problems}


def _actor() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return "unknown"
