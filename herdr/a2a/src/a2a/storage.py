"""状态目录的存储用量与提示(路线图 189,19 号方案 E 节 1)。只统计、只提示,不自动处理。

计入:done/、archive/、审计当前文件与全部分段、幂等记录,以及整个状态目录合计和所在磁盘的剩余空间。
默认阈值(用户 2026-10-12 确认):保留 30 天、审计当前文件 100 MB 轮转、磁盘剩余 10% 提示。
"""
from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .archive import _parse_time, archive_dir
from .audit import AuditLog
from .spool import Spool

DEFAULT_RETENTION_DAYS = 30
DEFAULT_AUDIT_MAX_MB = 100
DISK_FREE_WARN_RATIO = 0.10


def _files(path: Path, pattern: str = "**/*") -> Tuple[int, int]:
    """(文件数, 字节数)。只读统计,不持锁:与归档 / 轮转并发时文件可能刚被删除或改名,跳过即可。"""
    if not path.is_dir():
        return 0, 0
    count = size = 0
    for p in path.glob(pattern):
        try:
            if p.is_file():
                size += p.stat().st_size
                count += 1
        except FileNotFoundError:
            pass
    return count, size


def usage(state_dir: Path, spool: Spool, audit: AuditLog, *, now: "datetime | None" = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    done_count, done_bytes = _files(spool.done_dir, "*.json")
    _, archive_bytes = _files(archive_dir(spool))
    idem_count, idem_bytes = _files(state_dir / "idempotency", "*/*.json")
    _, state_bytes = _files(state_dir)
    try:
        audit_current = audit.path.stat().st_size
    except FileNotFoundError:
        audit_current = 0
    probe = state_dir
    while not probe.exists() and probe != probe.parent:      # 状态目录还没建:看它将来所在的磁盘
        probe = probe.parent
    disk = shutil.disk_usage(probe)
    cutoff = now - timedelta(days=DEFAULT_RETENTION_DAYS)
    old_done = 0
    for path in (spool.done_dir.glob("*.json") if spool.done_dir.is_dir() else []):
        try:
            finished = _parse_time(spool._load(path).updated_at)
        except Exception:                      # 刚被归档删除,或读不出:统计时跳过(归档本身会 fail-closed)
            continue
        if finished is not None and finished <= cutoff:
            old_done += 1
    hints: List[str] = []
    if disk.free < disk.total * DISK_FREE_WARN_RATIO:
        hints.append(f"状态目录所在磁盘剩余 {disk.free * 100 // disk.total}%,低于 {int(DISK_FREE_WARN_RATIO * 100)}%")
    if audit_current > DEFAULT_AUDIT_MAX_MB * 1024 * 1024:
        hints.append(f"审计当前文件超过 {DEFAULT_AUDIT_MAX_MB} MB,可执行 a2a audit rotate")
    if old_done:
        hints.append(f"有 {old_done} 条终态记录超过 {DEFAULT_RETENTION_DAYS} 天,"
                     f"可执行 a2a spool archive --older-than {DEFAULT_RETENTION_DAYS}")
    return {
        "state_dir": str(state_dir),
        "done": {"files": done_count, "bytes": done_bytes, "older_than_retention": old_done},
        "archive_bytes": archive_bytes,
        "audit": {"current_bytes": audit_current, "total_bytes": audit.size()},
        "idempotency": {"records": idem_count, "bytes": idem_bytes},
        "state_dir_bytes": state_bytes,
        "disk": {"total_bytes": disk.total, "free_bytes": disk.free},
        "hints": hints,
    }
