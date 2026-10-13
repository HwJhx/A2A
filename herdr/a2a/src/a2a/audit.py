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

轮转(路线图 189,19 号方案 E 节 3):`rotate()` 在写锁(独占)内把当前文件整体改名为
<日志名>.d/<代次>.jsonl(一步原子改名,每条记录任一时刻只属于一个文件),新建空的当前文件,再压缩成
<代次>.jsonl.gz(写 .tmp → fsync → 解压核对逐字节一致 → 改名 → 删 .jsonl)。read() 在共享锁内按代次升序
读全部分段、最后读当前文件,每个文件按追加顺序(不按时间戳排序),所以内部逻辑看到的历史不因轮转而变。
分段损坏(.gz 解压失败、CRC 不符)或代次缺口时 read() 抛 AuditCorrupt(fail-closed),不返回不完整的历史。
"""
from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import os
import re
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple

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


class AuditCorrupt(RuntimeError):
    """已轮转的审计分段损坏或缺失,读不出完整历史。修复:从备份恢复该分段(README)。"""


# 规范的分段名:代次不带前导零;.tmp 是压缩中途的临时文件(读取时忽略,下次轮转清理)
_SEGMENT = re.compile(r"^([1-9]\d*)\.jsonl(\.gz)?$")
_SEGMENT_TMP = re.compile(r"^[1-9]\d*\.jsonl\.gz\.tmp$")


class AuditLog:
    def __init__(self, path: "Optional[str | Path]" = None) -> None:
        raw = Path(path) if path is not None else default_audit_path()
        raw = raw.expanduser()
        if not raw.is_absolute():
            raise ValueError(f"审计日志路径必须是绝对路径: {raw}")
        self.path = raw
        self.lock_path = raw.with_name(raw.name + ".lock")
        self.corrupt_path = raw.with_name(raw.name + ".corrupt")
        self.segment_dir = raw.with_name(raw.stem + ".d")     # audit.jsonl → audit.d/
        self.crash_hook: Optional[Callable[[str], None]] = None   # 测试用:在轮转各步之间注入崩溃

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
    @contextmanager
    def _shared_lock(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _parse(text: str) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        for line in text.splitlines():
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

    def _segments(self) -> List[Tuple[int, Path]]:
        """[(代次, 要读的文件)],按代次升序。同一代次 .jsonl 与 .jsonl.gz 并存(压缩到一半)时读 .jsonl。
        代次必须从 1 起连续,否则抛 AuditCorrupt。"""
        if not os.path.lexists(self.segment_dir):
            return []                           # 从未轮转过
        if not self.segment_dir.is_dir():
            # 存在但不是目录(误操作、恢复出错):不能当作"没有分段",否则会静默漏掉历史
            raise AuditCorrupt(f"{self.segment_dir} 存在但不是目录;读不出完整历史,请人工核对")
        found: Dict[int, Dict[str, Path]] = {}
        for entry in self.segment_dir.iterdir():
            m = _SEGMENT.match(entry.name)
            if m:
                found.setdefault(int(m.group(1)), {}).__setitem__("gz" if m.group(2) else "plain", entry)
            elif not _SEGMENT_TMP.match(entry.name):
                # 不认识的文件(被改名的分段、01.jsonl 之类):不能确定历史是否完整,不静默忽略
                raise AuditCorrupt(f"审计分段目录 {self.segment_dir} 里有不认识的文件 {entry.name!r};"
                                   f"读不出完整历史,请人工核对(分段只应命名为 <代次>.jsonl 或 <代次>.jsonl.gz)")
        gens = sorted(found)
        if gens and gens != list(range(1, gens[-1] + 1)):
            missing = sorted(set(range(1, gens[-1] + 1)) - set(gens))
            raise AuditCorrupt(f"审计分段代次有缺口:{self.segment_dir} 缺少第 {missing} 代;"
                               f"读不出完整历史,请从备份恢复")
        return [(g, found[g].get("plain") or found[g]["gz"]) for g in gens]

    def _read_segment(self, path: Path) -> str:
        if path.suffix != ".gz":
            return path.read_text(encoding="utf-8", errors="replace")
        try:
            with gzip.open(path, "rb") as stream:
                return stream.read().decode("utf-8", errors="replace")
        except (OSError, EOFError, zlib.error) as exc:
            raise AuditCorrupt(f"审计分段 {path} 损坏({type(exc).__name__}: {exc});"
                               f"读不出完整历史,请从备份恢复该分段") from exc

    def read(self) -> List[Dict[str, Any]]:
        """全部历史:已轮转的分段(代次升序)+ 当前文件,各按追加顺序。分段损坏抛 AuditCorrupt。"""
        with self._shared_lock():
            entries: List[Dict[str, Any]] = []
            for _, path in self._segments():
                entries += self._parse(self._read_segment(path))
            if self.path.exists():
                entries += self._parse(self.path.read_text(encoding="utf-8", errors="replace"))
            return entries

    # ---- 轮转 --------------------------------------------------------
    def _checkpoint(self, name: str) -> None:
        if self.crash_hook is not None:
            self.crash_hook(name)

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _compress_unlocked(self, gen: int) -> None:
        plain = self.segment_dir / f"{gen}.jsonl"
        final = self.segment_dir / f"{gen}.jsonl.gz"
        temp = self.segment_dir / f"{gen}.jsonl.gz.tmp"
        data = plain.read_bytes()
        with open(temp, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
                gz.write(data)
            raw.flush()
            os.fsync(raw.fileno())
        with gzip.open(temp, "rb") as check:
            if check.read() != data:
                raise OSError(f"压缩核对不一致:{temp}")
        os.replace(temp, final)
        self._fsync_dir(self.segment_dir)
        self._checkpoint("after_publish_gz")
        plain.unlink()
        self._fsync_dir(self.segment_dir)

    def _finish_pending_unlocked(self) -> List[int]:
        """补完上次中断的压缩:清理 .tmp;仍有 .jsonl 的代次重新压缩(已有的 .gz 视为未完成,覆盖)。"""
        segments = self._segments()             # 先校验:有不认识的文件就 fail-closed,不清理任何东西
        if not self.segment_dir.is_dir():
            return []
        for temp in self.segment_dir.iterdir():
            if _SEGMENT_TMP.match(temp.name):   # 只清理规范命名的压缩临时文件
                temp.unlink()
        redone = []
        for gen, path in segments:
            if path.suffix == ".jsonl":
                self._compress_unlocked(gen)
                redone.append(gen)
        return redone

    def size(self) -> int:
        """当前文件 + 全部分段(含未完成的)的字节数。只读统计:与轮转并发时文件可能刚被改名或删除,跳过即可。"""
        total = 0
        paths = [self.path] + (list(self.segment_dir.iterdir()) if self.segment_dir.is_dir() else [])
        for p in paths:
            try:
                total += p.stat().st_size
            except FileNotFoundError:
                pass
        return total

    def rotate(self, max_bytes: int) -> Dict[str, Any]:
        """当前文件超过 max_bytes 时轮转(并先补完上次中断的压缩)。返回做了什么。"""
        with exclusive_lock(self.lock_path):
            self.segment_dir.mkdir(parents=True, exist_ok=True)
            redone = self._finish_pending_unlocked()
            current = self.path.stat().st_size if self.path.exists() else 0
            result: Dict[str, Any] = {"rotated": False, "current_bytes": current, "max_bytes": max_bytes,
                                      "finished_pending": redone}
            if current <= max_bytes:
                return result
            repaired = self._repair_tail_unlocked()        # 残片先隔离,分段里只留完整的行
            if repaired:                                    # 与追加时的修复一样留下审计记录
                self._append_unlocked({
                    "ts": now_iso(), "state": "AUDIT_REPAIRED",
                    "detail": f"轮转前发现上次写入中途崩溃,{repaired} 字节不完整记录已隔离到 {self.corrupt_path.name}",
                })
            gen = (self._segments()[-1][0] if self._segments() else 0) + 1
            os.rename(self.path, self.segment_dir / f"{gen}.jsonl")
            self._fsync_dir(self.segment_dir)
            self._fsync_dir(self.path.parent)
            self._checkpoint("after_rename")
            os.close(os.open(str(self.path), os.O_WRONLY | os.O_CREAT, 0o600))
            self._compress_unlocked(gen)
            result.update(rotated=True, generation=gen, segment=str(self.segment_dir / f"{gen}.jsonl.gz"))
            return result

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
