"""agent 生命周期(阶段 5f;设计见 herdr/claude/04-design-python-framework.md §2.1.1、§2.3、§5)。

  spawn(role, ip)    建 pane(注入身份环境变量)→ 预检启动脚本 → 覆盖 exec 启动 → 等 herdr 识别
                     → 等 READY → agent rename 为 agent_id → 登记注册表(lifecycle=running)
  stop(role, ip)     只停 agent 进程(SIGTERM 它的前台进程组),保留 pane 与注册信息 -> stopped
  close(role, ip)    关闭 pane,保留注册信息与消息历史 -> closed
  purge(role, ip)    关闭 pane(如还在)并注销;历史仍留在审计日志与 spool
  restore(role, ip)  stopped:在原 pane 里重新启动;closed / pane 已不在:新建 pane 启动;都会重新改名

布局:workspace = 拓扑的 workspace_label;每个角色一个 tab(label = 角色 label);每个 IP 一个 pane。
同一 agent 的生命周期操作在 <状态目录>/lifecycle/<agent_id>.lock 上串行。

关于消息(08 §7.2):stop / close / purge 都不改写已入队的消息。broker 投递前复核时发现目标不在,
会把队列头判为 TARGET_MISSING 并暂停该目标,等操作员处理;已经不确定的消息保持不确定。

限制:stop 通过 os.killpg 发信号,所以必须和 herdr 在同一台机器上运行(本项目是虚拟机)。
拓扑里的"节点"是 角色 × IP 的组合,purge 不修改拓扑;要删 IP 请用 `a2a topology remove-ip`。
"""
from __future__ import annotations

import getpass
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ._fsutil import exclusive_lock
from .audit import AuditLog
from .errors import HerdrError, HerdrNotFound, HerdrTimeout
from .herdr_client import READY_STATUSES, HerdrClient
from .identity import AgentIdentity, IdentityError, identity_env
from .launcher import build_launch_command, preflight_launcher
from .registry import AgentNotRegisteredError, AgentRecord, Registry
from .topology import TopologyStore


class LifecycleError(RuntimeError):
    pass


def _actor() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


class Lifecycle:
    def __init__(
        self,
        *,
        client: HerdrClient,
        topology: TopologyStore,
        registry: Registry,
        audit: AuditLog,
        state_dir: Path,
        detect_timeout_s: float = 30.0,
        ready_timeout_s: float = 60.0,
        stop_timeout_s: float = 15.0,
        kill: Callable[[int, int], None] = os.killpg,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not client.session:
            raise ValueError("生命周期操作必须指定 herdr 会话")
        self.client, self.topology, self.registry, self.audit = client, topology, registry, audit
        self.session = client.session
        self.state_dir = Path(state_dir)
        self.lock_dir = self.state_dir / "lifecycle"
        self.detect_timeout_s, self.ready_timeout_s, self.stop_timeout_s = detect_timeout_s, ready_timeout_s, stop_timeout_s
        self._kill, self._sleep = kill, sleep

    # ---- 公共操作 --------------------------------------------------------
    def spawn(self, role: str, ip: str, *, cwd: Optional[str] = None) -> AgentRecord:
        identity, launcher = self._identity_and_launcher(role, ip)
        with self._lock(identity.agent_id):
            try:
                existing = self.registry.get(identity.agent_id)
            except AgentNotRegisteredError:
                existing = None
            if existing is not None:
                raise LifecycleError(f"{identity.agent_id} 已登记(lifecycle={existing.lifecycle});"
                                     f"已停止或已关闭的请用 restore,要彻底删除请用 purge")
            created = self._new_pane(identity, cwd)
            try:
                agent_name = self._launch(created["pane_id"], identity, launcher)
            except Exception:
                self._close_quietly(created["pane_id"])
                raise
            record = self.registry.register(identity, session=self.session, workspace_id=created["workspace_id"],
                                            tab_id=created["tab_id"], pane_id=created["pane_id"],
                                            agent_name=agent_name, status="idle", lifecycle="running")
            self._audit("AGENT_SPAWNED", record, f"在 {record.pane_id} 启动")
            return record

    def stop(self, role: str, ip: str) -> AgentRecord:
        agent_id = f"{role}_{ip}"
        with self._lock(agent_id):
            record = self.registry.get(agent_id)
            if record.lifecycle != "running":
                raise LifecycleError(f"{agent_id} 的 lifecycle 是 {record.lifecycle},不是 running")
            if self._agent_on(record) is not None:
                self._terminate(record)
            updated = self.registry.update_runtime(agent_id, lifecycle="stopped", status="unknown")
            self._audit("AGENT_STOPPED", updated, "agent 进程已停止,pane 与注册信息保留")
            return updated

    def close(self, role: str, ip: str) -> AgentRecord:
        agent_id = f"{role}_{ip}"
        with self._lock(agent_id):
            record = self.registry.get(agent_id)
            if record.lifecycle == "closed":
                return record
            self._close_quietly(record.pane_id)
            updated = self.registry.update_runtime(agent_id, lifecycle="closed", status="unknown")
            self._audit("AGENT_CLOSED", updated, "pane 已关闭,注册信息保留")
            return updated

    def purge(self, role: str, ip: str) -> AgentRecord:
        agent_id = f"{role}_{ip}"
        with self._lock(agent_id):
            record = self.registry.get(agent_id)
            if record.lifecycle != "closed":
                self._close_quietly(record.pane_id)
            removed = self.registry.unregister(agent_id)
            self._audit("AGENT_PURGED", removed, "已注销;历史仍在审计日志与 spool 里")
            return removed

    def restore(self, role: str, ip: str, *, cwd: Optional[str] = None) -> AgentRecord:
        identity, launcher = self._identity_and_launcher(role, ip)
        with self._lock(identity.agent_id):
            record = self.registry.get(identity.agent_id)
            if record.lifecycle == "running" and self._agent_on(record) is not None:
                raise LifecycleError(f"{identity.agent_id} 正在运行,不需要恢复")
            pane_alive = record.lifecycle != "closed" and self._pane_exists(record.pane_id)
            if pane_alive:
                where = {"workspace_id": record.workspace_id, "tab_id": record.tab_id, "pane_id": record.pane_id}
            else:
                where = self._new_pane(identity, cwd)
            try:
                agent_name = self._launch(where["pane_id"], identity, launcher)
            except Exception:
                if not pane_alive:
                    self._close_quietly(where["pane_id"])
                self.registry.update_runtime(identity.agent_id, lifecycle="failed")
                raise
            updated = self.registry.update_runtime(identity.agent_id, workspace_id=where["workspace_id"],
                                                   tab_id=where["tab_id"], pane_id=where["pane_id"],
                                                   agent_name=agent_name, status="idle", lifecycle="running")
            self._audit("AGENT_RESTORED", updated, "原 pane 重启" if pane_alive else f"新建 pane {updated.pane_id}")
            return updated

    def list(self) -> List[Dict[str, Any]]:
        """注册表 + herdr 实时状态。"""
        rows = []
        for record in self.registry.list():
            agent = self._agent_on(record)
            rows.append({"agent_id": record.agent_id, "lifecycle": record.lifecycle, "pane_id": record.pane_id,
                         "agent_name": record.agent_name, "session": record.session,
                         "live_status": agent.get("agent_status") if agent else None})
        return rows

    # ---- 布局 ------------------------------------------------------------
    def _new_pane(self, identity: AgentIdentity, cwd: Optional[str]) -> Dict[str, str]:
        """按布局为 agent 建一个新 pane,注入身份环境变量。"""
        topology = self.topology.current()
        env = self._agent_env(identity)
        cwd = cwd or os.path.expanduser("~")
        role_label = topology.roles[identity.role].label or identity.role
        workspace = next((w for w in self.client.workspace_list() if w.get("label") == topology.workspace_label), None)
        if workspace is None:
            created = self.client.workspace_create(label=topology.workspace_label, cwd=cwd, env=env)
            self.client.tab_rename(created.tab_id, role_label)
            return self._where(created)
        workspace_id = workspace["workspace_id"]
        tab = next((t for t in self.client.tab_list(workspace_id) if t.get("label") == role_label), None)
        panes = [p for p in self.client.pane_list(workspace_id) if tab and p.get("tab_id") == tab["tab_id"]]
        if tab is None or not panes:
            return self._where(self.client.tab_create(workspace_id=workspace_id, label=role_label, cwd=cwd, env=env))
        return self._where(self.client.pane_split(panes[-1]["pane_id"], direction="down", cwd=cwd, env=env))

    def _agent_env(self, identity: AgentIdentity) -> Dict[str, str]:
        """注入新 pane 的环境变量:身份,加上 pi 插件调用 `a2a send` 所需的位置信息。

        插件在 agent 进程里用 A2A_PYTHON -m a2a.cli 调用 Router(PYTHONPATH=A2A_SRC),
        与 broker 共用同一个状态目录和拓扑文件;这样不需要把 a2a 安装进系统。
        """
        env = identity_env(identity.project_id, identity.role, identity.ip_id)
        env.update({"A2A_STATE_DIR": str(self.state_dir), "A2A_TOPOLOGY": str(self.topology.path),
                    "A2A_PYTHON": sys.executable, "A2A_SRC": str(Path(__file__).resolve().parents[1])})
        return env

    @staticmethod
    def _where(created) -> Dict[str, str]:
        if not (created.workspace_id and created.tab_id and created.pane_id):
            raise LifecycleError(f"herdr 没有返回完整的 ID: {created.raw}")
        return {"workspace_id": created.workspace_id, "tab_id": created.tab_id, "pane_id": created.pane_id}

    # ---- 启动与停止 ------------------------------------------------------
    def _identity_and_launcher(self, role: str, ip: str):
        topology = self.topology.current()
        try:
            identity = AgentIdentity.create(topology, role, ip)
        except IdentityError as exc:
            raise LifecycleError(f"({role}, {ip}) 不在拓扑里:{exc}") from exc
        launcher = topology.roles[role].launcher
        if not launcher:
            raise LifecycleError(f"角色 {role} 没有配置 launcher(topology.yaml 的 roles.{role}.launcher)")
        problems = preflight_launcher(launcher)
        if problems:
            raise LifecycleError(f"启动脚本 {launcher} 预检不通过:{'; '.join(problems)}")
        return identity, launcher

    def _launch(self, pane_id: str, identity: AgentIdentity, launcher: str) -> str:
        self.client.pane_rename(pane_id, identity.agent_id)  # 显示名,不影响寻址
        launch_args = self.topology.current().roles[identity.role].launch_args
        self.client.pane_run(pane_id, build_launch_command(launcher, launch_args))
        self.client.wait_for_agent_detected(pane_id, timeout_s=self.detect_timeout_s)
        try:
            self.client.agent_wait(pane_id, until=READY_STATUSES, timeout_ms=int(self.ready_timeout_s * 1000))
        except HerdrTimeout as exc:
            raise LifecycleError(f"{identity.agent_id} 启动后 {self.ready_timeout_s:g} 秒内没有进入可投递状态") from exc
        # 改名是启动流程的最后一步:识别之前不能改名;每次重启都要重新改名(04 §2.1.1)
        self.client.agent_rename(pane_id, identity.agent_id)
        return identity.agent_id

    def _terminate(self, record: AgentRecord) -> None:
        """SIGTERM agent 的前台进程组,等它从 herdr 里消失。不碰 pane 的 shell。"""
        # 先按登记的名字找 agent(与投递引擎一致),确认它就在登记的 pane 上;
        # 防止 pane 里换成了别的 pi 进程(例如有人手动启动)时误发信号
        try:
            named = self.client.agent_get(record.agent_name) if record.agent_name else None
        except HerdrNotFound:
            named = None
        if named is None or named.get("pane_id") != record.pane_id:
            raise LifecycleError(f"{record.pane_id} 上的 agent 不是登记的 {record.agent_name!r},拒绝发信号;"
                                 f"请人工确认后处理")
        info = self.client.pane_process_info(record.pane_id)
        pgid, shell_pid = info.get("foreground_process_group_id"), info.get("shell_pid")
        argv0 = [(p.get("argv") or [""])[0] for p in info.get("foreground_processes", [])]
        if not isinstance(pgid, int) or pgid <= 1 or pgid == shell_pid:
            raise LifecycleError(f"{record.agent_id} 的前台进程组不明确(pgid={pgid}, shell={shell_pid}),拒绝发信号")
        if "pi" not in argv0:
            raise LifecycleError(f"{record.pane_id} 的前台进程 {argv0} 不是 agent,拒绝发信号")
        self._kill(pgid, signal.SIGTERM)
        deadline = time.monotonic() + self.stop_timeout_s
        while time.monotonic() < deadline:
            if self._agent_on(record) is None:
                return
            self._sleep(0.3)
        raise LifecycleError(f"{record.agent_id} 在 {self.stop_timeout_s:g} 秒内没有退出(已发送 SIGTERM)")

    # ---- 查询与工具 ------------------------------------------------------
    def _agent_on(self, record: AgentRecord) -> Optional[Dict[str, Any]]:
        """该记录的 pane 上当前的 agent;没有时返回 None。"""
        try:
            agent = self.client.agent_get(record.pane_id)
        except HerdrNotFound:
            return None
        return agent if agent.get("pane_id") == record.pane_id else None

    def _pane_exists(self, pane_id: str) -> bool:
        try:
            self.client.pane_get(pane_id)
            return True
        except HerdrNotFound:
            return False

    def _close_quietly(self, pane_id: str) -> None:
        try:
            self.client.pane_close(pane_id)
        except HerdrNotFound:
            pass  # 已经不在了
        except HerdrError as exc:
            raise LifecycleError(f"关闭 pane {pane_id} 失败:{exc}") from exc

    def _lock(self, agent_id: str):
        return exclusive_lock(self.lock_dir / f"{agent_id}.lock")

    def _audit(self, event: str, record: AgentRecord, detail: str) -> None:
        self.audit.record({"state": event, "agent_id": record.agent_id, "pane_id": record.pane_id,
                           "tab_id": record.tab_id, "lifecycle": record.lifecycle, "session": record.session,
                           "actor": _actor(), "detail": detail})
