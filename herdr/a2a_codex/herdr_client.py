"""Herdr CLI 的第一版 Python 封装。

当前只通过 subprocess 调用 Herdr CLI，不包含业务路由、拓扑和队列。
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence

from .errors import (
    HerdrBinaryNotFound, HerdrError, HerdrPromptOutcomeUnknown, HerdrTimeout,
    HerdrUsageError, from_code,
)
from .models import Agent, ResourceRef, agent_model, resource_ref

Runner = Callable[[Sequence[str], Optional[float]], subprocess.CompletedProcess[str]]
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_STATUSES = {"idle", "working", "blocked", "done", "unknown"}


def _run(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout)


class HerdrClient:
    def __init__(self, session: Optional[str] = None, *, herdr_bin: str = "herdr",
                 timeout: float = 30.0, runner: Optional[Runner] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.session = session
        self.herdr_bin = herdr_bin
        self.timeout = timeout
        self._runner = runner or _run
        self._sleep = sleep
        self._monotonic = monotonic

    def _argv(self, *args: object) -> list[str]:
        prefix = [self.herdr_bin]
        if self.session:
            prefix += ["--session", self.session]
        return prefix + [str(x) for x in args]

    def _execute(self, args: Sequence[object], *, timeout: Optional[float] = None) -> subprocess.CompletedProcess[str]:
        argv = self._argv(*args)
        try:
            return self._runner(argv, self.timeout if timeout is None else timeout)
        except FileNotFoundError as exc:
            raise HerdrBinaryNotFound(f"找不到 Herdr 可执行文件: {self.herdr_bin}", argv=argv) from exc
        except subprocess.TimeoutExpired as exc:
            raise HerdrTimeout(f"Herdr 命令超时: {' '.join(argv)}", argv=argv) from exc

    @staticmethod
    def _json(text: str) -> Any:
        try:
            return json.loads((text or "").strip())
        except (TypeError, ValueError):
            return None

    def _error(self, argv: Sequence[str], proc: subprocess.CompletedProcess[str]) -> HerdrError:
        out, err = proc.stdout or "", proc.stderr or ""
        body = self._json(out) or self._json(err)
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            value = body["error"]
            return from_code(value.get("code"), str(value.get("message", "")),
                             argv=argv, returncode=proc.returncode, stdout=out, stderr=err)
        cls = HerdrUsageError if proc.returncode == 2 else HerdrError
        return cls((err or out).strip() or f"Herdr 退出码 {proc.returncode}",
                   argv=argv, returncode=proc.returncode, stdout=out, stderr=err)

    def _call(self, *args: object, timeout: Optional[float] = None) -> Any:
        proc = self._execute(args, timeout=timeout)
        argv = self._argv(*args)
        out, err = proc.stdout or "", proc.stderr or ""
        body = self._json(out) or self._json(err)
        if proc.returncode != 0 or (isinstance(body, dict) and "error" in body):
            raise self._error(argv, proc)
        if body is None:
            if not out.strip():
                return {}
            raise HerdrError("Herdr 返回了无法解析的输出", argv=argv, stdout=out, stderr=err)
        return body.get("result", body) if isinstance(body, dict) else body

    def _text(self, *args: object, timeout: Optional[float] = None) -> str:
        proc = self._execute(args, timeout=timeout)
        if proc.returncode != 0:
            raise self._error(self._argv(*args), proc)
        return proc.stdout or ""

    @staticmethod
    def _target(value: str) -> str:
        if not isinstance(value, str) or not value or value.startswith("-"):
            raise ValueError("目标 ID/name 不能为空且不能以 '-' 开头")
        return value

    @staticmethod
    def _env(env: Optional[Mapping[str, str]]) -> list[str]:
        result: list[str] = []
        for key, value in (env or {}).items():
            if not _ENV_KEY.match(key):
                raise ValueError(f"非法环境变量名: {key}")
            result += ["--env", f"{key}={value}"]
        return result

    @staticmethod
    def _focus(focus: Optional[bool]) -> list[str]:
        return [] if focus is None else (["--focus"] if focus else ["--no-focus"])

    # workspace
    def create_workspace(self, *, label: Optional[str] = None, cwd: Optional[str] = None,
                         env: Optional[Mapping[str, str]] = None, focus: bool = False) -> ResourceRef:
        args: list[object] = ["workspace", "create"]
        if label is not None: args += ["--label", label]
        if cwd is not None: args += ["--cwd", cwd]
        args += self._env(env) + self._focus(focus)
        return resource_ref(self._call(*args))

    def delete_workspace(self, workspace_id: str) -> Dict[str, Any]:
        return dict(self._call("workspace", "close", self._target(workspace_id)))

    def rename_workspace(self, workspace_id: str, label: str) -> Dict[str, Any]:
        return dict(self._call("workspace", "rename", self._target(workspace_id), label))

    # tab
    def create_tab(self, *, workspace_id: Optional[str] = None, label: Optional[str] = None,
                   cwd: Optional[str] = None, env: Optional[Mapping[str, str]] = None,
                   focus: bool = False) -> ResourceRef:
        args: list[object] = ["tab", "create"]
        if workspace_id: args += ["--workspace", self._target(workspace_id)]
        if label is not None: args += ["--label", label]
        if cwd is not None: args += ["--cwd", cwd]
        args += self._env(env) + self._focus(focus)
        return resource_ref(self._call(*args))

    def delete_tab(self, tab_id: str) -> Dict[str, Any]:
        return dict(self._call("tab", "close", self._target(tab_id)))

    def rename_tab(self, tab_id: str, label: str) -> Dict[str, Any]:
        return dict(self._call("tab", "rename", self._target(tab_id), label))

    # pane: Herdr 没有 pane create，使用 pane split 创建新 pane
    def create_pane(self, pane_id: str, *, direction: str = "right", cwd: Optional[str] = None,
                    env: Optional[Mapping[str, str]] = None, focus: bool = False,
                    ratio: Optional[float] = None) -> ResourceRef:
        if direction not in {"right", "down"}:
            raise ValueError("direction 必须是 right 或 down")
        args: list[object] = ["pane", "split", self._target(pane_id), "--direction", direction]
        if cwd is not None: args += ["--cwd", cwd]
        if ratio is not None: args += ["--ratio", str(ratio)]
        args += self._env(env) + self._focus(focus)
        return resource_ref(self._call(*args))

    def delete_pane(self, pane_id: str) -> Dict[str, Any]:
        return dict(self._call("pane", "close", self._target(pane_id)))

    def pane_process_info(self, pane_id: str) -> Dict[str, Any]:
        result = self._call("pane", "process-info", "--pane", self._target(pane_id))
        value = result.get("process_info", result) if isinstance(result, dict) else result
        return dict(value)

    def pane_process_info(self, pane_id: str) -> Dict[str, Any]:
        result = self._call("pane", "process-info", "--pane", self._target(pane_id))
        value = result.get("process_info", result) if isinstance(result, dict) else result
        return dict(value)

    def rename_pane(self, pane_id: str, label: Optional[str]) -> Dict[str, Any]:
        target = self._target(pane_id)
        return dict(self._call("pane", "rename", target, "--clear" if label is None else label))

    # agent
    def start_agent(self, pane_id: str, command: str) -> Dict[str, Any]:
        """在 pane 前台启动 agent；fnx 应传入 exec -a pi 的启动命令。"""
        if not command:
            raise ValueError("command 不能为空")
        return dict(self._call("pane", "run", self._target(pane_id), command))

    def get_agent(self, target: str) -> Agent:
        return agent_model(self._call("agent", "get", self._target(target)))

    def list_agents(self) -> list[Agent]:
        result = self._call("agent", "list")
        return [agent_model(item) for item in result.get("agents", [])]

    def rename_agent(self, target: str, name: Optional[str]) -> Agent:
        args: list[object] = ["agent", "rename", self._target(target)]
        args.append("--clear" if name is None else name)
        if name == "":
            raise ValueError("agent name 不能为空；清除名称请传 None")
        return agent_model(self._call(*args))

    def find_agent(self, pane_id: str) -> Optional[Agent]:
        return next((agent for agent in self.list_agents() if agent.pane_id == pane_id), None)

    def wait_for_agent_detected(self, pane_id: str, *, timeout_s: float = 30.0,
                                interval_s: float = 0.5) -> Agent:
        deadline = self._monotonic() + timeout_s
        while True:
            agent = self.find_agent(pane_id)
            if agent is not None:
                return agent
            if self._monotonic() >= deadline:
                raise HerdrTimeout(f"{timeout_s} 秒内未识别 pane {pane_id}", code="agent_not_detected")
            self._sleep(interval_s)

    def read_agent(self, target: str, *, source: str = "recent-unwrapped",
                   lines: Optional[int] = None) -> str:
        if source not in {"visible", "recent", "recent-unwrapped", "detection"}:
            raise ValueError("不支持的 agent read source")
        args: list[object] = ["agent", "read", self._target(target), "--source", source, "--format", "text"]
        if lines is not None: args += ["--lines", str(lines)]
        return self._text(*args)

    def send_prompt(self, target: str, prompt: str, *, wait: bool = False,
                    timeout_ms: Optional[int] = None) -> Agent:
        if not prompt:
            raise ValueError("prompt 不能为空")
        if wait and timeout_ms is None:
            raise ValueError("wait=True 时必须提供 timeout_ms，避免无界等待")
        args: list[object] = ["agent", "prompt", self._target(target), prompt]
        if wait: args.append("--wait")
        if timeout_ms is not None: args += ["--timeout", str(timeout_ms)]
        try:
            return agent_model(self._call(*args, timeout=(timeout_ms / 1000 + 10) if timeout_ms else None))
        except HerdrPromptOutcomeUnknown:
            raise
        except HerdrTimeout as exc:
            # The CLI may have submitted the prompt before its wait or response timed out.
            raise HerdrPromptOutcomeUnknown(
                f"prompt 结果不确定；可能已送达，重试前请核查目标 agent: {exc}",
                code=exc.code or "client_timeout", argv=exc.argv,
                returncode=exc.returncode, stdout=exc.stdout, stderr=exc.stderr,
            ) from exc

    def wait_agent(self, target: str, *, until: Iterable[str] = ("idle", "done", "blocked"),
                   timeout_ms: Optional[int] = None) -> Agent:
        statuses = list(until)
        if not statuses or any(value not in _STATUSES for value in statuses):
            raise ValueError(f"until 必须是 {_STATUSES} 中的状态")
        args: list[object] = ["agent", "wait", self._target(target)]
        for status in statuses: args += ["--until", status]
        if timeout_ms is not None: args += ["--timeout", str(timeout_ms)]
        return agent_model(self._call(*args, timeout=(timeout_ms / 1000 + 10) if timeout_ms else None))
