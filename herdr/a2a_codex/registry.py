"""进程安全、原子落盘的稳定业务身份 Registry。"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional

from .identity import AgentIdentity, IdentityError, IdentityMismatchError
from .paths import absolute_path, default_state_dir


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


class Registry:
    """JSON 文件 Registry。

    使用 OS 文件锁串行化多个 CLI 进程的读改写，使用同目录临时文件 +
    os.replace 避免进程中断时留下半截 JSON。适用于 macOS/Linux。
    """

    FORMAT_VERSION = 1
    STATUSES = {"idle", "working", "blocked", "done", "unknown"}
    LIFECYCLES = {"running", "stopped", "closed", "failed"}

    @staticmethod
    def default_state_dir(environ: Optional[Mapping[str, str]] = None) -> Path:
        return default_state_dir(environ)

    @classmethod
    def default_path(cls, environ: Optional[Mapping[str, str]] = None) -> Path:
        return cls.default_state_dir(environ) / "registry.json"

    def __init__(self, path: Optional[str | Path] = None, *,
                 environ: Optional[Mapping[str, str]] = None) -> None:
        self.path = absolute_path(path, "Registry 路径") if path is not None else self.default_path(environ)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._thread_lock = threading.RLock()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock, self.lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _load_unlocked(self) -> Dict[str, AgentRecord]:
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                payload = json.load(stream)
            if not isinstance(payload, dict):
                raise RegistryError("Registry 根节点必须是 JSON object")
            if payload.get("format_version") != self.FORMAT_VERSION:
                raise RegistryError("Registry format_version 不支持")
            records = payload.get("agents")
            if not isinstance(records, dict):
                raise RegistryError("Registry agents 必须是 mapping")
            result: Dict[str, AgentRecord] = {}
            for agent_id, raw in records.items():
                if not isinstance(raw, dict):
                    raise RegistryError(f"Registry 记录格式错误: {agent_id}")
                record = AgentRecord(**raw)
                if record.agent_id != agent_id:
                    raise RegistryError(f"Registry key 与记录 agent_id 不一致: {agent_id}")
                if not isinstance(record.session, str) or not record.session.strip():
                    raise RegistryError(f"Registry session 必须是非空字符串: {agent_id}")
                result[agent_id] = record
            return result
        except (OSError, ValueError, TypeError) as exc:
            if isinstance(exc, RegistryError):
                raise
            raise RegistryError(f"无法读取 Registry {self.path}: {exc}") from exc

    def _write_unlocked(self, records: Dict[str, AgentRecord]) -> None:
        payload = {
            "format_version": self.FORMAT_VERSION,
            "updated_at": _now(),
            "agents": {agent_id: asdict(record) for agent_id, record in sorted(records.items())},
        }
        fd, temp_name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.path)
            directory_fd = os.open(str(self.path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    @staticmethod
    def _validate_text(value: Optional[str], field: str, *, optional: bool = False) -> None:
        if optional and value is None:
            return
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} 必须是非空字符串")

    @staticmethod
    def _assert_unique_runtime(records: Dict[str, AgentRecord], candidate: AgentRecord,
                               *, except_agent_id: Optional[str] = None) -> None:
        for other_id, other in records.items():
            if other_id == except_agent_id:
                continue
            if other.session != candidate.session:
                continue
            if other.pane_id == candidate.pane_id:
                raise RuntimeAddressConflictError(
                    f"pane {candidate.pane_id} 在 session {candidate.session!r} 已注册给 {other_id}"
                )
            if other.agent_name == candidate.agent_name:
                raise RuntimeAddressConflictError(
                    f"agent name {candidate.agent_name} 在 session {candidate.session!r} 已注册给 {other_id}"
                )

    def register(self, identity: AgentIdentity, *, session: str, workspace_id: str,
                 tab_id: str, pane_id: str, agent_name: Optional[str] = None,
                 status: str = "unknown", lifecycle: str = "running") -> AgentRecord:
        for value, name in ((identity.project_id, "project_id"), (identity.ip_id, "ip_id"),
                            (identity.role, "role"), (identity.agent_id, "agent_id"),
                            (workspace_id, "workspace_id"), (tab_id, "tab_id"), (pane_id, "pane_id")):
            self._validate_text(value, name)
        if identity.agent_id != f"{identity.role}_{identity.ip_id}" or len(identity.agent_id) > 32:
            raise ValueError("agent_id 必须为 role_ip 格式且不超过 32 字符")
        self._validate_text(session, "session")
        name = agent_name or identity.agent_id
        self._validate_text(name, "agent_name")
        if status not in self.STATUSES:
            raise ValueError(f"非法 agent status: {status}")
        if lifecycle not in self.LIFECYCLES:
            raise ValueError(f"非法 lifecycle: {lifecycle}")
        if len(name) > 32:
            raise ValueError("agent_name 最长 32 字符")
        with self._locked():
            records = self._load_unlocked()
            if identity.agent_id in records:
                raise AgentAlreadyRegisteredError(f"业务身份已注册: {identity.agent_id}")
            now = _now()
            record = AgentRecord(identity.project_id, identity.ip_id, identity.role, identity.agent_id,
                                 session, workspace_id, tab_id, pane_id, name, status, lifecycle, now, now)
            self._assert_unique_runtime(records, record)
            records[identity.agent_id] = record
            self._write_unlocked(records)
            return record

    def get(self, agent_id: str) -> AgentRecord:
        with self._locked():
            try:
                return self._load_unlocked()[agent_id]
            except KeyError as exc:
                raise AgentNotRegisteredError(f"业务身份未注册: {agent_id}") from exc

    def list(self, *, project_id: Optional[str] = None) -> List[AgentRecord]:
        with self._locked():
            records = self._load_unlocked()
        return sorted((record for record in records.values()
                       if project_id is None or record.project_id == project_id),
                      key=lambda item: item.agent_id)

    def get_by_pane(self, pane_id: str, *, session: Optional[str] = None) -> AgentRecord:
        with self._locked():
            records = self._load_unlocked().values()
            matches = [record for record in records
                       if record.pane_id == pane_id and (session is None or record.session == session)]
        if not matches:
            raise AgentNotRegisteredError(f"pane 未注册: {pane_id}")
        if len(matches) > 1:
            raise AmbiguousPaneError(f"pane {pane_id} 在多个 session 中匹配，请指定 session")
        return matches[0]

    def resolve_sender(self, env: Mapping[str, str], *, session: str) -> AgentRecord:
        self._validate_text(session, "session")
        pane_id = env.get("HERDR_PANE_ID")
        if not pane_id:
            raise IdentityError("缺少 HERDR_PANE_ID")
        record = self.get_by_pane(pane_id, session=session)
        supplied = (env.get("A2A_PROJECT_ID"), env.get("A2A_IP"), env.get("A2A_ROLE"))
        expected = (record.project_id, record.ip_id, record.role)
        if supplied != expected:
            raise IdentityMismatchError(f"环境身份 {supplied!r} 与 Registry 身份 {expected!r} 不一致")
        return record

    def update_runtime(self, agent_id: str, *, session: Optional[str] = None,
                       workspace_id: Optional[str] = None, tab_id: Optional[str] = None,
                       pane_id: Optional[str] = None, agent_name: Optional[str] = None,
                       status: Optional[str] = None, lifecycle: Optional[str] = None) -> AgentRecord:
        with self._locked():
            records = self._load_unlocked()
            old = records.get(agent_id)
            if old is None:
                raise AgentNotRegisteredError(f"业务身份未注册: {agent_id}")
            changes: Dict[str, Any] = {"updated_at": _now()}
            for key, value in (("session", session), ("workspace_id", workspace_id), ("tab_id", tab_id),
                               ("pane_id", pane_id), ("agent_name", agent_name),
                               ("status", status), ("lifecycle", lifecycle)):
                if value is not None:
                    changes[key] = value
            for key in ("workspace_id", "tab_id", "pane_id", "agent_name"):
                if key in changes:
                    self._validate_text(changes[key], key)
            if "session" in changes:
                self._validate_text(changes["session"], "session", optional=True)
            if len(changes.get("agent_name", old.agent_name)) > 32:
                raise ValueError("agent_name 最长 32 字符")
            if changes.get("status", old.status) not in self.STATUSES:
                raise ValueError(f"非法 agent status: {changes['status']}")
            if changes.get("lifecycle", old.lifecycle) not in self.LIFECYCLES:
                raise ValueError(f"非法 lifecycle: {changes['lifecycle']}")
            updated = AgentRecord(**{**asdict(old), **changes})
            self._assert_unique_runtime(records, updated, except_agent_id=agent_id)
            records[agent_id] = updated
            self._write_unlocked(records)
            return updated

    def update_status(self, agent_id: str, status: str) -> AgentRecord:
        return self.update_runtime(agent_id, status=status)

    def unregister(self, agent_id: str) -> AgentRecord:
        with self._locked():
            records = self._load_unlocked()
            try:
                removed = records.pop(agent_id)
            except KeyError as exc:
                raise AgentNotRegisteredError(f"业务身份未注册: {agent_id}") from exc
            self._write_unlocked(records)
            return removed
