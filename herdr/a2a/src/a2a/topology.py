"""拓扑:角色、IP、有向通信边、固定消息模板。

拓扑是"谁能给谁发什么"的唯一事实源(04 号文档 §3)。这里分两层:

  Topology       不可变快照。负责解析和校验,不做任何 IO。
  TopologyStore  文件托管的拓扑。负责热加载(文件被改动后自动重新读取)和
                 动态修改(增删 IP / 角色 / 边,修改模板),修改会校验后原子写回文件。

移植自 herdr/a2a_codex 的拓扑模块,并做了这些调整:
  * ips 与 edges 允许为空(动态拓扑需要从空开始逐步添加)
  * 增加 to_dict / edges_from / nodes
  * 增加 TopologyStore:热加载、动态修改、跨进程文件锁、写前备份、内容修订号

边只描述角色方向,不携带 IP。"目标 IP 必须等于发送方 IP"由 Router 在发送时强制校验,配置无法放开。
"""
from __future__ import annotations

import copy
import hashlib
import os
import re
import string
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple

import yaml

from ._fsutil import atomic_write_text, exclusive_lock
from .paths import default_topology_path


class TopologyError(ValueError):
    """拓扑文件不可读,或内容不符合 schema,或修改会让拓扑非法。"""


_SLUG = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_EDGE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_ALLOWED_TEMPLATE_FIELDS = {"ip"}
SCHEMA_VERSION = 1

_FILE_HEADER = (
    "# 此文件由 a2a 管理:人工编辑后会被自动重新加载;程序修改时会整体重写,\n"
    "# 重写不会保留注释(上一版本保存在同目录的 .bak 文件里)。\n"
)


@dataclass(frozen=True)
class RoleSpec:
    role_id: str
    label: str
    kind: str = "pi"
    launcher: Optional[str] = None
    # 追加给启动脚本的参数,例如测试时用 ["--no-builtin-tools"] 让模型只剩 a2a_send
    launch_args: Tuple[str, ...] = ()


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

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: "str | Path") -> "Topology":
        config_path = Path(path)
        try:
            text = config_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise TopologyError(f"无法读取拓扑文件 {config_path}: {exc}") from exc
        return cls.from_text(text)

    @classmethod
    def from_text(cls, text: str) -> "Topology":
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise TopologyError(f"YAML 格式错误: {exc}") from exc
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: Any) -> "Topology":
        if not isinstance(data, dict):
            raise TopologyError("拓扑根节点必须是 mapping")
        version = data.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version != SCHEMA_VERSION:
            raise TopologyError(f"version 必须为 {SCHEMA_VERSION}")
        project_id = _required_slug(data.get("project_id"), "project_id")
        workspace_label = data.get("workspace_label", project_id)
        if not isinstance(workspace_label, str) or not workspace_label.strip():
            raise TopologyError("workspace_label 必须是非空字符串")

        role_data = data.get("roles")
        if not isinstance(role_data, dict) or not role_data:
            raise TopologyError("roles 必须是非空 mapping")
        roles: Dict[str, RoleSpec] = {}
        for raw_id, config in role_data.items():
            role_id = _required_slug(raw_id, "role id")
            if config is None:
                config = {}
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
            launch_args = config.get("launch_args") or []
            if not isinstance(launch_args, list) or not all(isinstance(a, str) and a for a in launch_args):
                raise TopologyError(f"roles.{role_id}.launch_args 必须是非空字符串的列表")
            roles[role_id] = RoleSpec(role_id, label, kind, launcher, tuple(launch_args))

        ip_data = data.get("ips", [])
        if ip_data is None:
            ip_data = []
        if not isinstance(ip_data, list):
            raise TopologyError("ips 必须是列表(可以为空)")
        ips = tuple(_required_slug(ip_id, "ip id") for ip_id in ip_data)
        if len(set(ips)) != len(ips):
            raise TopologyError("ips 中不能有重复项")

        edge_data = data.get("edges", [])
        if edge_data is None:
            edge_data = []
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

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
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

    def edges_from(self, role: str) -> List[Edge]:
        return [edge for edge in self.edges.values() if edge.source_role == role]

    def nodes(self) -> Iterator[Tuple[str, str]]:
        """所有 (角色, IP) 组合,即理论上应有的 agent 节点。"""
        for role in self.roles:
            for ip_id in self.ips:
                yield role, ip_id

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "project_id": self.project_id,
            "workspace_label": self.workspace_label,
            "roles": {
                role.role_id: dict({"label": role.label, "kind": role.kind, "launcher": role.launcher},
                                   **({"launch_args": list(role.launch_args)} if role.launch_args else {}))
                for role in self.roles.values()
            },
            "ips": list(self.ips),
            "edges": [
                {"id": e.edge_id, "from": e.source_role, "to": e.target_role, "template": e.template}
                for e in self.edges.values()
            ],
        }


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
        raise TopologyError(f"{where}.template 使用未允许的字段: {', '.join(repr(u) for u in unknown)}")
    if any(field is not None and (format_spec or conversion) for _, field, format_spec, conversion in parts):
        raise TopologyError(f"{where}.template 目前只允许简单占位符 {{ip}}")


# ----------------------------------------------------------------------
# 文件托管的拓扑:热加载 + 动态修改
# ----------------------------------------------------------------------
Signature = Tuple[int, int, int]


class TopologyStore:
    """文件托管的拓扑。

    读:  current() 每次调用都比较文件签名,文件变了就重新加载。
         新内容不合法时保留上一份合法拓扑继续服务,错误记在 last_error,不会让 broker 崩溃。
    写:  add_ip / remove_ip / add_role / remove_role / set_role_launcher /
         add_edge / remove_edge / set_template。
         每次修改都在跨进程文件锁里:重新读磁盘 → 修改 → 校验 → 备份 → 原子写回。
         因此多个进程同时修改不同条目时不会互相覆盖;修改会让拓扑非法时整体拒绝,文件不变。
    注意: 程序写回会整体重写 YAML,不保留注释;上一版保存在同目录的 .bak。
    """

    def __init__(self, path: "Optional[str | Path]" = None, *, env: Optional[Mapping[str, str]] = None) -> None:
        target = Path(path) if path is not None else default_topology_path(env)
        target = Path(os.path.expanduser(str(target)))
        if not target.is_absolute():
            raise TopologyError(f"拓扑文件必须是绝对路径: {target}")
        self.path = target
        self.lock_path = target.with_name(target.name + ".lock")
        self.backup_path = target.with_name(target.name + ".bak")
        self._thread_lock = threading.RLock()
        self._topology: Optional[Topology] = None
        self._signature: Optional[Signature] = None
        self._failed_signature: Optional[Signature] = None
        self._revision: str = ""
        self.last_error: Optional[str] = None
        self.load()

    # ---- 创建 --------------------------------------------------------
    @classmethod
    def create(cls, path: "str | Path", data: Any) -> "TopologyStore":
        """用一份初始拓扑创建文件。文件已存在时拒绝覆盖。"""
        target = Path(os.path.expanduser(str(path)))
        if not target.is_absolute():
            raise TopologyError(f"拓扑文件必须是绝对路径: {target}")
        topology = data if isinstance(data, Topology) else Topology.from_dict(data)
        lock = target.with_name(target.name + ".lock")
        with exclusive_lock(lock):
            if target.exists():
                raise TopologyError(f"拓扑文件已存在,拒绝覆盖: {target}")
            atomic_write_text(target, _dump(topology))
        return cls(target)

    # ---- 读 ----------------------------------------------------------
    @property
    def revision(self) -> str:
        """当前生效拓扑的内容修订号(文件内容的 SHA-256 前 12 位)。审计日志里应记录它。"""
        return self._revision

    def _stat_signature(self) -> Signature:
        st = os.stat(self.path)
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    def load(self) -> Topology:
        """严格加载:读不了或不合法时抛 TopologyError。"""
        with self._thread_lock:
            raw, signature = self._read_file()
            topology = Topology.from_text(raw)
            self._adopt(topology, raw, signature)
            return topology

    def current(self) -> Topology:
        """返回当前生效的拓扑;文件被改动过则先重新加载。"""
        self.reload_if_changed()
        assert self._topology is not None
        return self._topology

    def reload_if_changed(self) -> bool:
        """文件被改动时重新加载,返回是否换成了新拓扑。新内容不合法时保留旧拓扑并记录 last_error。"""
        with self._thread_lock:
            try:
                signature = self._stat_signature()
            except OSError as exc:
                self.last_error = f"无法访问拓扑文件 {self.path}: {exc}"
                return False
            if signature == self._signature or signature == self._failed_signature:
                return False
            try:
                raw, signature = self._read_file()
                topology = Topology.from_text(raw)
            except TopologyError as exc:
                self._failed_signature = signature
                self.last_error = str(exc)
                return False
            self._adopt(topology, raw, signature)
            return True

    def _read_file(self) -> Tuple[str, Signature]:
        try:
            signature = self._stat_signature()
            return self.path.read_text(encoding="utf-8"), signature
        except OSError as exc:
            raise TopologyError(f"无法读取拓扑文件 {self.path}: {exc}") from exc

    def _adopt(self, topology: Topology, raw: str, signature: Signature) -> None:
        self._topology = topology
        self._signature = signature
        self._failed_signature = None
        self._revision = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        self.last_error = None

    # ---- 写 ----------------------------------------------------------
    def update(self, mutate: Callable[[Dict[str, Any]], None]) -> Topology:
        """通用修改入口:mutate 直接修改传入的 dict(磁盘上的最新内容)。

        校验失败(TopologyError)时文件保持不变。
        """
        with self._thread_lock, exclusive_lock(self.lock_path):
            raw, _ = self._read_file()
            try:
                data = yaml.safe_load(raw)
            except yaml.YAMLError as exc:
                raise TopologyError(f"磁盘上的拓扑文件已损坏,拒绝修改: {exc}") from exc
            Topology.from_dict(data)  # 基线必须合法,否则先让人修复
            working = copy.deepcopy(data)
            mutate(working)
            topology = Topology.from_dict(working)  # 修改后必须仍然合法
            atomic_write_text(self.backup_path, raw)
            text = _dump(topology)
            atomic_write_text(self.path, text)
            self._adopt(topology, text, self._stat_signature())
            return topology

    def add_ip(self, ip_id: str) -> Topology:
        def mutate(data: Dict[str, Any]) -> None:
            ips = _list_field(data, "ips")
            if ip_id in ips:
                raise TopologyError(f"IP 已存在: {ip_id}")
            ips.append(ip_id)

        return self.update(mutate)

    def remove_ip(self, ip_id: str) -> Topology:
        """从拓扑里删除 IP。注意:不检查是否还有该 IP 的 agent 在运行,调用方应先 purge。"""
        def mutate(data: Dict[str, Any]) -> None:
            ips = _list_field(data, "ips")
            if ip_id not in ips:
                raise TopologyError(f"IP 不存在: {ip_id}")
            ips.remove(ip_id)

        return self.update(mutate)

    def add_role(self, role_id: str, *, label: Optional[str] = None, kind: str = "pi",
                 launcher: Optional[str] = None) -> Topology:
        def mutate(data: Dict[str, Any]) -> None:
            roles = data.setdefault("roles", {})
            if role_id in roles:
                raise TopologyError(f"角色已存在: {role_id}")
            roles[role_id] = {"label": label or role_id, "kind": kind, "launcher": launcher}

        return self.update(mutate)

    def remove_role(self, role_id: str, *, cascade_edges: bool = False) -> Topology:
        def mutate(data: Dict[str, Any]) -> None:
            roles = data.get("roles", {})
            if role_id not in roles:
                raise TopologyError(f"角色不存在: {role_id}")
            edges = _list_field(data, "edges")
            touching = [e for e in edges if isinstance(e, dict) and role_id in (e.get("from"), e.get("to"))]
            if touching and not cascade_edges:
                ids = ", ".join(str(e.get("id")) for e in touching)
                raise TopologyError(f"角色 {role_id} 仍被通信边引用: {ids};如需一并删除请指定 cascade_edges=True")
            edges[:] = [e for e in edges if e not in touching]
            del roles[role_id]

        return self.update(mutate)

    def set_role_launcher(self, role_id: str, launcher: Optional[str]) -> Topology:
        def mutate(data: Dict[str, Any]) -> None:
            roles = data.get("roles", {})
            if role_id not in roles:
                raise TopologyError(f"角色不存在: {role_id}")
            if roles[role_id] is None:
                roles[role_id] = {}
            roles[role_id]["launcher"] = launcher

        return self.update(mutate)

    def add_edge(self, edge_id: str, source_role: str, target_role: str, template: str) -> Topology:
        def mutate(data: Dict[str, Any]) -> None:
            edges = _list_field(data, "edges")
            if any(isinstance(e, dict) and e.get("id") == edge_id for e in edges):
                raise TopologyError(f"edge id 已存在: {edge_id}")
            edges.append({"id": edge_id, "from": source_role, "to": target_role, "template": template})

        return self.update(mutate)

    def remove_edge(self, edge_id: str) -> Topology:
        def mutate(data: Dict[str, Any]) -> None:
            edges = _list_field(data, "edges")
            kept = [e for e in edges if not (isinstance(e, dict) and e.get("id") == edge_id)]
            if len(kept) == len(edges):
                raise TopologyError(f"通信边不存在: {edge_id}")
            edges[:] = kept

        return self.update(mutate)

    def set_template(self, edge_id: str, template: str) -> Topology:
        def mutate(data: Dict[str, Any]) -> None:
            for edge in _list_field(data, "edges"):
                if isinstance(edge, dict) and edge.get("id") == edge_id:
                    edge["template"] = template
                    return
            raise TopologyError(f"通信边不存在: {edge_id}")

        return self.update(mutate)


def _list_field(data: Dict[str, Any], key: str) -> List[Any]:
    value = data.get(key)
    if value is None:
        value = []
        data[key] = value
    if not isinstance(value, list):
        raise TopologyError(f"{key} 必须是列表")
    return value


def _dump(topology: Topology) -> str:
    return _FILE_HEADER + yaml.safe_dump(
        topology.to_dict(), allow_unicode=True, sort_keys=False, default_flow_style=False
    )
