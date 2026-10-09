"""稳定业务身份以及从 pane 环境解析发送方身份。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Optional

from .topology import Topology, TopologyError

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class IdentityError(ValueError):
    pass


class IdentityMismatchError(IdentityError):
    pass


@dataclass(frozen=True)
class AgentIdentity:
    project_id: str
    ip_id: str
    role: str
    agent_id: str

    def __post_init__(self) -> None:
        for name, value in (("project_id", self.project_id), ("ip_id", self.ip_id), ("role", self.role)):
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise IdentityError(f"{name} 格式非法")
        if self.agent_id != f"{self.role}_{self.ip_id}" or len(self.agent_id) > 32:
            raise IdentityError("agent_id 必须是 role_ip 格式且不超过 32 字符")

    @classmethod
    def create(cls, topology: Topology, role: str, ip_id: str) -> "AgentIdentity":
        try:
            topology.require_node(role, ip_id)
        except TopologyError as exc:
            raise IdentityError(str(exc)) from exc
        agent_id = f"{role}_{ip_id}"
        if len(agent_id) > 32 or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", agent_id):
            raise IdentityError(f"业务 agent_id 不符合 Herdr 名称约束: {agent_id}")
        return cls(topology.project_id, ip_id, role, agent_id)

    @classmethod
    def from_environment(cls, env: Mapping[str, str], topology: Topology) -> "AgentIdentity":
        required = ("A2A_PROJECT_ID", "A2A_IP", "A2A_ROLE")
        missing = [key for key in required if not env.get(key)]
        if missing:
            raise IdentityError(f"缺少身份环境变量: {', '.join(missing)}")
        if env["A2A_PROJECT_ID"] != topology.project_id:
            raise IdentityMismatchError(
                f"A2A_PROJECT_ID={env['A2A_PROJECT_ID']} 与拓扑 project_id={topology.project_id} 不一致"
            )
        return cls.create(topology, env["A2A_ROLE"], env["A2A_IP"])
