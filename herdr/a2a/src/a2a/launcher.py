"""构造"覆盖 exec"的启动命令,并对启动脚本做预检。

背景(见 herdr/claude/04-design-python-framework.md §2.3):
  herdr 按前台进程的 argv[0] 识别 agent。fnx_dv / fnx_sw 的启动脚本最后一行是
  `exec "$LIBEXEC_DIR/forenyx-cli" "$@"`,进程 argv[0] 不是 pi,herdr 认不出。
  解决办法是不改 fnx 的安装包,而是在子 shell 里把 exec 换成 `exec -a pi`,
  再 source 它自己的启动脚本,让脚本照常设置环境变量、做授权检查,
  只是最终进程的 argv[0] 变成 pi。

这个办法依赖启动脚本满足两个前提,所以启动前必须预检:
  1. 脚本里只有一处 exec(因为替换后的 exec 函数会覆盖脚本内所有 exec 调用)
  2. 脚本不依赖 $0 / BASH_SOURCE(source 之后它们指向 bash)
"""
from __future__ import annotations

import os
import re
import shlex
from typing import List, Sequence

# 路径里出现这些字符,会破坏我们拼出来的引号结构,一律拒绝
_FORBIDDEN_PATH_CHARS = set("'\"$`\\\n\r")

_EXEC_RE = re.compile(r"(^|[;&|(){}]\s*|\s)exec(\s|$)")
_ARGV0_RE = re.compile(r"\$\{?0\}?(?![0-9A-Za-z_])|BASH_SOURCE")


class LauncherError(ValueError):
    """启动脚本路径不合法,或无法构造启动命令。"""


def resolve_launcher_path(path: str) -> str:
    """展开 ~ 和环境变量,并要求结果是绝对路径、且不含会破坏引号的字符。"""
    expanded = os.path.expandvars(os.path.expanduser(path))
    if not os.path.isabs(expanded):
        raise LauncherError(f"启动脚本必须是绝对路径: {path!r}")
    bad = sorted(set(expanded) & _FORBIDDEN_PATH_CHARS)
    if bad:
        raise LauncherError(f"启动脚本路径含有不允许的字符 {bad!r}: {expanded!r}")
    return expanded


def build_launch_command(launcher: str, args: Sequence[str] = ()) -> str:
    """返回写入 pane 终端的启动命令(交给 `herdr pane run`)。

    无参数时形如:
        bash -c 'exec(){ builtin exec -a pi "$@"; }; source "<launcher>"'
    有参数时参数追加在后面,并通过 "$@" 转发给启动脚本:
        bash -c 'exec(){ builtin exec -a pi "$@"; }; source "<launcher>" "$@"' _ <参数...>
    """
    path = resolve_launcher_path(launcher)
    inner = 'exec(){ builtin exec -a pi "$@"; }; source "%s"' % path
    if args:
        inner += ' "$@"'
    command = "bash -c " + shlex.quote(inner)
    if args:
        command += " _ " + " ".join(shlex.quote(a) for a in args)
    return command


def _code_lines(text: str) -> List[str]:
    """去掉整行注释和空行,返回有效代码行。"""
    lines = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(raw)
    return lines


def preflight_launcher(path: str) -> List[str]:
    """检查启动脚本是否满足覆盖 exec 的前提。返回问题列表,空列表表示通过。"""
    try:
        resolved = resolve_launcher_path(path)
    except LauncherError as exc:
        return [str(exc)]
    if not os.path.isfile(resolved):
        return [f"启动脚本不存在: {resolved}"]
    if not os.access(resolved, os.R_OK):
        return [f"启动脚本不可读: {resolved}"]
    try:
        with open(resolved, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        return [f"无法读取启动脚本 {resolved}: {exc}"]

    problems: List[str] = []
    code = _code_lines(text)

    exec_lines = [line.strip() for line in code if _EXEC_RE.search(line)]
    if len(exec_lines) == 0:
        problems.append("脚本里没有找到 exec 语句,无法通过覆盖 exec 改变进程的 argv[0]")
    elif len(exec_lines) > 1:
        problems.append(
            "脚本里有 %d 处 exec,覆盖会影响所有调用,必须人工确认: %s"
            % (len(exec_lines), " | ".join(exec_lines[:3]))
        )

    uses_argv0 = [line.strip() for line in code if _ARGV0_RE.search(line)]
    if uses_argv0:
        problems.append("脚本使用了 $0 或 BASH_SOURCE,被 source 后它们会指向 bash: " + " | ".join(uses_argv0[:3]))

    return problems
