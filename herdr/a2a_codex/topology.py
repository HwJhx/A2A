"""拓扑配置及校验。

拓扑描述单个 project 中可用的角色、IP、启动器和有向通信边。这里仅
加载并校验配置，不负责授权或投递；Router 会在后续阶段消费 Edge。
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import re
import string
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Tuple

import yaml

from .paths import absolute_path


class TopologyError(ValueError):
    """拓扑文件不可读或内容不符合 schema。"""


_SLUG = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_EDGE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_ALLOWED_TEMPLATE_FIELDS = {"ip"}


@dataclass(frozen=True)
class RoleSpec:
    role_id: str
    label: str
    kind: str = "pi"
    launcher: Optional[str] = None


@dataclass(frozen=True)
class Edge:
    edge_id: str
    source_role: str
    target_role: str
    template: str


@dataclass(frozen=True)
class Topology:
    version: int
    project_id: str
    workspace_label: str
    roles: Mapping[str, RoleSpec]
    ips: Tuple[str, ...]
    edges: Mapping[str, Edge]

    @classmethod
    def load(cls, path: str | Path) -> "Topology":
        config_path = Path(path)
        try:
            with config_path.open("r", encoding="utf-8") as stream:
                data = yaml.safe_load(stream)
        except OSError as exc:
            raise TopologyError(f"无法读取拓扑文件 {config_path}: {exc}") from exc
        except yaml.YAMLError as exc:
            raise TopologyError(f"YAML 格式错误: {exc}") from exc
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: Any) -> "Topology":
        if not isinstance(data, dict):
            raise TopologyError("拓扑根节点必须是 mapping")
        version = data.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version != 1:
            raise TopologyError("version 必须为 1")
        project_id = _required_slug(data.get("project_id"), "project_id")
        workspace_label = data.get("workspace_label", project_id)
        if not isinstance(workspace_label, str) or not workspace_label.strip():
            raise TopologyError("workspace_label 必须是非空字符串")

        role_data = data.get("roles")
        if not isinstance(role_data, dict):
            raise TopologyError("roles 必须是 mapping")
        roles: Dict[str, RoleSpec] = {}
        for role_id, config in role_data.items():
            role_id = _required_slug(role_id, "role id")
            if not isinstance(config, dict):
                raise TopologyError(f"roles.{role_id} 必须是 mapping")
            label = config.get("label", role_id)
            kind = config.get("kind", "pi")
            launcher = config.get("launcher")
            if not isinstance(label, str) or not label.strip():
                raise TopologyError(f"roles.{role_id}.label 必须是非空字符串")
            if not isinstance(kind, str) or not kind.strip():
                raise TopologyError(f"roles.{role_id}.kind 必须是非空字符串")
            if launcher is not None and (not isinstance(launcher, str) or not launcher.startswith("/")):
                raise TopologyError(f"roles.{role_id}.launcher 必须为空或绝对路径")
            roles[role_id] = RoleSpec(role_id, label, kind, launcher)

        ip_data = data.get("ips")
        if not isinstance(ip_data, list):
            raise TopologyError("ips 必须是列表")
        ips = tuple(_required_slug(ip_id, "ip id") for ip_id in ip_data)
        if len(set(ips)) != len(ips):
            raise TopologyError("ips 中不能有重复项")

        edge_data = data.get("edges", [])
        if not isinstance(edge_data, list):
            raise TopologyError("edges 必须是列表")
        edges: Dict[str, Edge] = {}
        for index, item in enumerate(edge_data):
            where = f"edges[{index}]"
            if not isinstance(item, dict):
                raise TopologyError(f"{where} 必须是 mapping")
            edge_id = item.get("id")
            if not isinstance(edge_id, str) or not _EDGE_ID.fullmatch(edge_id):
                raise TopologyError(f"{where}.id 格式非法")
            if edge_id in edges:
                raise TopologyError(f"edge id 重复: {edge_id}")
            source = item.get("from")
            target = item.get("to")
            if not isinstance(source, str) or source not in roles:
                raise TopologyError(f"{where}.from 引用了未知角色: {source!r}")
            if not isinstance(target, str) or target not in roles:
                raise TopologyError(f"{where}.to 引用了未知角色: {target!r}")
            if source == target:
                raise TopologyError(f"{where} 不允许角色向自身发送")
            template = item.get("template")
            if not isinstance(template, str) or not template.strip():
                raise TopologyError(f"{where}.template 必须是非空字符串")
            _validate_template(template, where)
            edges[edge_id] = Edge(edge_id, source, target, template)

        return cls(version, project_id, workspace_label, roles, ips, edges)

    def has_node(self, role: str, ip_id: str) -> bool:
        return role in self.roles and ip_id in self.ips

    def require_node(self, role: str, ip_id: str) -> None:
        if role not in self.roles:
            raise TopologyError(f"未知角色: {role}")
        if ip_id not in self.ips:
            raise TopologyError(f"拓扑未配置 IP: {ip_id}")

    def get_edge(self, edge_id: str) -> Edge:
        try:
            return self.edges[edge_id]
        except KeyError as exc:
            raise TopologyError(f"拓扑未配置通信边: {edge_id}") from exc


class TopologyStore:
    """动态拓扑存储：变更经校验、备份和原子写入；读取时按需热加载。"""

    def __init__(self, path: str | Path) -> None:
        self.path = absolute_path(path, "TopologyStore 路径")
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.backup_path = self.path.with_name(self.path.name + ".bak")
        self._lock = threading.RLock()
        self._topology, self._signature, self._revision = self._read_snapshot()
        self._checked_signature = self._signature
        self.last_error: Optional[str] = None

    def _read_snapshot(self) -> Tuple[Topology, Tuple[int, int, int], str]:
        raw = self.path.read_bytes()
        data = yaml.safe_load(raw.decode("utf-8"))
        topology = Topology.from_dict(data)
        signature = self._stat_signature()
        revision = hashlib.sha256(raw).hexdigest()[:12]
        return topology, signature, revision

    def _stat_signature(self) -> Tuple[int, int, int]:
        stat = self.path.stat()
        return stat.st_ino, stat.st_mtime_ns, stat.st_size

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.lock_path.open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _to_dict(topology: Topology) -> Dict[str, Any]:
        return {
            "version": topology.version,
            "project_id": topology.project_id,
            "workspace_label": topology.workspace_label,
            "roles": {
                role_id: {"label": role.label, "kind": role.kind, "launcher": role.launcher}
                for role_id, role in topology.roles.items()
            },
            "ips": list(topology.ips),
            "edges": [
                {"id": edge.edge_id, "from": edge.source_role, "to": edge.target_role,
                 "template": edge.template}
                for edge in topology.edges.values()
            ],
        }

    @classmethod
    def _dump(cls, topology: Topology) -> str:
        return yaml.safe_dump(cls._to_dict(topology), allow_unicode=True, sort_keys=False)

    @property
    def current(self) -> Topology:
        with self._lock:
            try:
                signature = self._stat_signature()
                if signature != self._checked_signature:
                    candidate, loaded_signature, revision = self._read_snapshot()
                    self._topology = candidate
                    self._signature = loaded_signature
                    self._checked_signature = loaded_signature
                    self._revision = revision
                    self.last_error = None
            except (OSError, UnicodeError, yaml.YAMLError, TopologyError) as exc:
                self._checked_signature = signature if "signature" in locals() else None
                self.last_error = str(exc)
            return self._topology

    def reload(self) -> Topology:
        with self._file_lock():
            candidate, signature, revision = self._read_snapshot()
            self._topology = candidate
            self._signature = signature
            self._checked_signature = signature
            self._revision = revision
            self.last_error = None
            return candidate

    @property
    def revision(self) -> str:
        self.current
        return self._revision

    def update(self, mutate: Callable[[Dict[str, Any]], None]) -> Topology:
        """在最新磁盘配置上执行一次校验后的原子修改。"""
        with self._file_lock():
            try:
                raw = self.path.read_text(encoding="utf-8")
                data = yaml.safe_load(raw)
            except (OSError, yaml.YAMLError) as exc:
                raise TopologyError(f"无法读取或解析拓扑文件 {self.path}: {exc}") from exc
            baseline = Topology.from_dict(data)
            working = self._to_dict(baseline)
            mutate(working)
            candidate = Topology.from_dict(working)
            self._atomic_write(self.backup_path, raw)
            self._atomic_write(self.path, self._dump(candidate))
            self._topology = candidate
            self._topology, self._signature, self._revision = self._read_snapshot()
            self._checked_signature = self._signature
            self.last_error = None
            return candidate

    def add_ip(self, ip_id: str) -> Topology:
        def mutate(data: Dict[str, Any]) -> None:
            if ip_id in data["ips"]:
                raise TopologyError(f"IP 已存在: {ip_id}")
            data["ips"].append(ip_id)
        return self.update(mutate)

    def remove_ip(self, ip_id: str) -> Topology:
        """移除拓扑节点；不检查或关闭 Registry/Herdr 中的运行中 agent。"""
        def mutate(data: Dict[str, Any]) -> None:
            if ip_id not in data["ips"]:
                raise TopologyError(f"IP 不存在: {ip_id}")
            data["ips"].remove(ip_id)
        return self.update(mutate)

    def add_role(self, role_id: str, *, label: Optional[str] = None, kind: str = "pi",
                 launcher: Optional[str] = None) -> Topology:
        def mutate(data: Dict[str, Any]) -> None:
            if role_id in data["roles"]:
                raise TopologyError(f"角色已存在: {role_id}")
            data["roles"][role_id] = {
                "label": role_id if label is None else label,
                "kind": kind,
                "launcher": launcher,
            }
        return self.update(mutate)

    def remove_role(self, role_id: str, *, cascade_edges: bool = False) -> Topology:
        def mutate(data: Dict[str, Any]) -> None:
            if role_id not in data["roles"]:
                raise TopologyError(f"角色不存在: {role_id}")
            linked = [edge for edge in data["edges"]
                      if edge["from"] == role_id or edge["to"] == role_id]
            if linked and not cascade_edges:
                raise TopologyError(f"角色 {role_id} 仍被 edge 引用；使用 cascade_edges=True 才可级联删除")
            if cascade_edges:
                data["edges"] = [edge for edge in data["edges"]
                                 if edge["from"] != role_id and edge["to"] != role_id]
            del data["roles"][role_id]
        return self.update(mutate)

    def set_role_launcher(self, role_id: str, launcher: Optional[str]) -> Topology:
        def mutate(data: Dict[str, Any]) -> None:
            try:
                data["roles"][role_id]["launcher"] = launcher
            except KeyError as exc:
                raise TopologyError(f"角色不存在: {role_id}") from exc
        return self.update(mutate)

    def add_edge(self, edge_id: str, source_role: str, target_role: str, template: str) -> Topology:
        def mutate(data: Dict[str, Any]) -> None:
            if any(edge["id"] == edge_id for edge in data["edges"]):
                raise TopologyError(f"edge 已存在: {edge_id}")
            data["edges"].append({"id": edge_id, "from": source_role,
                                  "to": target_role, "template": template})
        return self.update(mutate)

    def remove_edge(self, edge_id: str) -> Topology:
        def mutate(data: Dict[str, Any]) -> None:
            edges = [edge for edge in data["edges"] if edge["id"] != edge_id]
            if len(edges) == len(data["edges"]):
                raise TopologyError(f"edge 不存在: {edge_id}")
            data["edges"] = edges
        return self.update(mutate)

    def set_template(self, edge_id: str, template: str) -> Topology:
        def mutate(data: Dict[str, Any]) -> None:
            for edge in data["edges"]:
                if edge["id"] == edge_id:
                    edge["template"] = template
                    return
            raise TopologyError(f"edge 不存在: {edge_id}")
        return self.update(mutate)


def _required_slug(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SLUG.fullmatch(value):
        raise TopologyError(f"{field} 必须匹配 {_SLUG.pattern}")
    return value


def _validate_template(template: str, where: str) -> None:
    try:
        parts = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise TopologyError(f"{where}.template 格式错误: {exc}") from exc
    fields = [field for _, field, format_spec, conversion in parts if field is not None]
    unknown = sorted(set(fields) - _ALLOWED_TEMPLATE_FIELDS)
    if unknown:
        raise TopologyError(f"{where}.template 使用未允许的字段: {', '.join(unknown)}")
    if any(field is not None and (format_spec or conversion) for _, field, format_spec, conversion in parts):
        raise TopologyError(f"{where}.template 目前只允许简单占位符 {{ip}}")
