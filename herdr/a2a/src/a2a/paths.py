"""状态目录与默认文件路径。

优先级(高到低):
  状态目录:  A2A_STATE_DIR  >  $XDG_STATE_HOME/a2a  >  ~/.local/state/a2a
  拓扑文件:  A2A_TOPOLOGY   >  <状态目录>/topology.yaml
  注册表:    <状态目录>/registry.json
  消息队列:  <状态目录>/spool/
  审计日志:  <状态目录>/audit.jsonl

路径必须是绝对路径。agent 们的工作目录各不相同,相对路径在不同进程里会指向不同位置,
悄悄产生多份互不相通的注册表,所以一律拒绝。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional


class PathConfigError(ValueError):
    """环境变量给出的路径不合法。"""


def _absolute(value: str, source: str) -> Path:
    path = Path(os.path.expanduser(value))
    if not path.is_absolute():
        raise PathConfigError(f"{source} 必须是绝对路径,收到 {value!r}")
    return path


def state_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    explicit = env.get("A2A_STATE_DIR")
    if explicit:
        return _absolute(explicit, "A2A_STATE_DIR")
    xdg = env.get("XDG_STATE_HOME")
    # XDG 规范:相对路径无效,应忽略
    if xdg and os.path.isabs(xdg):
        return Path(xdg) / "a2a"
    return Path(os.path.expanduser("~")) / ".local" / "state" / "a2a"


def default_topology_path(env: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    explicit = env.get("A2A_TOPOLOGY")
    if explicit:
        return _absolute(explicit, "A2A_TOPOLOGY")
    return state_dir(env) / "topology.yaml"


def default_registry_path(env: Optional[Mapping[str, str]] = None) -> Path:
    return state_dir(env) / "registry.json"


def default_spool_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    return state_dir(env) / "spool"


def default_audit_path(env: Optional[Mapping[str, str]] = None) -> Path:
    return state_dir(env) / "audit.jsonl"
