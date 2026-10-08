"""herdr 调用相关的异常。

herdr CLI 的失败有三种形态:
  * 退出码 1,stdout 或 stderr 里是 {"error": {"code": ..., "message": ...}} 的 JSON
  * 退出码 2,stderr 里是语法错误(用法不对)
  * 子进程根本没起来(找不到 herdr、执行超时)

这里把它们映射成不同的异常类,调用方可以只捕获自己关心的那几种。
"""
from __future__ import annotations

from typing import Optional, Sequence


class HerdrError(Exception):
    """所有 herdr 调用错误的基类。"""

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        argv: Optional[Sequence[str]] = None,
        returncode: Optional[int] = None,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.argv = list(argv) if argv is not None else []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __str__(self) -> str:
        prefix = f"[{self.code}] " if self.code else ""
        return f"{prefix}{self.message}"


class HerdrBinaryNotFound(HerdrError):
    """找不到 herdr 可执行文件。"""


class HerdrUsageError(HerdrError):
    """命令行语法错误(herdr 退出码 2)。"""


class HerdrServerNotRunning(HerdrError):
    """目标 session 的 server 没有运行。"""


class HerdrNotFound(HerdrError):
    """pane / tab / workspace / agent 不存在。"""


class HerdrAgentNotReady(HerdrError):
    """agent 当前不能接收输入(例如前台进程已不是该 agent)。"""


class HerdrAgentBlocked(HerdrError):
    """agent 停在审批或提问界面,herdr 拒绝向它写入。"""


class HerdrAgentNameTaken(HerdrError):
    """agent 名字已被其他存活的 agent 占用。"""


class HerdrInvalidAgentName(HerdrError):
    """agent 名字格式不合法。"""


class HerdrPromptStalled(HerdrError):
    """prompt 可能已经送达,但没有观察到对方状态变化。

    重要:这种情况下输入不一定没发出去。重试之前必须先读状态和最近输出,
    否则可能重复提交。
    """


class HerdrTimeout(HerdrError):
    """等待超时(herdr 侧的 timeout,或本进程等待子进程超时)。"""


# herdr 返回的 error.code -> 异常类
CODE_TO_EXCEPTION = {
    "server_not_running": HerdrServerNotRunning,
    "agent_not_ready": HerdrAgentNotReady,
    "agent_blocked": HerdrAgentBlocked,
    "agent_name_taken": HerdrAgentNameTaken,
    "invalid_agent_name": HerdrInvalidAgentName,
    "agent_prompt_stalled": HerdrPromptStalled,
    "timeout": HerdrTimeout,
}


def classify(
    code: Optional[str],
    message: str,
    *,
    argv: Optional[Sequence[str]] = None,
    returncode: Optional[int] = None,
    stdout: str = "",
    stderr: str = "",
) -> HerdrError:
    """根据 herdr 的 error.code 构造合适的异常对象。"""
    cls = CODE_TO_EXCEPTION.get(code or "")
    if cls is None:
        if code and code.endswith("_not_found"):
            cls = HerdrNotFound
        else:
            cls = HerdrError
    return cls(
        message,
        code=code,
        argv=argv,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )
