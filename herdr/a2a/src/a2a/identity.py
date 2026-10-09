"""稳定业务身份,以及"这条消息到底是谁发的"的判定。

业务身份与 herdr 的运行时位置分开:
  project_id  所属项目(拓扑里的 project_id)
  role        角色代号,如 dv、sw
  ip_id       固定负责的 IP,如 uart
  agent_id    `{role}_{ip_id}`,不带项目前缀(决策 D10),同时是 herdr agent 名字的候选

发送方身份的唯一入口是 resolve_sender():
  1. 环境变量(A2A_PROJECT_ID / A2A_ROLE / A2A_IP / HERDR_PANE_ID)必须齐全
  2. 用 HERDR_PANE_ID 在注册表里找到该 pane 登记的 agent
  3. 环境变量声明的身份必须与注册表记录完全一致(防冒充)
  4. 该 agent 必须处于 running,且在当前拓扑里仍然存在
任何一步失败都抛 IdentityError 的子类,Router 据此拒绝发送。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping

from .topology import Topology, TopologyError

if TYPE_CHECKING:  # 仅用于类型标注,避免与 registry 循环导入
    from .registry import AgentRecord, Registry

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

ENV_PROJECT = "A2A_PROJECT_ID"
ENV_ROLE = "A2A_ROLE"
ENV_IP = "A2A_IP"
ENV_PANE = "HERDR_PANE_ID"


class IdentityError(ValueError):
    pass


class IdentityMismatchError(IdentityError):
    """环境变量声明的身份与登记的身份对不上。"""


class SenderNotRegisteredError(IdentityError):
    """该 pane 没有登记过 agent(或已注销)。"""


class NodeNotInTopologyError(IdentityError):
    """登记的 agent 所对应的 (角色, IP) 已不在当前拓扑里。"""


class SenderNotRunningError(IdentityError):
    """登记的 agent 不是 running 状态。"""


def identity_env(project_id: str, role: str, ip_id: str) -> dict:
    """创建 tab / pane 时应注入的身份环境变量(传给 HerdrClient 的 env=)。"""
    return {ENV_PROJECT: project_id, ENV_ROLE: role, ENV_IP: ip_id}


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

    @property
    def env(self) -> dict:
        return identity_env(self.project_id, self.role, self.ip_id)

    @classmethod
    def create(cls, topology: Topology, role: str, ip_id: str) -> "AgentIdentity":
        try:
            topology.require_node(role, ip_id)
        except TopologyError as exc:
            raise IdentityError(str(exc)) from exc
        agent_id = f"{role}_{ip_id}"
        if len(agent_id) > 32 or not _IDENTIFIER.fullmatch(agent_id):
            raise IdentityError(f"业务 agent_id 不符合 herdr 名称约束: {agent_id}")
        return cls(topology.project_id, ip_id, role, agent_id)

    @classmethod
    def from_environment(cls, env: Mapping[str, str], topology: Topology) -> "AgentIdentity":
        """只凭环境变量和拓扑构造身份(不查注册表)。判定发送方请用 resolve_sender。"""
        missing = [key for key in (ENV_PROJECT, ENV_IP, ENV_ROLE) if not env.get(key)]
        if missing:
            raise IdentityError(f"缺少身份环境变量: {', '.join(missing)}")
        if env[ENV_PROJECT] != topology.project_id:
            raise IdentityMismatchError(
                f"{ENV_PROJECT}={env[ENV_PROJECT]} 与拓扑 project_id={topology.project_id} 不一致"
            )
        return cls.create(topology, env[ENV_ROLE], env[ENV_IP])


def resolve_sender(
    env: Mapping[str, str],
    registry: "Registry",
    topology: Topology,
    *,
    session: str,
) -> "AgentRecord":
    """判定发送方是谁,返回它在注册表里的记录。失败抛 IdentityError 的子类。

    session 必须显式传入(默认会话请传 "default"):pane 编号 w1:p2 在不同会话里会重复,
    不指定会话可能把别的会话里的 pane 误认成发送方。
    """
    from .registry import AgentNotRegisteredError, AmbiguousPaneError  # 延迟导入,避免循环

    if not isinstance(session, str) or not session:
        raise ValueError("session 必须是非空字符串(默认会话请传 'default')")

    missing = [key for key in (ENV_PANE, ENV_PROJECT, ENV_ROLE, ENV_IP) if not env.get(key)]
    if missing:
        raise IdentityError(f"缺少身份环境变量: {', '.join(missing)}")

    if env[ENV_PROJECT] != topology.project_id:
        raise IdentityMismatchError(
            f"{ENV_PROJECT}={env[ENV_PROJECT]} 与拓扑 project_id={topology.project_id} 不一致"
        )

    pane_id = env[ENV_PANE]
    try:
        record = registry.get_by_pane(pane_id, session=session)
    except (AgentNotRegisteredError, AmbiguousPaneError) as exc:
        raise SenderNotRegisteredError(f"pane {pane_id}(会话 {session})没有登记的 agent: {exc}") from exc

    supplied = (env[ENV_PROJECT], env[ENV_IP], env[ENV_ROLE])
    expected = (record.project_id, record.ip_id, record.role)
    if supplied != expected:
        raise IdentityMismatchError(f"环境身份 {supplied!r} 与登记身份 {expected!r} 不一致")

    if record.lifecycle != "running":
        raise SenderNotRunningError(f"{record.agent_id} 的状态是 {record.lifecycle},不是 running")

    if not topology.has_node(record.role, record.ip_id):
        raise NodeNotInTopologyError(f"({record.role}, {record.ip_id}) 已不在当前拓扑中")

    return record
