"""审计日志:追加写的 JSON Lines 文件。

每个事件一行 JSON。被拒绝的发送也必须记录(含原因),这是排查"谁试图越权"的依据。

保证:
  * 多个进程同时追加,在跨进程文件锁里一次性写入整行,不会交织。
  * 进程在写到一半时崩溃,会留下没有换行结尾的半行。**下一次追加之前,这段残片会被移到
    <日志>.corrupt 隔离保存(不丢弃证据),再从日志里截掉**,并补记一条 AUDIT_REPAIRED 事件;
    否则新记录会被拼进坏行,一起丢失。
  * read() 对中间损坏的行是容忍的:该行以 CORRUPT_LINE 事件的形式返回,不会让整份日志无法读取。
    read() 本身**不修改文件**(修复只发生在追加时)。

尾行修复的做法移植自 herdr/a2a_codex,并改为隔离保存而不是直接丢弃。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from ._fsutil import exclusive_lock
from .messages import now_iso
from .paths import default_audit_path


def fingerprint(text: str) -> str:
    """损坏内容的指纹:操作员作废一条读不出的裁定时用它来指代(08 §7.3 规则 8)。"""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def corrupt_entry(line: str) -> Dict[str, Any]:
    """无法解析的一行。mentions_ruling 表示它可能是一条操作员裁定(08 §7.3 规则 7:读不出的裁定要 fail-closed)。"""
    return {"ts": "", "state": "CORRUPT_LINE", "detail": line[:200], "fingerprint": fingerprint(line),
            "mentions_ruling": "OPERATOR_RULING" in line}


class AuditLog:
    def __init__(self, path: "Optional[str | Path]" = None) -> None:
        raw = Path(path) if path is not None else default_audit_path()
        raw = raw.expanduser()
        if not raw.is_absolute():
            raise ValueError(f"审计日志路径必须是绝对路径: {raw}")
        self.path = raw
        self.lock_path = raw.with_name(raw.name + ".lock")
        self.corrupt_path = raw.with_name(raw.name + ".corrupt")

    # ---- 写 ----------------------------------------------------------
    @staticmethod
    def _write_all(fd: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("审计日志写入没有取得进展")
            view = view[written:]

    def _append_unlocked(self, entry: Mapping[str, Any]) -> None:
        line = (json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        fd = os.open(str(self.path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
            self._write_all(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)

    def _repair_tail_unlocked(self) -> int:
        """若日志不是以换行结尾(上次写入中途崩溃),把最后那段残片隔离并截掉。返回被隔离的字节数。"""
        if not self.path.exists():
            return 0
        with self.path.open("r+b") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            if size == 0:
                return 0
            stream.seek(size - 1)
            if stream.read(1) == b"\n":
                return 0
            # 往回找最后一个换行,它之后的内容就是残片
            cursor, cut = size, 0
            while cursor > 0:
                start = max(0, cursor - 8192)
                stream.seek(start)
                block = stream.read(cursor - start)
                newline = block.rfind(b"\n")
                if newline >= 0:
                    cut = start + newline + 1
                    break
                cursor = start
            stream.seek(cut)
            fragment = stream.read(size - cut)
            # 先把残片安全地保存到隔离文件,保存成功后才从日志里截掉
            quarantine = os.open(str(self.corrupt_path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.fchmod(quarantine, 0o600)
                self._write_all(quarantine, f"# {now_iso()} 隔离 {len(fragment)} 字节\n".encode("utf-8") + fragment + b"\n")
                os.fsync(quarantine)
            finally:
                os.close(quarantine)
            stream.truncate(cut)
            stream.flush()
            os.fsync(stream.fileno())
            return len(fragment)

    def record(self, event: Mapping[str, Any]) -> Dict[str, Any]:
        entry: Dict[str, Any] = {"ts": now_iso()}
        entry.update(event)
        with exclusive_lock(self.lock_path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            repaired = self._repair_tail_unlocked()
            if repaired:
                self._append_unlocked({
                    "ts": now_iso(), "state": "AUDIT_REPAIRED",
                    "detail": f"上次写入中途崩溃,{repaired} 字节不完整记录已隔离到 {self.corrupt_path.name}",
                })
            self._append_unlocked(entry)
        return entry

    # ---- 读 ----------------------------------------------------------
    def read(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        entries: List[Dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except ValueError:
                    entries.append(corrupt_entry(line))
                    continue
                entries.append(value if isinstance(value, dict) else corrupt_entry(line))
        return entries

    def quarantined(self) -> List[str]:
        """被隔离到 <日志>.corrupt 的残片(每段一条)。"""
        if not self.corrupt_path.exists():
            return []
        fragments: List[str] = []
        current: List[str] = []
        for line in self.corrupt_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("# ") and "隔离" in line:
                if current:
                    fragments.append("\n".join(current))
                current = []
            elif line:
                current.append(line)
        if current:
            fragments.append("\n".join(current))
        return fragments

    def tail(self, n: int = 50) -> List[Dict[str, Any]]:
        if n < 0:
            raise ValueError("n 不能小于 0")
        return self.read()[-n:] if n else []
