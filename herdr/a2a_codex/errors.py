"""Herdr CLI 错误映射。"""
from __future__ import annotations

from typing import Optional, Sequence


class HerdrError(Exception):
    def __init__(self, message: str, *, code: Optional[str] = None,
                 argv: Optional[Sequence[str]] = None, returncode: Optional[int] = None,
                 stdout: str = "", stderr: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.argv = list(argv or [])
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}" if self.code else self.message


class HerdrBinaryNotFound(HerdrError):
    pass


class HerdrTimeout(HerdrError):
    pass


class HerdrUsageError(HerdrError):
    pass


class HerdrServerNotRunning(HerdrError):
    pass


class HerdrNotFound(HerdrError):
    pass


class HerdrAgentNotReady(HerdrError):
    """herdr 0.9.3 实测为 prompt 写入前拒绝；Broker 退避后复核再试。"""
    pass


class HerdrAgentBlocked(HerdrError):
    """herdr 0.9.3 实测为 prompt 写入前拒绝；Broker 应转 TARGET_BLOCKED。"""
    pass


class HerdrAgentNameTaken(HerdrError):
    pass


class HerdrInvalidAgentName(HerdrError):
    pass


class HerdrPromptOutcomeUnknown(HerdrError):
    """Prompt 可能已提交；调用方必须核查后再考虑重试。"""


class HerdrAgentPromptFailed(HerdrPromptOutcomeUnknown):
    """herdr 0.9.3 的 PTY 写入中断错误；实测 prompt 可能已部分/全部写入。"""


_ERRORS = {
    "server_not_running": HerdrServerNotRunning,
    "agent_not_ready": HerdrAgentNotReady,
    "agent_blocked": HerdrAgentBlocked,
    "agent_not_found": HerdrNotFound,
    "agent_name_taken": HerdrAgentNameTaken,
    "invalid_agent_name": HerdrInvalidAgentName,
    "agent_prompt_failed": HerdrAgentPromptFailed,
    "agent_prompt_stalled": HerdrPromptOutcomeUnknown,
    "timeout": HerdrTimeout,
}


def from_code(code: Optional[str], message: str, **kwargs: object) -> HerdrError:
    cls = _ERRORS.get(code or "")
    if cls is None and code and code.endswith("_not_found"):
        cls = HerdrNotFound
    if cls is None:
        cls = HerdrError
    return cls(message, code=code, **kwargs)  # type: ignore[arg-type]
