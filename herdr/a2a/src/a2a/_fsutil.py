"""文件锁和原子写入。拓扑文件和注册表文件共用。

约定:
  * 读改写必须在 exclusive_lock 里完成,这样多个进程(agent 里的 a2a send、broker、人工命令)
    同时修改同一个文件时不会互相覆盖。
  * 写入一律先写同目录的临时文件、fsync,再 os.replace 覆盖目标,读的人永远看不到写了一半的文件。
  * 依赖 fcntl,适用于 macOS / Linux(本项目运行在 Linux 虚拟机里)。
"""
from __future__ import annotations

import fcntl
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def exclusive_lock(lock_path: Path) -> Iterator[None]:
    """独占文件锁。同一进程里的不同线程、不同进程之间都会互斥。

    不可重入:持有锁时不要再次对同一个 lock_path 加锁,否则会死锁。
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_write_text(path: Path, text: str) -> None:
    """原子地把 text 写入 path:临时文件 + fsync + os.replace + 目录 fsync。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
