"""spool 终态记录的归档(路线图 189,19 号方案 E 节 2)。

`a2a spool archive --older-than <天数>`:把到终态超过 N 天的 done/<msg_id>.json 打包进
spool/archive/<年-月>-<分卷>.tar.gz(按到终态的月份;每次新建分卷,不改旧包),逐个核对包内文件与原文件
一致后才删除原文件。不做无策略删除:不归档的
  * 槽位仍未放行的(包括某个队列头的 last_terminal):broker 还要按它决定放行;
  * 被未作废裁定引用的(OPERATOR_RULING 的 msg_id 或 retry_msg_id):补做、核对可能还要读它;
  * 保留天数小于幂等键有效期的整次请求被拒绝:幂等补齐要能可靠判断 msg_id 是否已入队。
中途崩溃(包已发布、原文件未删)后重跑:原文件已在某个旧包里且内容一致的,只删除原文件,不再打包,所以不丢不重。
"""
from __future__ import annotations

import io
import os
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from ._fsutil import exclusive_lock
from .router import IDEMPOTENCY_TTL_S
from .spool import Spool

MIN_OLDER_THAN_DAYS = -(-IDEMPOTENCY_TTL_S // 86400)     # 向上取整:7


class ArchiveError(RuntimeError):
    pass


def _parse_time(text: str) -> Optional[datetime]:
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _fsync_dir(directory: Path) -> None:
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def archive_dir(spool: Spool) -> Path:
    return spool.root / "archive"


def archived_members(spool: Spool) -> Dict[str, bytes]:
    """所有分卷里的 {文件名: 内容}。读不出的分卷报错(不当作空)。"""
    found: Dict[str, bytes] = {}
    directory = archive_dir(spool)
    if not directory.is_dir():
        return found
    for volume in sorted(directory.glob("*.tar.gz")):
        try:
            with tarfile.open(volume, "r:gz") as tar:
                for member in tar.getmembers():
                    if member.isfile():
                        found[member.name] = tar.extractfile(member).read()
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise ArchiveError(f"归档分卷 {volume} 读不出:{exc}") from exc
    return found


def find_archived(spool: Spool, msg_id: str) -> Optional[bytes]:
    return archived_members(spool).get(f"{msg_id}.json")


def _keep_reasons(spool: Spool, referenced: Set[str]) -> Dict[str, str]:
    """不归档的 msg_id 及原因。"""
    keep: Dict[str, str] = {}
    if spool.queues_dir.is_dir():
        for path in spool.queues_dir.glob("*.json"):
            queue = spool._read_queue(path.stem)            # 损坏时抛错:不在读不懂的状态上归档
            for entry in (queue or {}).get("unreleased", {}).values():
                keep[entry["msg_id"]] = "槽位未放行(含队列头的 last_terminal)"
    for msg_id in referenced:
        keep.setdefault(msg_id, "被未作废的裁定引用")
    return keep


def archive(spool: Spool, older_than_days: int, *, referenced: Iterable[str] = (), dry_run: bool = False,
            now: Optional[datetime] = None, crash_hook=None) -> Dict[str, Any]:
    if older_than_days < MIN_OLDER_THAN_DAYS:
        raise ArchiveError(f"--older-than 不能小于幂等键有效期 {MIN_OLDER_THAN_DAYS} 天(幂等补齐要能判断消息是否已入队)")
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=older_than_days)
    directory = archive_dir(spool)
    with exclusive_lock(spool.lock_path):
        keep = _keep_reasons(spool, set(referenced))
        candidates: Dict[str, List[Path]] = {}       # 月份 -> done 文件
        skipped: Dict[str, str] = {}
        if spool.done_dir.is_dir():
            for path in sorted(spool.done_dir.glob("*.json")):
                message = spool._load(path)
                finished = _parse_time(message.updated_at) or _parse_time(message.created_at)
                if finished is None or finished > cutoff:
                    continue
                if message.msg_id in keep:
                    skipped[message.msg_id] = keep[message.msg_id]
                    continue
                if spool.pending_dir.is_dir() and any(spool.pending_dir.glob(f"*/{path.name}")):
                    # 到终态归档时中途崩溃:pending 里还留着一份,等 broker 启动恢复清理后再归档
                    skipped[message.msg_id] = "pending 里还有同一条消息(终态归档中断,待 broker 恢复清理)"
                    continue
                candidates.setdefault(finished.strftime("%Y-%m"), []).append(path)
        result: Dict[str, Any] = {"older_than_days": older_than_days, "dry_run": dry_run, "skipped": skipped,
                                  "archived": 0, "already_archived": 0, "volumes": []}
        total = sum(len(v) for v in candidates.values())
        if dry_run or total == 0:
            result["would_archive" if dry_run else "archived"] = total
            return result
        directory.mkdir(parents=True, exist_ok=True)
        existing = archived_members(spool)
        for month, paths in sorted(candidates.items()):
            fresh = []
            for path in paths:
                old = existing.get(path.name)
                if old is not None:
                    if old != path.read_bytes():
                        raise ArchiveError(f"{path.name} 已在旧分卷里但内容不同,停止(不删除原文件)")
                    path.unlink()                    # 上次打包后、删除前中断:只补删
                    result["already_archived"] += 1
                else:
                    fresh.append(path)
            if not fresh:
                continue
            n = 1
            while (directory / f"{month}-{n}.tar.gz").exists():
                n += 1
            final = directory / f"{month}-{n}.tar.gz"
            temp = directory / f"{month}-{n}.tar.gz.tmp"
            originals = {p.name: p.read_bytes() for p in fresh}
            with open(temp, "wb") as raw:
                with tarfile.open(fileobj=raw, mode="w:gz") as tar:
                    for name, data in originals.items():
                        info = tarfile.TarInfo(name)
                        info.size = len(data)
                        info.mtime = int(now.timestamp())
                        tar.addfile(info, io.BytesIO(data))
                raw.flush()
                os.fsync(raw.fileno())
            with tarfile.open(temp, "r:gz") as tar:   # 核对:每个文件都在包里且逐字节一致
                packed = {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}
            if packed != originals:
                temp.unlink()
                raise ArchiveError(f"打包核对不一致:{final.name},原文件未删除")
            os.replace(temp, final)
            _fsync_dir(directory)
            if crash_hook:
                crash_hook("after_publish")
            for path in fresh:
                path.unlink()
            _fsync_dir(spool.done_dir)
            result["archived"] += len(fresh)
            result["volumes"].append(final.name)
        for temp in directory.glob("*.tmp"):
            temp.unlink()
        return result


def archive_guarded(state_dir: Path, spool: Spool, audit, older_than_days: int, *, dry_run: bool = False,
                    now: Optional[datetime] = None, crash_hook=None) -> Dict[str, Any]:
    """CLI 用:在裁定锁内完成"检查读不出的裁定 → 算引用集合 → 归档",与 resolve / ruling void / broker 补做串行。
    否则先算引用、后拿 spool 锁之间,新裁定可能放行一个仍需保留的队列头,它会被移出 done/。
    锁顺序与 resolve 相同:裁定锁在外,spool 锁在内。"""
    from . import rulings
    with rulings.lock(state_dir):
        _, corrupt = rulings.unapplied(audit)
        if corrupt:
            raise ArchiveError(f"审计里有 {len(corrupt)} 条读不出的裁定记录,无法判断哪些消息仍被引用,不归档;"
                               f"先用 a2a ruling void corrupt:<指纹> 处理:{[c['fingerprint'] for c in corrupt]}")
        return archive(spool, older_than_days, referenced=referenced_by_rulings(audit.read()), dry_run=dry_run,
                       now=now, crash_hook=crash_hook)


def referenced_by_rulings(entries: Iterable[Dict[str, Any]]) -> Set[str]:
    """未作废裁定引用的 msg_id(含 retry_msg_id)。"""
    from .rulings import OPERATOR_RULING, RULING_VOIDED
    entries = list(entries)
    voided = {e.get("voided_ruling_id") for e in entries if e.get("state") == RULING_VOIDED}
    refs: Set[str] = set()
    for e in entries:
        if e.get("state") == OPERATOR_RULING and e.get("ruling_id") not in voided:
            refs.update(v for v in (e.get("msg_id"), e.get("retry_msg_id")) if v)
    return refs
