"""a2a: 基于 herdr 的多智能体编排框架。

当前只有 herdr 调用层(HerdrClient)和启动命令构造(launcher);
拓扑、路由、broker 等后续按 herdr/claude/04-design-python-framework.md 逐层添加。
"""
from .errors import (
    HerdrAgentBlocked,
    HerdrAgentNameTaken,
    HerdrAgentNotReady,
    HerdrBinaryNotFound,
    HerdrError,
    HerdrInvalidAgentName,
    HerdrNotFound,
    HerdrPromptStalled,
    HerdrServerNotRunning,
    HerdrTimeout,
    HerdrUsageError,
)
from .herdr_client import (
    AGENT_STATUSES,
    READY_OR_BLOCKED,
    READY_STATUSES,
    Created,
    HerdrClient,
    session_delete,
    session_list,
    session_stop,
)
from .launcher import LauncherError, build_launch_command, preflight_launcher

__all__ = [
    "AGENT_STATUSES",
    "READY_OR_BLOCKED",
    "READY_STATUSES",
    "Created",
    "HerdrAgentBlocked",
    "HerdrAgentNameTaken",
    "HerdrAgentNotReady",
    "HerdrBinaryNotFound",
    "HerdrClient",
    "HerdrError",
    "HerdrInvalidAgentName",
    "HerdrNotFound",
    "HerdrPromptStalled",
    "HerdrServerNotRunning",
    "HerdrTimeout",
    "HerdrUsageError",
    "LauncherError",
    "build_launch_command",
    "preflight_launcher",
    "session_delete",
    "session_list",
    "session_stop",
]
