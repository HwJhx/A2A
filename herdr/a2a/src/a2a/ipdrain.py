"""按 IP 的隔离:排空标记与两把锁(方案 herdr/claude/17-stage8-ip-add-remove-plan.md §3.2)。

  <状态目录>/locks/ip-<ip>.op.lock     IP 操作锁(独占):ip add / ip remove / ip undrain 全程持有,同一 IP 串行。
  <状态目录>/locks/ip-<ip>.drain.lock  IP 排空锁(共享 / 独占):
        Router 持共享锁完成"检查标记 → 入队",broker 持共享锁完成"检查 → 写 DISPATCHING";
        写 / 撤销标记持独占锁。独占锁要等所有共享锁释放,所以标记写入之后,之前开始的入队、
        DISPATCHING 都已落盘可见,之后的 Router / broker 一定能看到标记。
  <状态目录>/ip_draining/<ip>.json     排空标记(原子写入,持久化):存在时该 IP 不入队、不开始新投递。
        读不出的标记按"正在排空"处理(fail-closed)。
"""
from __future__ import annotations

import fcntl
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from ._fsutil import atomic_write_text, exclusive_lock

_SLUG = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")   # 与拓扑里 IP id 的规则相同


def _check(ip_id: str) -> str:
    if not isinstance(ip_id, str) or not _SLUG.fullmatch(ip_id):
        raise ValueError(f"IP id 格式非法: {ip_id!r}")
    return ip_id


def op_lock_path(state_dir: Path, ip_id: str) -> Path:
    return Path(state_dir) / "locks" / f"ip-{_check(ip_id)}.op.lock"


def drain_lock_path(state_dir: Path, ip_id: str) -> Path:
    return Path(state_dir) / "locks" / f"ip-{_check(ip_id)}.drain.lock"


def marker_path(state_dir: Path, ip_id: str) -> Path:
    return Path(state_dir) / "ip_draining" / f"{_check(ip_id)}.json"


@contextmanager
def op_lock(state_dir: Path, ip_id: str) -> Iterator[None]:
    with exclusive_lock(op_lock_path(state_dir, ip_id)):
        yield


@contextmanager
def drain_shared(state_dir: Path, ip_id: str) -> Iterator[None]:
    """共享锁:持有期间排空标记不会被写入或撤销。只包住几次本地文件操作。"""
    path = drain_lock_path(state_dir, ip_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def drain_exclusive(state_dir: Path, ip_id: str) -> Iterator[None]:
    with exclusive_lock(drain_lock_path(state_dir, ip_id)):
        yield


def read_marker(state_dir: Path, ip_id: str) -> Optional[Dict[str, Any]]:
    """排空标记内容;没有标记返回 None;读不出时返回 {"unreadable": ...}(按正在排空处理)。"""
    path = marker_path(state_dir, ip_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:   # 无法访问(权限、路径错误等):不能当作"没有标记"
        return {"unreadable": f"{type(exc).__name__}: {exc}"}
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("不是 JSON 对象")
        return value
    except (OSError, ValueError) as exc:
        return {"unreadable": f"{type(exc).__name__}: {exc}"}


def is_draining(state_dir: Path, ip_id: str) -> bool:
    """只有明确确认标记不存在才返回 False;检查本身失败(权限、路径错误等)按正在排空处理(fail-closed)。"""
    try:
        os.stat(marker_path(state_dir, ip_id))
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def write_marker(state_dir: Path, ip_id: str, op_id: str, reason: str) -> None:
    """调用方须持有排空锁的独占锁。"""
    atomic_write_text(marker_path(state_dir, ip_id), json.dumps(
        {"ip": ip_id, "op_id": op_id, "reason": reason, "pid": os.getpid(),
         "at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False) + "\n")


def clear_marker(state_dir: Path, ip_id: str) -> None:
    """调用方须持有排空锁的独占锁。"""
    try:
        marker_path(state_dir, ip_id).unlink()
    except FileNotFoundError:
        pass
