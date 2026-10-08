"""fnx_dv/fnx_sw 的 Herdr 兼容启动命令。"""
from __future__ import annotations

import os
import re
import shlex
from typing import List, Sequence


class LauncherError(ValueError):
    pass


_EXEC = re.compile(r"(^|[;&|(){}]\s*|\s)exec(\s|$)")
_ARGV0 = re.compile(r"\$\{?0\}?(?![0-9A-Za-z_])|BASH_SOURCE")


def resolve_launcher_path(path: str) -> str:
    path = os.path.expandvars(os.path.expanduser(path))
    if not os.path.isabs(path):
        raise LauncherError(f"启动脚本必须使用绝对路径: {path!r}")
    if any(char in path for char in "'\"$`\\\n\r"):
        raise LauncherError(f"启动脚本路径包含不安全字符: {path!r}")
    return path


def build_launch_command(launcher: str, args: Sequence[str] = ()) -> str:
    path = resolve_launcher_path(launcher)
    inner = 'exec(){ builtin exec -a pi "$@"; }; source "%s"' % path
    command = "bash -c " + shlex.quote(inner)
    if args:
        inner += ' "$@"'
        command = "bash -c " + shlex.quote(inner)
        command += " _ " + " ".join(shlex.quote(arg) for arg in args)
    return command


def preflight_launcher(path: str) -> List[str]:
    try:
        path = resolve_launcher_path(path)
    except LauncherError as exc:
        return [str(exc)]
    if not os.path.isfile(path):
        return [f"启动脚本不存在: {path}"]
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as stream:
            lines = [line for line in stream.read().splitlines()
                     if line.strip() and not line.lstrip().startswith("#")]
    except OSError as exc:
        return [f"无法读取启动脚本: {exc}"]
    problems: List[str] = []
    if sum(bool(_EXEC.search(line)) for line in lines) != 1:
        problems.append("启动脚本必须恰好包含一处 exec")
    if any(_ARGV0.search(line) for line in lines):
        problems.append("启动脚本不能依赖 $0 或 BASH_SOURCE")
    return problems
