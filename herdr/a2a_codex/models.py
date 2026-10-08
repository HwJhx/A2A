"""HerdrClient 使用的数据模型。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class ResourceRef:
    """创建 Herdr 资源后返回的运行时 ID。"""

    raw: Dict[str, Any]
    workspace_id: Optional[str] = None
    tab_id: Optional[str] = None
    pane_id: Optional[str] = None


@dataclass(frozen=True)
class Agent:
    """Herdr agent 的常用字段；raw 保留完整返回值。"""

    raw: Dict[str, Any]
    agent: Optional[str] = None
    pane_id: Optional[str] = None
    tab_id: Optional[str] = None
    workspace_id: Optional[str] = None
    status: Optional[str] = None


def resource_ref(result: Dict[str, Any]) -> ResourceRef:
    pane = result.get("pane") or result.get("root_pane") or {}
    tab = result.get("tab") or {}
    workspace = result.get("workspace") or {}
    return ResourceRef(
        raw=dict(result),
        workspace_id=workspace.get("workspace_id") or tab.get("workspace_id") or pane.get("workspace_id"),
        tab_id=tab.get("tab_id") or pane.get("tab_id"),
        pane_id=pane.get("pane_id"),
    )


def agent_model(result: Dict[str, Any]) -> Agent:
    value = result.get("agent") if isinstance(result.get("agent"), dict) else result
    return Agent(
        raw=dict(value),
        agent=value.get("agent"),
        pane_id=value.get("pane_id"),
        tab_id=value.get("tab_id"),
        workspace_id=value.get("workspace_id"),
        status=value.get("agent_status") or value.get("status"),
    )
