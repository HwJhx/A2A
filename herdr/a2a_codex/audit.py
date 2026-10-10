"""追加写 JSON Lines 的跨进程审计日志。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .messages import now_iso
from .paths import absolute_path, default_state_dir
from .storage import exclusive_lock


class AuditLogCorruptionError(RuntimeError):
    """审计日志含无法解析的完整行；涉及恢复/裁定的调用必须失败关闭。"""


class AuditLog:
    def __init__(self, path: Optional[str | Path] = None) -> None:
        raw = absolute_path(path, "审计日志路径") if path is not None else default_state_dir() / "audit.jsonl"
        self.path = raw
        self.lock_path = raw.with_name(raw.name + ".lock")

    def _repair_tail_unlocked(self) -> Optional[dict]:
        """隔离崩溃留下的坏尾行；合法但缺换行的末行只补换行。"""
        if not self.path.exists():
            return None
        with self.path.open("r+b") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            if size == 0:
                return None
            stream.seek(size - 1)
            if stream.read(1) == b"\n":
                return None

            cursor = size
            truncate_at = 0
            while cursor > 0:
                start = max(0, cursor - 8192)
                stream.seek(start)
                block = stream.read(cursor - start)
                newline = block.rfind(b"\n")
                if newline >= 0:
                    truncate_at = start + newline + 1
                    break
                cursor = start
            stream.seek(truncate_at)
            tail = stream.read()
            try:
                recovered = json.loads(tail.decode("utf-8"))
            except (UnicodeError, ValueError):
                recovered = None
            if isinstance(recovered, dict):
                stream.seek(0, os.SEEK_END)
                stream.write(b"\n")
                stream.flush()
                os.fsync(stream.fileno())
                return None
            # 保留原始字节供操作员检查；主日志仅移除不完整尾行，避免后续追加
            # 把它与下一条记录粘连。sidecar 可重复追加多个隔离片段。
            corrupt_path = self.path.with_name(self.path.name + ".corrupt")
            with corrupt_path.open("ab") as corrupt:
                corrupt.write(tail)
                corrupt.write(b"\n")
                corrupt.flush()
                os.fsync(corrupt.fileno())
            stream.truncate(truncate_at)
            stream.flush()
            os.fsync(stream.fileno())
            return {"event": "AUDIT_REPAIRED", "quarantine_location": str(corrupt_path),
                    "detail": "不完整审计尾行已隔离；原始字节保存在 sidecar"}

    def _append_unlocked(self, entry: Mapping[str, Any]) -> Dict[str, Any]:
        encoded = (json.dumps(dict(entry), ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        descriptor = os.open(str(self.path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            remaining = memoryview(encoded)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("审计日志写入未取得进展")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return dict(entry)

    def record(self, event: Mapping[str, Any]) -> Dict[str, Any]:
        entry: Dict[str, Any] = {"ts": now_iso()}
        entry.update(event)
        with exclusive_lock(self.lock_path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            repaired = self._repair_tail_unlocked()
            if repaired is not None:
                self._append_unlocked({"ts": now_iso(), **repaired})
            self._append_unlocked(entry)
        return entry

    def read(self, *, strict: bool = False) -> List[Dict[str, Any]]:
        """读取审计事件。

        默认将中间损坏行表示为协议定义的 ``CORRUPT_LINE`` 诊断事件；
        裁定恢复、派发恢复等安全敏感调用应使用 ``strict=True``，遇到损坏即失败。
        """
        records: List[Dict[str, Any]] = []
        with exclusive_lock(self.lock_path):
            repaired = self._repair_tail_unlocked()
            if repaired is not None:
                self._append_unlocked({"ts": now_iso(), **repaired})
            if not self.path.exists():
                return []
            corrupt_lines = []
            with self.path.open("rb") as stream:
                for line_number, raw in enumerate(stream, start=1):
                    if not raw.strip():
                        continue
                    try:
                        value = json.loads(raw.decode("utf-8"))
                        if not isinstance(value, dict):
                            raise ValueError("审计日志每行必须是 JSON object")
                        records.append(value)
                    except (UnicodeError, ValueError) as exc:
                        diagnostic = {"event": "CORRUPT_LINE", "line": line_number,
                                      "detail": raw.decode("utf-8", errors="replace")[:200],
                                      "error": str(exc)}
                        records.append(diagnostic)
                        corrupt_lines.append(line_number)
            if strict and corrupt_lines:
                raise AuditLogCorruptionError(
                    "审计日志包含无法解析的行 " + ", ".join(map(str, corrupt_lines))
                )
        return records

    def tail(self, count: int = 50) -> List[Dict[str, Any]]:
        if count < 0:
            raise ValueError("count 不能小于 0")
        return self.read()[-count:] if count else []
