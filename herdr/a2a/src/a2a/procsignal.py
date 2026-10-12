"""按进程身份发信号(19 号方案 §A)。

stop 要给 agent 的前台进程组发 SIGTERM。只按 pgid 发信号时,"核对"与"发信号"之间若 pi 恰好退出、
pgid 被新进程组复用,信号会发给别人。Linux 上用 pidfd 锁定组长这个具体进程:

  快照组长(/proc/<pid>/stat 的启动时间、所在进程组,/proc/<pid>/cmdline 的 argv0)
  -> pidfd_open(组长) -> 再快照一次,必须与第一次相同(确认 pidfd 指向的就是快照的那个进程)
  -> pidfd_send_signal 给组长(对同一个进程无竞态) -> 成功才 killpg 覆盖组内其他进程

pidfd_send_signal 失败(组长已退出)就不再 killpg。剩余限制:pidfd 发信号成功与 killpg 之间,
组长退出、被回收、整个进程组变空且 pgid 被复用,killpg 仍会命中新组;窗口极窄,不声称彻底消除。

没有 pidfd(macOS、Python 3.8 及以下、内核 < 5.3)时 supported 为 False,调用方退回只用 killpg。
"""
from __future__ import annotations

import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class ProcInspectError(RuntimeError):
    """无法检查进程(权限不足、/proc 以 hidepid 挂载等)。与"进程已不在"不同,调用方必须 fail-closed。"""


@dataclass(frozen=True)
class ProcSnapshot:
    start_time: int   # /proc/<pid>/stat 第 22 个字段(开机以来的时钟滴答数)
    pgid: int         # 第 5 个字段
    argv0: str


class ProcSignaler:
    def __init__(self, proc_root: str = "/proc", *, enabled: bool = True) -> None:
        self.proc_root = Path(proc_root)
        self.supported = (enabled and hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal")
                          and (self.proc_root / "self" / "stat").exists())

    @classmethod
    def disabled(cls) -> "ProcSignaler":
        return cls(enabled=False)

    def snapshot(self, pid: int) -> Optional[ProcSnapshot]:
        """进程身份快照;进程不存在返回 None。"""
        try:
            stat = (self.proc_root / str(pid) / "stat").read_text()
            cmdline = (self.proc_root / str(pid) / "cmdline").read_bytes()
        except (FileNotFoundError, ProcessLookupError):
            return None
        except OSError as exc:
            raise ProcInspectError(f"无法读取 /proc/{pid}: {exc}") from exc
        # comm 字段可能含空格和括号:从最后一个 ')' 之后开始按空格切分,rest[0] 是第 3 个字段
        rest = stat[stat.rindex(")") + 2:].split()
        return ProcSnapshot(start_time=int(rest[19]), pgid=int(rest[2]),
                            argv0=cmdline.split(b"\0")[0].decode(errors="replace"))

    def group_members(self, pgid: int) -> list:
        """进程组 pgid 里还活着的进程(扫描 /proc)。组长退出后,shell 会收回终端,子进程变成看不见的
        后台进程(实测),只看 pane 的前台进程判断不出来。"""
        if self._hidepid():
            raise ProcInspectError(f"{self.proc_root} 以 hidepid 方式挂载,看不到全部进程,无法确认进程组 {pgid} 已清空")
        members = []
        for entry in self.proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                rest = (entry / "stat").read_text()
            except (FileNotFoundError, ProcessLookupError):
                continue   # 扫描期间退出了
            except OSError as exc:
                raise ProcInspectError(f"无法读取 {entry}/stat: {exc}") from exc
            rest = rest[rest.rindex(")") + 2:].split()
            if int(rest[2]) == pgid and rest[0] != "Z":   # 僵尸进程已经结束,只是还没被回收
                members.append(int(entry.name))
        return members

    def _hidepid(self) -> bool:
        """/proc 是否以 hidepid(1、2、invisible、noaccess)挂载:那样别的用户的进程整个不出现在 /proc 里。"""
        try:
            lines = (self.proc_root / "self" / "mountinfo").read_text().splitlines()
        except OSError as exc:
            raise ProcInspectError(f"无法读取 /proc 挂载信息: {exc}") from exc
        for line in lines:
            fields = line.split()
            if len(fields) > 4 and fields[4] == str(self.proc_root) and " - proc " in line:
                options = line.split(" - ", 1)[1].split()[-1].split(",")
                return any(o.startswith("hidepid=") and o not in ("hidepid=0", "hidepid=off") for o in options)
        return False

    def open(self, pid: int) -> int:
        """打开 pid 的 pidfd;进程不存在时抛 ProcessLookupError。"""
        return os.pidfd_open(pid)

    def send(self, fd: int, sig: int) -> None:
        """经 pidfd 发信号;进程已退出时抛 ProcessLookupError。"""
        signal.pidfd_send_signal(fd, sig)

    def close(self, fd: int) -> None:
        os.close(fd)
