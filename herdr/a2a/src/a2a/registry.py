"""注册表:稳定业务 ID <-> 当前 herdr 运行时位置。

移植自 herdr/a2a_codex 的注册表,并做了这些调整:
  * session 必须是非空字符串(默认会话写 "default"),不再允许 None
  * 路径可省略,默认 <状态目录>/registry.json(见 paths.py)
  * 文件锁与原子写入抽到 _fsutil,和拓扑文件共用同一套实现
  * 发送方判定移到 identity.resolve_sender(同时校验拓扑),这里只提供按 pane 查找
  * 增加 find(role, ip)、list 的 role / ip 过滤

业务身份(agent_id = role_ip)是稳定的;pane_id / tab_id / agent_name 是会变的运行时地址,
pane 被重建后只更新这些字段。status 只是上次观察到的快照,会过期:投递前必须向 herdr 查实时状态。
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from ._fsutil import atomic_write_text, exclusive_lock
from .identity import AgentIdentity
from .paths import default_registry_path


class RegistryError(RuntimeError):
    pass


class AgentAlreadyRegisteredError(RegistryError):
    pass


class AgentNotRegisteredError(RegistryError):
    pass


class RuntimeAddressConflictError(RegistryError):
    pass


class AmbiguousPaneError(RegistryError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class AgentRecord:
    project_id: str
    ip_id: str
    role: str
    agent_id: str
    session: str
    workspace_id: str
    tab_id: str
    pane_id: str
    agent_name: str
    status: str
    lifecycle: str
    registered_at: str
    updated_at: str

    @property
    def identity(self) -> AgentIdentity:
        return AgentIdentity(self.project_id, self.ip_id, self.role, self.agent_id)


def _text(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")


class Registry:
    """JSON 文件注册表。

    多个进程(agent 里的 `a2a send`、broker、人工命令)可能同时读写,
    所以每次读改写都在跨进程文件锁里完成,写入是原子替换。适用于 macOS / Linux。
    """

    FORMAT_VERSION = 1
    STATUSES = {"idle", "working", "blocked", "done", "unknown"}
    LIFECYCLES = {"running", "stopped", "closed", "failed"}

    def __init__(self, path: "Optional[str | Path]" = None) -> None:
        raw = Path(path) if path is not None else default_registry_path()
        raw = raw.expanduser()
        if not raw.is_absolute():
            raise RegistryError(f"注册表路径必须是绝对路径: {raw}")
        self.path = raw
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._thread_lock = threading.RLock()

    # ------------------------------------------------------------------
    # 读写底层
    # ------------------------------------------------------------------
    def _load_unlocked(self) -> Dict[str, AgentRecord]:
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                payload = json.load(stream)
            if not isinstance(payload, dict):
                raise RegistryError("注册表根节点必须是 JSON object")
            if payload.get("format_version") != self.FORMAT_VERSION:
                raise RegistryError("注册表 format_version 不支持")
            records = payload.get("agents")
            if not isinstance(records, dict):
                raise RegistryError("注册表 agents 必须是 mapping")
            result: Dict[str, AgentRecord] = {}
            for agent_id, raw in records.items():
                if not isinstance(raw, dict):
                    raise RegistryError(f"注册表记录格式错误: {agent_id}")
                record = AgentRecord(**raw)
                if record.agent_id != agent_id:
                    raise RegistryError(f"注册表 key 与记录 agent_id 不一致: {agent_id}")
                result[agent_id] = record
            return result
        except (OSError, ValueError, TypeError) as exc:
            if isinstance(exc, RegistryError):
                raise
            raise RegistryError(f"无法读取注册表 {self.path}: {exc}") from exc

    def _write_unlocked(self, records: Dict[str, AgentRecord]) -> None:
        payload = {
            "format_version": self.FORMAT_VERSION,
            "updated_at": _now(),
            "agents": {agent_id: asdict(record) for agent_id, record in sorted(records.items())},
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        atomic_write_text(self.path, text)

    @staticmethod
    def _assert_unique_runtime(
        records: Dict[str, AgentRecord], candidate: AgentRecord, *, except_agent_id: Optional[str] = None
    ) -> None:
        for other_id, other in records.items():
            if other_id == except_agent_id or other.session != candidate.session:
                continue
            if other.pane_id == candidate.pane_id:
                raise RuntimeAddressConflictError(
                    f"pane {candidate.pane_id} 在会话 {candidate.session!r} 已登记给 {other_id}"
                )
            if other.agent_name == candidate.agent_name:
                raise RuntimeAddressConflictError(
                    f"agent 名字 {candidate.agent_name} 在会话 {candidate.session!r} 已登记给 {other_id}"
                )

    def _validate_status(self, status: str) -> None:
        if status not in self.STATUSES:
            raise ValueError(f"非法 agent status: {status}")

    def _validate_lifecycle(self, lifecycle: str) -> None:
        if lifecycle not in self.LIFECYCLES:
            raise ValueError(f"非法 lifecycle: {lifecycle}")

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    def register(
        self,
        identity: AgentIdentity,
        *,
        session: str,
        workspace_id: str,
        tab_id: str,
        pane_id: str,
        agent_name: Optional[str] = None,
        status: str = "unknown",
        lifecycle: str = "running",
    ) -> AgentRecord:
        for value, name in (
            (identity.project_id, "project_id"), (identity.ip_id, "ip_id"), (identity.role, "role"),
            (identity.agent_id, "agent_id"), (session, "session"), (workspace_id, "workspace_id"),
            (tab_id, "tab_id"), (pane_id, "pane_id"),
        ):
            _text(value, name)
        if identity.agent_id != f"{identity.role}_{identity.ip_id}" or len(identity.agent_id) > 32:
            raise ValueError("agent_id 必须为 role_ip 格式且不超过 32 字符")
        name = agent_name or identity.agent_id
        _text(name, "agent_name")
        if len(name) > 32:
            raise ValueError("agent_name 最长 32 字符")
        self._validate_status(status)
        self._validate_lifecycle(lifecycle)
        with self._thread_lock, exclusive_lock(self.lock_path):
            records = self._load_unlocked()
            if identity.agent_id in records:
                raise AgentAlreadyRegisteredError(f"业务身份已登记: {identity.agent_id}")
            now = _now()
            record = AgentRecord(
                identity.project_id, identity.ip_id, identity.role, identity.agent_id, session,
                workspace_id, tab_id, pane_id, name, status, lifecycle, now, now,
            )
            self._assert_unique_runtime(records, record)
            records[identity.agent_id] = record
            self._write_unlocked(records)
            return record

    def update_runtime(
        self,
        agent_id: str,
        *,
        session: Optional[str] = None,
        workspace_id: Optional[str] = None,
        tab_id: Optional[str] = None,
        pane_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        status: Optional[str] = None,
        lifecycle: Optional[str] = None,
    ) -> AgentRecord:
        """更新运行时地址或状态。业务身份不可改。pane 重建后调用它。"""
        with self._thread_lock, exclusive_lock(self.lock_path):
            records = self._load_unlocked()
            old = records.get(agent_id)
            if old is None:
                raise AgentNotRegisteredError(f"业务身份未登记: {agent_id}")
            changes: Dict[str, Any] = {"updated_at": _now()}
            for key, value in (
                ("session", session), ("workspace_id", workspace_id), ("tab_id", tab_id),
                ("pane_id", pane_id), ("agent_name", agent_name), ("status", status),
                ("lifecycle", lifecycle),
            ):
                if value is not None:
                    changes[key] = value
            for key in ("session", "workspace_id", "tab_id", "pane_id", "agent_name"):
                if key in changes:
                    _text(changes[key], key)
            if len(changes.get("agent_name", old.agent_name)) > 32:
                raise ValueError("agent_name 最长 32 字符")
            self._validate_status(changes.get("status", old.status))
            self._validate_lifecycle(changes.get("lifecycle", old.lifecycle))
            updated = AgentRecord(**{**asdict(old), **changes})
            self._assert_unique_runtime(records, updated, except_agent_id=agent_id)
            records[agent_id] = updated
            self._write_unlocked(records)
            return updated

    def update_status(self, agent_id: str, status: str) -> AgentRecord:
        return self.update_runtime(agent_id, status=status)

    def unregister(self, agent_id: str) -> AgentRecord:
        with self._thread_lock, exclusive_lock(self.lock_path):
            records = self._load_unlocked()
            try:
                removed = records.pop(agent_id)
            except KeyError as exc:
                raise AgentNotRegisteredError(f"业务身份未登记: {agent_id}") from exc
            self._write_unlocked(records)
            return removed

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def _snapshot(self) -> Dict[str, AgentRecord]:
        with self._thread_lock, exclusive_lock(self.lock_path):
            return self._load_unlocked()

    def get(self, agent_id: str) -> AgentRecord:
        try:
            return self._snapshot()[agent_id]
        except KeyError as exc:
            raise AgentNotRegisteredError(f"业务身份未登记: {agent_id}") from exc

    def find(self, role: str, ip_id: str) -> Optional[AgentRecord]:
        """按 (角色, IP) 查找;没有登记时返回 None。"""
        return self._snapshot().get(f"{role}_{ip_id}")

    def list(
        self,
        *,
        project_id: Optional[str] = None,
        role: Optional[str] = None,
        ip_id: Optional[str] = None,
    ) -> List[AgentRecord]:
        records = self._snapshot().values()
        return sorted(
            (r for r in records
             if (project_id is None or r.project_id == project_id)
             and (role is None or r.role == role)
             and (ip_id is None or r.ip_id == ip_id)),
            key=lambda item: item.agent_id,
        )

    def get_by_pane(self, pane_id: str, *, session: Optional[str] = None) -> AgentRecord:
        """按 pane 查找。session 为 None 表示不限会话,此时若多个会话里有同号 pane 会抛 AmbiguousPaneError。

        判定发送方时必须指定 session,见 identity.resolve_sender。
        """
        matches = [r for r in self._snapshot().values()
                   if r.pane_id == pane_id and (session is None or r.session == session)]
        if not matches:
            raise AgentNotRegisteredError(f"pane 未登记: {pane_id}")
        if len(matches) > 1:
            raise AmbiguousPaneError(f"pane {pane_id} 在多个会话中匹配,请指定 session")
        return matches[0]
