"""herdr CLI 的薄封装。

只做一件事:把 herdr 子命令变成 Python 方法,并把输出和错误整理好。
不包含任何业务逻辑(没有拓扑、路由、模板、队列)。

设计要点:
  * 通过 subprocess 调 herdr CLI,不依赖 jq,不直接连 socket。
  * 所有命令都可以带 --session,用来指向命名会话,避免碰到默认会话。
  * 失败一律抛出 errors.py 里的异常;成功返回 result 里的内容(dict / list / str)。
  * runner 可注入,单元测试不需要真的运行 herdr。
  * 依据的是虚拟机里 herdr 0.9.3 的实测行为,见 herdr/claude/04-design-python-framework.md。
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from .errors import (
    HerdrBinaryNotFound,
    HerdrError,
    HerdrServerNotRunning,
    HerdrTimeout,
    HerdrUsageError,
    classify,
)

# agent 状态
AGENT_STATUSES = ("idle", "working", "blocked", "done", "unknown")

# 可以接收新输入的状态。
# 实测:done 不会自己变回 idle,所以等待"可投递"时必须同时带上 idle 和 done。
READY_STATUSES = ("idle", "done")
# 等待"可投递,或已卡住":用于一次 wait 就能分流 READY / BLOCKED。
READY_OR_BLOCKED = ("idle", "done", "blocked")

PANE_READ_SOURCES = ("visible", "recent", "recent-unwrapped")
AGENT_READ_SOURCES = PANE_READ_SOURCES + ("detection",)
READ_FORMATS = ("text", "ansi")
SPLIT_DIRECTIONS = ("right", "down")

DEFAULT_TIMEOUT_S = 30.0
# herdr 侧的 timeout 到期后,留给子进程退出的额外时间
_TIMEOUT_MARGIN_S = 10.0

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

Runner = Callable[[Sequence[str], Optional[float]], "subprocess.CompletedProcess[str]"]


def _default_runner(argv: Sequence[str], timeout: Optional[float]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout)


@dataclass(frozen=True)
class Created:
    """创建 workspace / tab / pane 之后拿到的 ID。

    创建时就把 pane_id 记下来,不要事后去猜。raw 里是 herdr 返回的完整 result。
    """

    raw: Dict[str, Any]
    workspace_id: Optional[str]
    tab_id: Optional[str]
    pane_id: Optional[str]


def _created_from(result: Mapping[str, Any]) -> Created:
    pane = result.get("root_pane") or result.get("pane") or {}
    tab = result.get("tab") or {}
    workspace = result.get("workspace") or {}
    return Created(
        raw=dict(result),
        workspace_id=workspace.get("workspace_id") or tab.get("workspace_id") or pane.get("workspace_id"),
        tab_id=tab.get("tab_id") or pane.get("tab_id"),
        pane_id=pane.get("pane_id"),
    )


def _try_json(text: str) -> Any:
    text = (text or "").strip()
    if not text or text[0] not in "{[":
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _env_args(env: Optional[Mapping[str, str]]) -> List[str]:
    args: List[str] = []
    for key, value in (env or {}).items():
        if not _ENV_KEY_RE.match(key):
            raise ValueError(f"非法的环境变量名: {key!r}")
        args += ["--env", f"{key}={value}"]
    return args


def _focus_args(focus: Optional[bool]) -> List[str]:
    if focus is None:
        return []
    return ["--focus"] if focus else ["--no-focus"]


def _check_target(value: str, what: str = "目标") -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{what}不能为空")
    if value.startswith("-"):
        # 防止被 herdr 当成选项解析
        raise ValueError(f"{what}不能以 '-' 开头: {value!r}")
    return value


def _timeout_args(timeout_ms: Optional[int]) -> List[str]:
    if timeout_ms is None:
        return []
    if timeout_ms <= 0:
        raise ValueError("timeout_ms 必须大于 0")
    return ["--timeout", str(int(timeout_ms))]


class HerdrClient:
    """herdr CLI 客户端。

    session=None 时沿用 herdr 自己的解析顺序(环境变量或默认会话)。
    做实验时请显式传入命名会话,避免碰到默认会话。
    """

    def __init__(
        self,
        session: Optional[str] = None,
        *,
        herdr_bin: str = "herdr",
        timeout: float = DEFAULT_TIMEOUT_S,
        runner: Optional[Runner] = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.session = session
        self.herdr_bin = herdr_bin
        self.timeout = timeout
        self._runner = runner or _default_runner
        self._sleep = sleep
        self._monotonic = monotonic

    # ------------------------------------------------------------------
    # 底层调用
    # ------------------------------------------------------------------
    def _argv(self, args: Sequence[str]) -> List[str]:
        argv = [self.herdr_bin]
        if self.session:
            argv += ["--session", self.session]
        argv += [str(a) for a in args]
        return argv

    def _client_timeout(self, timeout_ms: Optional[int]) -> float:
        if timeout_ms is None:
            return self.timeout
        return max(self.timeout, timeout_ms / 1000.0 + _TIMEOUT_MARGIN_S)

    def _run(self, argv: Sequence[str], timeout: Optional[float]) -> "subprocess.CompletedProcess[str]":
        try:
            return self._runner(argv, timeout)
        except FileNotFoundError as exc:
            raise HerdrBinaryNotFound(f"找不到 herdr 可执行文件: {self.herdr_bin}", argv=argv) from exc
        except subprocess.TimeoutExpired as exc:
            raise HerdrTimeout(
                f"herdr 命令超过 {timeout} 秒没有返回",
                code="client_timeout",
                argv=argv,
            ) from exc

    def _fail(self, argv: Sequence[str], proc: "subprocess.CompletedProcess[str]") -> HerdrError:
        out, err = proc.stdout or "", proc.stderr or ""
        envelope = _try_json(out) or _try_json(err)
        if isinstance(envelope, dict) and isinstance(envelope.get("error"), dict):
            e = envelope["error"]
            return classify(
                e.get("code"),
                str(e.get("message", "")),
                argv=argv,
                returncode=proc.returncode,
                stdout=out,
                stderr=err,
            )
        if proc.returncode == 2:
            return HerdrUsageError(
                (err or out).strip() or "命令行用法错误",
                argv=argv,
                returncode=proc.returncode,
                stdout=out,
                stderr=err,
            )
        return HerdrError(
            (err or out).strip() or f"herdr 退出码 {proc.returncode}",
            argv=argv,
            returncode=proc.returncode,
            stdout=out,
            stderr=err,
        )

    def _call(self, args: Sequence[str], *, timeout_ms: Optional[int] = None) -> Any:
        """执行命令,期望 JSON。成功返回 result 的内容,没有输出时返回 {}。"""
        argv = self._argv(args)
        proc = self._run(argv, self._client_timeout(timeout_ms))
        out, err = proc.stdout or "", proc.stderr or ""
        envelope = _try_json(out) or _try_json(err)
        # 即使退出码是 0,只要带 error 就算失败
        if isinstance(envelope, dict) and isinstance(envelope.get("error"), dict):
            raise self._fail(argv, proc)
        if proc.returncode != 0:
            raise self._fail(argv, proc)
        if envelope is None:
            if not out.strip():
                return {}
            raise HerdrError(
                f"herdr 返回了无法解析的输出: {out.strip()[:200]}",
                argv=argv,
                returncode=proc.returncode,
                stdout=out,
                stderr=err,
            )
        if isinstance(envelope, dict) and "result" in envelope:
            return envelope["result"]
        return envelope

    def _call_text(self, args: Sequence[str], *, timeout_ms: Optional[int] = None) -> str:
        """执行命令,期望纯文本(例如读取屏幕)。成功时原样返回 stdout。"""
        argv = self._argv(args)
        proc = self._run(argv, self._client_timeout(timeout_ms))
        if proc.returncode != 0:
            raise self._fail(argv, proc)
        return proc.stdout or ""

    # ------------------------------------------------------------------
    # 会话 / 服务
    # ------------------------------------------------------------------
    def is_server_running(self) -> bool:
        """当前 session 的 server 是否在运行。"""
        try:
            self.workspace_list()
        except HerdrServerNotRunning:
            return False
        return True

    # ------------------------------------------------------------------
    # workspace
    # ------------------------------------------------------------------
    def workspace_list(self) -> List[Dict[str, Any]]:
        return list(self._call(["workspace", "list"]).get("workspaces", []))

    def workspace_get(self, workspace_id: str) -> Dict[str, Any]:
        return self._call(["workspace", "get", _check_target(workspace_id, "workspace_id")])

    def workspace_create(
        self,
        *,
        cwd: Optional[str] = None,
        label: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        focus: Optional[bool] = False,
    ) -> Created:
        args: List[str] = ["workspace", "create"]
        if cwd is not None:
            args += ["--cwd", cwd]
        if label is not None:
            args += ["--label", label]
        args += _env_args(env) + _focus_args(focus)
        return _created_from(self._call(args))

    def workspace_rename(self, workspace_id: str, label: str) -> Dict[str, Any]:
        return self._call(["workspace", "rename", _check_target(workspace_id, "workspace_id"), label])

    def workspace_focus(self, workspace_id: str) -> Dict[str, Any]:
        return self._call(["workspace", "focus", _check_target(workspace_id, "workspace_id")])

    def workspace_close(self, workspace_id: str, *, group: bool = False) -> Dict[str, Any]:
        args = ["workspace", "close", _check_target(workspace_id, "workspace_id")]
        if group:
            # 会连带关闭关联的 worktree workspace,只有明确需要时才用
            args.append("--group")
        return self._call(args)

    # ------------------------------------------------------------------
    # tab
    # ------------------------------------------------------------------
    def tab_list(self, workspace_id: Optional[str] = None) -> List[Dict[str, Any]]:
        args = ["tab", "list"]
        if workspace_id:
            args += ["--workspace", _check_target(workspace_id, "workspace_id")]
        return list(self._call(args).get("tabs", []))

    def tab_get(self, tab_id: str) -> Dict[str, Any]:
        return self._call(["tab", "get", _check_target(tab_id, "tab_id")])

    def tab_create(
        self,
        *,
        workspace_id: Optional[str] = None,
        cwd: Optional[str] = None,
        label: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        focus: Optional[bool] = False,
    ) -> Created:
        """新建 tab。返回值里的 pane_id 是新 tab 的第一个 pane。"""
        args: List[str] = ["tab", "create"]
        if workspace_id:
            args += ["--workspace", _check_target(workspace_id, "workspace_id")]
        if cwd is not None:
            args += ["--cwd", cwd]
        if label is not None:
            args += ["--label", label]
        args += _env_args(env) + _focus_args(focus)
        return _created_from(self._call(args))

    def tab_rename(self, tab_id: str, label: str) -> Dict[str, Any]:
        return self._call(["tab", "rename", _check_target(tab_id, "tab_id"), label])

    def tab_focus(self, tab_id: str) -> Dict[str, Any]:
        return self._call(["tab", "focus", _check_target(tab_id, "tab_id")])

    def tab_close(self, tab_id: str) -> Dict[str, Any]:
        return self._call(["tab", "close", _check_target(tab_id, "tab_id")])

    # ------------------------------------------------------------------
    # pane
    # ------------------------------------------------------------------
    def pane_list(self, workspace_id: Optional[str] = None) -> List[Dict[str, Any]]:
        args = ["pane", "list"]
        if workspace_id:
            args += ["--workspace", _check_target(workspace_id, "workspace_id")]
        return list(self._call(args).get("panes", []))

    def pane_get(self, pane_id: str) -> Dict[str, Any]:
        return self._call(["pane", "get", _check_target(pane_id, "pane_id")])

    def pane_split(
        self,
        pane_id: Optional[str] = None,
        *,
        direction: str = "right",
        cwd: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        ratio: Optional[float] = None,
        focus: Optional[bool] = False,
    ) -> Created:
        """拆分 pane,返回新 pane 的 ID。pane_id 为空时拆分当前 pane(--current)。"""
        if direction not in SPLIT_DIRECTIONS:
            raise ValueError(f"direction 必须是 {SPLIT_DIRECTIONS} 之一")
        args: List[str] = ["pane", "split"]
        args += [_check_target(pane_id, "pane_id")] if pane_id else ["--current"]
        args += ["--direction", direction]
        if ratio is not None:
            args += ["--ratio", str(ratio)]
        if cwd is not None:
            args += ["--cwd", cwd]
        args += _env_args(env) + _focus_args(focus)
        return _created_from(self._call(args))

    def pane_rename(self, pane_id: str, label: Optional[str]) -> Dict[str, Any]:
        """设置 pane 的显示名。label 为 None 时清除。只用于给人看,不能用来查找。"""
        _check_target(pane_id, "pane_id")
        if label is None:
            return self._call(["pane", "rename", pane_id, "--clear"])
        return self._call(["pane", "rename", pane_id, label])

    def pane_close(self, pane_id: str) -> Dict[str, Any]:
        return self._call(["pane", "close", _check_target(pane_id, "pane_id")])

    def pane_process_info(self, pane_id: str) -> Dict[str, Any]:
        """前台进程信息。注意必须用 --pane,位置参数会被 herdr 当成未知选项。"""
        result = self._call(["pane", "process-info", "--pane", _check_target(pane_id, "pane_id")])
        return dict(result.get("process_info", result))

    def pane_run(self, pane_id: str, command: str) -> Dict[str, Any]:
        """把 command 加回车原子地写入 pane 的终端。不检查里面运行的是什么。"""
        return self._call(["pane", "run", _check_target(pane_id, "pane_id"), command])

    def pane_send_text(self, pane_id: str, text: str) -> Dict[str, Any]:
        """只写入文字,不回车。

        注意:`send_text` 再 `send_keys("enter")` 不是原子操作。实测 TUI 类 agent 需要
        约 0.5 秒间隔才会处理随后的回车,否则文字会停在输入框里不执行。
        要向 agent 发消息请用 agent_prompt(原子地写入文字和回车,并检查对方状态)。
        """
        return self._call(["pane", "send-text", _check_target(pane_id, "pane_id"), text])

    def pane_send_keys(self, pane_id: str, *keys: str) -> Dict[str, Any]:
        if not keys:
            raise ValueError("至少需要一个按键")
        return self._call(["pane", "send-keys", _check_target(pane_id, "pane_id"), *keys])

    def pane_read(
        self,
        pane_id: str,
        *,
        source: str = "recent-unwrapped",
        lines: Optional[int] = None,
        fmt: str = "text",
    ) -> str:
        """读取 pane 的屏幕文字。返回纯文本,原样不解析。"""
        if source not in PANE_READ_SOURCES:
            raise ValueError(f"source 必须是 {PANE_READ_SOURCES} 之一")
        if fmt not in READ_FORMATS:
            raise ValueError(f"fmt 必须是 {READ_FORMATS} 之一")
        args = ["pane", "read", _check_target(pane_id, "pane_id"), "--source", source, "--format", fmt]
        if lines is not None:
            args += ["--lines", str(int(lines))]
        return self._call_text(args)

    def pane_wait_output(
        self,
        pane_id: str,
        *,
        match: Optional[str] = None,
        regex: Optional[str] = None,
        source: Optional[str] = None,
        lines: Optional[int] = None,
        timeout_ms: Optional[int] = None,
    ) -> Any:
        """等待 pane 输出里出现某段文字(match,字面子串)或匹配正则(regex)。

        注意:已存在的文字也会立即匹配;它不解析 agent 生命周期。
        """
        if (match is None) == (regex is None):
            raise ValueError("match 和 regex 必须二选一")
        args = ["pane", "wait-output", _check_target(pane_id, "pane_id")]
        args += ["--match", match] if match is not None else ["--regex", regex]  # type: ignore[list-item]
        if source is not None:
            if source not in PANE_READ_SOURCES:
                raise ValueError(f"source 必须是 {PANE_READ_SOURCES} 之一")
            args += ["--source", source]
        if lines is not None:
            args += ["--lines", str(int(lines))]
        args += _timeout_args(timeout_ms)
        return self._call(args, timeout_ms=timeout_ms)

    # ------------------------------------------------------------------
    # agent
    # ------------------------------------------------------------------
    def agent_list(self) -> List[Dict[str, Any]]:
        return list(self._call(["agent", "list"]).get("agents", []))

    def agent_get(self, target: str) -> Dict[str, Any]:
        """target 是 agent 名字或承载它的 pane ID。"""
        result = self._call(["agent", "get", _check_target(target)])
        return dict(result.get("agent", result))

    def agent_rename(self, target: str, name: Optional[str]) -> Dict[str, Any]:
        """设置 agent 名字;name 为 None 时清除。

        实测:agent 退出后名字失效,重启后不会自动恢复,需要重新设置。
        名字须小写字母开头,只含小写字母、数字、'-'、'_',1 到 32 个字符,存活 agent 间唯一。
        """
        _check_target(target)
        if name is None:
            result = self._call(["agent", "rename", target, "--clear"])
        else:
            result = self._call(["agent", "rename", target, name])
        return dict(result.get("agent", result))

    def agent_focus(self, target: str) -> Dict[str, Any]:
        """聚焦。副作用:状态 done 会变成 idle(视为已查看)。"""
        result = self._call(["agent", "focus", _check_target(target)])
        return dict(result.get("agent", result))

    def agent_prompt(
        self,
        target: str,
        text: str,
        *,
        wait: bool = False,
        until: Optional[Iterable[str]] = None,
        timeout_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        """向 agent 提交一段提示词。

        对方 blocked 时 herdr 拒绝发送并抛出 HerdrAgentBlocked(不写入任何输入)。
        wait=True 必须同时给 timeout_ms,避免无限等待。
        超时或 HerdrPromptStalled 不代表没发出去:重试前先读状态和输出。
        """
        _check_target(target)
        if not isinstance(text, str) or not text:
            raise ValueError("text 不能为空")
        if wait and timeout_ms is None:
            raise ValueError("wait=True 时必须指定 timeout_ms")
        args = ["agent", "prompt", target, text]
        if wait:
            args.append("--wait")
        for status in until or ():
            args += ["--until", self._check_status(status)]
        args += _timeout_args(timeout_ms)
        result = self._call(args, timeout_ms=timeout_ms)
        return dict(result.get("agent", result))

    def agent_wait(
        self,
        target: str,
        *,
        until: Optional[Iterable[str]] = None,
        timeout_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        """在 herdr 服务端阻塞,直到 agent 进入指定状态之一。

        until 为空时使用 herdr 默认(idle / done / blocked 里最先到的)。
        等"可投递"请传 READY_STATUSES:只等 idle 时,done 状态会一直等到超时。
        超时抛出 HerdrTimeout。
        """
        args = ["agent", "wait", _check_target(target)]
        for status in until or ():
            args += ["--until", self._check_status(status)]
        args += _timeout_args(timeout_ms)
        result = self._call(args, timeout_ms=timeout_ms)
        return dict(result.get("agent", result))

    def agent_read(
        self,
        target: str,
        *,
        source: str = "recent-unwrapped",
        lines: Optional[int] = None,
        fmt: str = "text",
    ) -> str:
        if source not in AGENT_READ_SOURCES:
            raise ValueError(f"source 必须是 {AGENT_READ_SOURCES} 之一")
        if fmt not in READ_FORMATS:
            raise ValueError(f"fmt 必须是 {READ_FORMATS} 之一")
        args = ["agent", "read", _check_target(target), "--source", source, "--format", fmt]
        if lines is not None:
            args += ["--lines", str(int(lines))]
        return self._call_text(args)

    def agent_send_keys(self, target: str, *keys: str) -> Dict[str, Any]:
        if not keys:
            raise ValueError("至少需要一个按键")
        return self._call(["agent", "send-keys", _check_target(target), *keys])

    def agent_explain(self, target: str, *, verbose: bool = False) -> str:
        """herdr 对该 agent 状态判断的诊断文字。状态显示异常时用。"""
        args = ["agent", "explain", _check_target(target)]
        if verbose:
            args.append("--verbose")
        return self._call_text(args)

    @staticmethod
    def _check_status(status: str) -> str:
        if status not in AGENT_STATUSES:
            raise ValueError(f"状态必须是 {AGENT_STATUSES} 之一,收到 {status!r}")
        return status

    # ------------------------------------------------------------------
    # 组合便利方法(仍然只涉及 herdr 本身,不含业务逻辑)
    # ------------------------------------------------------------------
    def find_agent(self, pane_id: str) -> Optional[Dict[str, Any]]:
        """在 agent 列表里找某个 pane 上的 agent;没被识别时返回 None。"""
        for agent in self.agent_list():
            if agent.get("pane_id") == pane_id:
                return agent
        return None

    def wait_for_agent_detected(
        self,
        pane_id: str,
        *,
        timeout_s: float = 30.0,
        interval_s: float = 0.5,
    ) -> Dict[str, Any]:
        """轮询直到 herdr 把该 pane 识别为 agent,返回 agent 信息;超时抛 HerdrTimeout。

        启动 agent 之后、调用 agent_rename 之前使用:识别之前不能改名。
        """
        deadline = self._monotonic() + timeout_s
        while True:
            found = self.find_agent(pane_id)
            if found is not None:
                return found
            if self._monotonic() >= deadline:
                raise HerdrTimeout(
                    f"{timeout_s} 秒内 pane {pane_id} 没有被识别为 agent",
                    code="agent_not_detected",
                )
            self._sleep(interval_s)


# ----------------------------------------------------------------------
# session 管理(这些命令不带 --session,单独放成函数)
# ----------------------------------------------------------------------
def _session_cmd(
    args: Sequence[str],
    *,
    herdr_bin: str = "herdr",
    runner: Optional[Runner] = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> str:
    argv = [herdr_bin, "session", *args]
    run = runner or _default_runner
    try:
        proc = run(argv, timeout)
    except FileNotFoundError as exc:
        raise HerdrBinaryNotFound(f"找不到 herdr 可执行文件: {herdr_bin}", argv=argv) from exc
    except subprocess.TimeoutExpired as exc:
        raise HerdrTimeout(f"herdr session 命令超过 {timeout} 秒没有返回", code="client_timeout", argv=argv) from exc
    if proc.returncode != 0:
        envelope = _try_json(proc.stdout or "") or _try_json(proc.stderr or "")
        if isinstance(envelope, dict) and isinstance(envelope.get("error"), dict):
            e = envelope["error"]
            raise classify(e.get("code"), str(e.get("message", "")), argv=argv, returncode=proc.returncode,
                           stdout=proc.stdout or "", stderr=proc.stderr or "")
        raise HerdrError(
            ((proc.stderr or "") or (proc.stdout or "")).strip() or f"退出码 {proc.returncode}",
            argv=argv,
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
        )
    return proc.stdout or ""


def session_list(*, herdr_bin: str = "herdr", runner: Optional[Runner] = None) -> List[Dict[str, str]]:
    """列出所有会话。返回 [{"name":..., "status":..., "directory":..., "socket":...}]。

    解析 `herdr session list` 的表格输出(按空白分列,最后几列不含空格)。
    """
    text = _session_cmd(["list"], herdr_bin=herdr_bin, runner=runner)
    rows: List[Dict[str, str]] = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4:
            rows.append({"name": parts[0], "status": parts[1], "directory": parts[2], "socket": parts[3]})
    return rows


def session_stop(name: str, *, herdr_bin: str = "herdr", runner: Optional[Runner] = None) -> str:
    """停止命名会话。"default" 也可以停止,但请谨慎。"""
    return _session_cmd(["stop", _check_target(name, "会话名")], herdr_bin=herdr_bin, runner=runner)


def session_delete(name: str, *, herdr_bin: str = "herdr", runner: Optional[Runner] = None) -> str:
    """删除命名会话。默认会话不能删除(herdr 会拒绝)。"""
    if name == "default":
        raise ValueError("herdr 不支持删除默认会话")
    return _session_cmd(["delete", _check_target(name, "会话名")], herdr_bin=herdr_bin, runner=runner)


_VERSION_RE = re.compile(r"\bherdr\s+v?(\d+\.\d+\.\d+\S*)")


def herdr_version(*, herdr_bin: str = "herdr", runner: Optional[Runner] = None) -> str:
    """返回 `herdr --version` 报告的版本号(例如 "0.9.3")。

    Broker 启动时用它判断 08-protocol.md §5 的实测结论是否适用于当前 herdr。
    """
    argv = [herdr_bin, "--version"]
    run = runner or _default_runner
    try:
        proc = run(argv, DEFAULT_TIMEOUT_S)
    except FileNotFoundError as exc:
        raise HerdrBinaryNotFound(f"找不到 herdr 可执行文件: {herdr_bin}", argv=argv) from exc
    except subprocess.TimeoutExpired as exc:
        raise HerdrTimeout("herdr --version 没有返回", code="client_timeout", argv=argv) from exc
    text = (proc.stdout or "") + (proc.stderr or "")
    match = _VERSION_RE.search(text)
    if proc.returncode != 0 or match is None:
        raise HerdrError(f"无法解析 herdr 版本: {text.strip()[:200]}", argv=argv, returncode=proc.returncode,
                         stdout=proc.stdout or "", stderr=proc.stderr or "")
    return match.group(1)
