"""追加写 JSON Lines 的跨进程审计日志。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .messages import now_iso
from .paths import absolute_path, default_state_dir
from .storage import exclusive_lock


class AuditLog:
    def __init__(self, path: Optional[str | Path] = None) -> None:
        raw = absolute_path(path, "审计日志路径") if path is not None else default_state_dir() / "audit.jsonl"
        self.path = raw
        self.lock_path = raw.with_name(raw.name + ".lock")

    def _repair_tail_unlocked(self) -> None:
        """丢弃进程崩溃造成的最后一条不完整 JSONL 记录。"""
        if not self.path.exists():
            return
        with self.path.open("r+b") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            if size == 0:
                return
            stream.seek(size - 1)
            if stream.read(1) == b"\n":
                return

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
                return
            stream.truncate(truncate_at)
            stream.flush()
            os.fsync(stream.fileno())

    def record(self, event: Mapping[str, Any]) -> Dict[str, Any]:
        entry: Dict[str, Any] = {"ts": now_iso()}
        entry.update(event)
        encoded = (json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        with exclusive_lock(self.lock_path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._repair_tail_unlocked()
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
        return entry

    def read(self) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        with exclusive_lock(self.lock_path):
            self._repair_tail_unlocked()
            if not self.path.exists():
                return []
            with self.path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    if line.strip():
                        value = json.loads(line)
                        if not isinstance(value, dict):
                            raise ValueError("审计日志每行必须是 JSON object")
                        records.append(value)
        return records

    def tail(self, count: int = 50) -> List[Dict[str, Any]]:
        if count < 0:
            raise ValueError("count 不能小于 0")
        return self.read()[-count:] if count else []
