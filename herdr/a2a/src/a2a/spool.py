"""持久消息队列(spool)。

布局:
  <spool>/pending/<目标 agent_id>/<msg_id>.json   未到终态的消息(Router 入队,broker 消费)
  <spool>/done/<msg_id>.json                       已到终态的消息(到达终态时自动归档)
  <spool>/queues/<目标 agent_id>.json              目标队列状态:下一个 queue_seq、尚未放行的终态槽位

保证:
  * 入队是"先写同目录临时文件再原子替换",崩溃后不会留下半截消息。
  * **同一目标的顺序由 queue_seq 决定**(08-protocol.md §4 规则 10、§7.4):每目标持久单调,
    只由 Spool 在锁内分配;可以有空洞(崩溃),但不会复用、不会倒退。msg_id 只是唯一标识。
  * **队列头**(08 §7.1):每个目标只处理 queue_seq 最小、尚未放行的槽位。
    **消息到终态归档不等于放行**:到终态时先把槽位记为"未放行",再归档;只有 release() 才放行。
  * 状态修改只能沿 messages.ALLOWED_TRANSITIONS 迁移,终态之后不可再修改。
  * 所有 ID 都经过严格格式校验,不会被用来穿越目录。
  * 读不懂的队列状态文件一律报错(fail-closed,08 §7.2),不会当作空队列继续投递。
  * 终态归档分三步(记录未放行槽位 → 写 done/ → 删 pending/)。任意两步之间崩溃:
    pending() 会过滤已有 done 记录的消息;队列头计算同时看 pending 与未放行槽位;
    recover() 负责清理(broker 启动时调用一次)。

移植自 herdr/a2a_codex 的做法:崩溃恢复、ID 严格校验、状态名校验。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._fsutil import atomic_write_text, exclusive_lock
from .messages import (
    AGENT_ID_RE,
    ALL_STATES,
    DELIVERED,
    MSG_ID_RE,
    QUEUED,
    TERMINAL_STATES,
    Message,
    can_transition,
    now_iso,
)
from .paths import default_spool_dir


class SpoolError(RuntimeError):
    pass


class MessageNotFoundError(SpoolError):
    pass


class InvalidIdError(SpoolError):
    """msg_id 或目标 agent_id 的格式不合法。"""


class IllegalTransitionError(SpoolError):
    """状态迁移不在协议允许的范围内,或试图修改已到终态的消息。"""


class QueueStateError(SpoolError):
    """目标队列状态文件损坏或自相矛盾。按 08 §7.2 fail-closed:该目标停止投递,交操作员处理。"""


class SlotError(SpoolError):
    """槽位操作不合法:放行的不是队列头、对已放行槽位重试、同一槽位已有未终结消息等。"""


def _check_msg_id(msg_id: object) -> str:
    if not isinstance(msg_id, str) or not MSG_ID_RE.fullmatch(msg_id):
        raise InvalidIdError(f"msg_id 格式非法: {str(msg_id)[:40]!r}")
    return msg_id


def _check_agent_id(agent_id: object) -> str:
    if not isinstance(agent_id, str) or not AGENT_ID_RE.fullmatch(agent_id):
        raise InvalidIdError(f"目标 agent_id 格式非法: {str(agent_id)[:40]!r}")
    return agent_id


def _seq_key(message: Message) -> "tuple[int, str]":
    # 没有 queue_seq 的旧消息排在最前(按 msg_id),保证升级前留下的消息不会被跳过
    return (message.queue_seq if message.queue_seq is not None else 0, message.msg_id)


@dataclass(frozen=True)
class QueueHead:
    """一个目标的队列头(08 §7.1)。

    * active 不为空:该槽位有未终结的消息,broker 处理它(含 DELIVERY_UNCERTAIN,那时应暂停等裁定)。
    * active 为空:该槽位的消息都已到终态,last_terminal 是最后一条;broker 按它的状态决定
      自动放行(DELIVERED)还是暂停等操作员(确定失败)。
    """

    dst: str
    queue_seq: int
    active: Optional[Message]
    last_terminal: Optional[Message]

    @property
    def awaiting_release(self) -> bool:
        return self.active is None


class Spool:
    def __init__(self, root: "Optional[str | Path]" = None) -> None:
        raw = Path(root) if root is not None else default_spool_dir()
        raw = raw.expanduser()
        if not raw.is_absolute():
            raise SpoolError(f"spool 目录必须是绝对路径: {raw}")
        self.root = raw
        self.pending_dir = raw / "pending"
        self.done_dir = raw / "done"
        self.queues_dir = raw / "queues"
        self.lock_path = raw / ".lock"

    # ---- 路径 --------------------------------------------------------
    def _pending_path(self, dst: str, msg_id: str) -> Path:
        return self.pending_dir / _check_agent_id(dst) / f"{_check_msg_id(msg_id)}.json"

    def _done_path(self, msg_id: str) -> Path:
        return self.done_dir / f"{_check_msg_id(msg_id)}.json"

    def _queue_path(self, dst: str) -> Path:
        return self.queues_dir / f"{_check_agent_id(dst)}.json"

    @staticmethod
    def _dump(message: Message) -> str:
        return json.dumps(message.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    @staticmethod
    def _load(path: Path) -> Message:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("消息 JSON 根节点必须是 object")
            return Message.from_dict(data)
        except (OSError, ValueError, TypeError) as exc:
            raise SpoolError(f"无法读取消息文件 {path}: {exc}") from exc

    # ---- 目标队列状态(调用方必须已持有锁) -------------------------------
    def _read_queue(self, dst: str) -> Optional[Dict[str, Any]]:
        path = self._queue_path(dst)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            next_seq = data["next_seq"]
            unreleased = data["unreleased"]
            if (not isinstance(next_seq, int) or isinstance(next_seq, bool) or next_seq < 1
                    or not isinstance(unreleased, dict)):
                raise ValueError("字段类型不对")
            for seq, entry in unreleased.items():
                if (not seq.isdigit() or int(seq) >= next_seq or not isinstance(entry, dict)
                        or not MSG_ID_RE.fullmatch(str(entry.get("msg_id", "")))):
                    raise ValueError(f"未放行槽位 {seq!r} 的记录不合法")
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise QueueStateError(f"目标 {dst} 的队列状态文件损坏,停止该目标的投递: {path}: {exc}") from exc
        return data

    def _write_queue(self, dst: str, data: Dict[str, Any]) -> None:
        atomic_write_text(self._queue_path(dst),
                          json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    def _queue_or_init(self, dst: str) -> Dict[str, Any]:
        """读取目标队列状态;不存在时按已有消息初始化(兼容升级前留下的消息),保证不复用序号。"""
        data = self._read_queue(dst)
        if data is not None:
            return data
        highest = 0
        for message in self._all_messages_unlocked(dst):
            if message.queue_seq is not None:
                highest = max(highest, message.queue_seq)
        return {"next_seq": highest + 1, "unreleased": {}, "last_release": None}

    def _all_messages_unlocked(self, dst: str) -> List[Message]:
        found: List[Message] = []
        target_dir = self.pending_dir / dst
        if target_dir.is_dir():
            found += [self._load(p) for p in target_dir.glob("*.json")]
        if self.done_dir.is_dir():
            found += [m for m in (self._load(p) for p in self.done_dir.glob("*.json")) if m.dst == dst]
        return found

    # ---- 写 ----------------------------------------------------------
    def enqueue(self, message: Message) -> Message:
        """写入待投递队列,并分配 queue_seq。新消息必须是 QUEUED、不得自带 queue_seq / retry_of;
        重复的 msg_id 会被拒绝。返回带 queue_seq 的消息。"""
        _check_msg_id(message.msg_id)
        dst = _check_agent_id(message.dst)
        if message.state != QUEUED:
            raise SpoolError(f"新消息必须以 {QUEUED} 状态入队,收到 {message.state!r}")
        if message.queue_seq is not None or message.retry_of is not None:
            raise SpoolError("queue_seq 只能由 Spool 分配;重试请用 enqueue_retry()")
        with exclusive_lock(self.lock_path):
            if self._locate(message.msg_id) is not None:
                raise SpoolError(f"msg_id 已存在: {message.msg_id}")
            queue = self._queue_or_init(dst)
            seq = queue["next_seq"]
            queue["next_seq"] = seq + 1
            # 先持久化序号再写消息:中途崩溃只会留下序号空洞,不会复用
            self._write_queue(dst, queue)
            stored = replace(message, queue_seq=seq)
            atomic_write_text(self._pending_path(dst, message.msg_id), self._dump(stored))
        return stored

    def enqueue_retry(self, original_msg_id: str, new_msg_id: str, *, detail: str = "") -> Message:
        """受信任的重试入队路径(08 §7.3 规则 3):为已到确定失败终态、槽位尚未放行的消息新建一条重试消息。

        * 新消息继承原消息的授权与内容(edge_id、src、dst、text、topology_revision 等),
          不重新鉴权、不重新渲染;**继承原 queue_seq**,占住同一槽位;retry_of 指向原消息。
        * new_msg_id 由调用方在裁定时确定(写进 OPERATOR_RULING),因此本操作**幂等**:
          同一 new_msg_id 已存在且是这条原消息的重试时,直接返回它,不重复创建。
        * 拒绝:原消息不是终态;原消息是 DELIVERED;槽位已放行;同一槽位已有未终结消息。
        """
        _check_msg_id(original_msg_id)
        _check_msg_id(new_msg_id)
        with exclusive_lock(self.lock_path):
            existing = self._locate(new_msg_id)
            if existing is not None:
                message = self._load(existing)
                if message.retry_of != original_msg_id:
                    raise SpoolError(f"msg_id 已存在且不是 {original_msg_id} 的重试: {new_msg_id}")
                return message
            path = self._locate(original_msg_id)
            if path is None:
                raise MessageNotFoundError(f"消息不存在: {original_msg_id}")
            original = self._load(path)
            if original.state not in TERMINAL_STATES:
                raise SlotError(f"消息 {original_msg_id} 还不是终态({original.state});"
                                "不确定态的重试应直接迁到 RETRYING,不新建消息")
            if original.state == DELIVERED:
                raise SlotError(f"消息 {original_msg_id} 已送达,不能重试")
            if original.queue_seq is None:
                raise SlotError(f"消息 {original_msg_id} 没有 queue_seq,无法确定槽位")
            dst = original.dst
            queue = self._queue_or_init(dst)
            entry = queue["unreleased"].get(str(original.queue_seq))
            if entry is None:
                raise SlotError(f"{dst} 的槽位 {original.queue_seq} 已放行(或不存在),不能再重试")
            if entry["msg_id"] != original_msg_id:
                raise SlotError(f"槽位 {original.queue_seq} 的最后一条消息是 {entry['msg_id']},"
                                f"不是 {original_msg_id};只能重试槽位里最后一次的结果")
            if any(m.queue_seq == original.queue_seq for m in self._pending_unlocked(dst)):
                raise SlotError(f"槽位 {original.queue_seq} 已有未终结的消息")
            now = now_iso()
            retry = replace(original, msg_id=new_msg_id, created_at=now, updated_at=now, state=QUEUED,
                            attempts=0, detail=detail or f"重试 {original_msg_id}", retry_of=original_msg_id,
                            ruling_id=None)
            atomic_write_text(self._pending_path(dst, new_msg_id), self._dump(retry))
            return retry

    def update(self, msg_id: str, *, state: Optional[str] = None, detail: Optional[str] = None,
               attempts: Optional[int] = None, ruling_id: Optional[str] = None) -> Message:
        """修改消息。状态只能沿合法迁移表前进;到达终态时记为未放行槽位并归档到 done/。

        * 未知状态名 → SpoolError
        * 不在迁移表里的迁移(含终态回退)→ IllegalTransitionError
        * 已到终态的消息不可再修改任何字段 → IllegalTransitionError
        * 同一状态内更新 detail / attempts 是允许的(非终态)
        """
        _check_msg_id(msg_id)
        if state is not None and state not in ALL_STATES:
            raise SpoolError(f"未知消息状态: {state!r}")
        with exclusive_lock(self.lock_path):
            path = self._locate(msg_id)
            if path is None:
                raise MessageNotFoundError(f"消息不存在: {msg_id}")
            current = self._load(path)
            if current.state in TERMINAL_STATES:
                raise IllegalTransitionError(f"消息 {msg_id} 已是终态 {current.state},不可再修改")
            if state is not None and state != current.state and not can_transition(current.state, state):
                raise IllegalTransitionError(f"非法状态迁移: {current.state} -> {state}")
            changes: Dict[str, object] = {"updated_at": now_iso()}
            if state is not None:
                changes["state"] = state
            if detail is not None:
                changes["detail"] = detail
            if attempts is not None:
                changes["attempts"] = attempts
            if ruling_id is not None:
                changes["ruling_id"] = ruling_id  # 与状态迁移在同一次原子写入里,补做裁定时据此判断"已做过"
            updated = replace(current, **changes)
            if updated.state in TERMINAL_STATES:
                # 三步归档:先把槽位记为未放行(归档不等于放行),再写 done/,最后删 pending/
                if updated.queue_seq is not None:
                    queue = self._queue_or_init(updated.dst)
                    queue["unreleased"][str(updated.queue_seq)] = {"msg_id": msg_id, "state": updated.state}
                    self._write_queue(updated.dst, queue)
                atomic_write_text(self._done_path(msg_id), self._dump(updated))
                path.unlink(missing_ok=True)
            else:
                atomic_write_text(path, self._dump(updated))
            return updated

    def release(self, dst: str, queue_seq: int, *, reason: str, ruling_id: Optional[str] = None) -> Dict[str, Any]:
        """放行队列头槽位(08 §7.1)。只有队列头、且该槽位没有未终结消息时才允许。

        幂等:该槽位已经放行(不在未放行记录里、且比当前队列头小)时返回 {"already_released": True},不报错。
        真正放行时返回放行记录,already_released 为 False。
        放行原因与 ruling_id 记在队列状态的 last_release 里;审计事件由调用方(broker)记录。
        """
        _check_agent_id(dst)
        if not isinstance(queue_seq, int) or isinstance(queue_seq, bool) or queue_seq < 1:
            raise SlotError(f"queue_seq 不合法: {queue_seq!r}")
        with exclusive_lock(self.lock_path):
            queue = self._queue_or_init(dst)
            head = self._head_unlocked(dst, queue)
            key = str(queue_seq)
            if queue_seq >= queue["next_seq"]:
                raise SlotError(f"{dst} 还没有分配过槽位 {queue_seq}")
            if key not in queue["unreleased"]:
                if head is None or queue_seq < head.queue_seq:
                    return {"queue_seq": queue_seq, "already_released": True}  # 已放行过:幂等
                raise SlotError(f"{dst} 的槽位 {queue_seq} 没有到终态的消息,不能放行")
            if head is None or head.queue_seq != queue_seq:
                raise SlotError(f"只能放行队列头;{dst} 的队列头是 {head.queue_seq if head else None},"
                                f"不是 {queue_seq}")
            if head.active is not None:
                raise SlotError(f"{dst} 的槽位 {queue_seq} 还有未终结的消息 {head.active.msg_id}")
            entry = queue["unreleased"].pop(key)
            record = {"queue_seq": queue_seq, "msg_id": entry["msg_id"], "state": entry["state"],
                      "reason": reason, "ruling_id": ruling_id, "at": now_iso()}
            queue["last_release"] = record
            self._write_queue(dst, queue)
            return dict(record, already_released=False)

    def recover(self) -> Dict[str, int]:
        """崩溃恢复,**broker 启动时调用一次**:

        * done/ 里已有终态记录的消息,删除它在 pending/ 里残留的那一份(归档的后半步没做完)
        * 未放行槽位记录指向的消息若还在 pending/(记录写了、归档没做),删掉这条过早的记录
        * 清理原子写入遗留的临时文件(*.tmp)

        在独占锁内执行,不会和入队、状态修改交错。损坏的队列状态文件**不修改**,只列在
        unreadable_queue_files 里,由 broker 对该目标 fail-closed(08 §7.2)。
        """
        removed_pending = removed_tmp = stale_slots = 0
        unreadable: List[str] = []
        with exclusive_lock(self.lock_path):
            if self.pending_dir.is_dir():
                for path in sorted(self.pending_dir.rglob("*.json")):
                    if MSG_ID_RE.fullmatch(path.stem) and self._has_valid_done(path.stem):
                        path.unlink(missing_ok=True)
                        removed_pending += 1
            if self.queues_dir.is_dir():
                for qpath in sorted(self.queues_dir.glob("*.json")):
                    if not AGENT_ID_RE.fullmatch(qpath.stem):
                        continue
                    try:
                        queue = self._read_queue(qpath.stem)
                    except QueueStateError:
                        unreadable.append(qpath.stem)  # 不在这里修:交给 broker 对该目标 fail-closed
                        continue
                    if queue is None:
                        continue
                    stale = [seq for seq, entry in queue["unreleased"].items()
                             if not self._has_valid_done(entry["msg_id"])]
                    for seq in stale:
                        del queue["unreleased"][seq]
                    if stale:
                        self._write_queue(qpath.stem, queue)
                        stale_slots += len(stale)
            for directory in (self.pending_dir, self.done_dir, self.queues_dir):
                if directory.is_dir():
                    for path in directory.rglob("*.tmp"):
                        path.unlink(missing_ok=True)
                        removed_tmp += 1
        result: Dict[str, Any] = {"removed_duplicate_pending": removed_pending, "removed_temp_files": removed_tmp,
                                  "removed_stale_slot_records": stale_slots}
        if unreadable:
            result["unreadable_queue_files"] = unreadable
        return result

    def _has_valid_done(self, msg_id: str) -> bool:
        done = self._done_path(msg_id)
        if not done.exists():
            return False
        try:
            return self._load(done).state in TERMINAL_STATES
        except SpoolError:
            return False

    # ---- 读 ----------------------------------------------------------
    def _locate(self, msg_id: str) -> Optional[Path]:
        done = self._done_path(msg_id)
        if done.exists():
            return done
        if self.pending_dir.is_dir():
            for target_dir in self.pending_dir.iterdir():
                candidate = target_dir / f"{_check_msg_id(msg_id)}.json"
                if candidate.exists():
                    return candidate
        return None

    def get(self, msg_id: str) -> Message:
        _check_msg_id(msg_id)
        path = self._locate(msg_id)
        if path is None:
            raise MessageNotFoundError(f"消息不存在: {msg_id}")
        return self._load(path)

    def _pending_unlocked(self, dst: Optional[str]) -> List[Message]:
        if not self.pending_dir.is_dir():
            return []
        dirs = [self.pending_dir / dst] if dst else sorted(self.pending_dir.iterdir())
        messages: List[Message] = []
        for target_dir in dirs:
            if not target_dir.is_dir():
                continue
            for path in target_dir.glob("*.json"):
                if self._done_path(path.stem).exists():
                    continue
                messages.append(self._load(path))
        return sorted(messages, key=lambda m: (m.dst, *_seq_key(m)))

    def pending(self, dst: Optional[str] = None) -> List[Message]:
        """未到终态的消息,同一目标内按 queue_seq 排序。dst 为空时返回所有目标的。

        已经在 done/ 里有终态记录的消息会被过滤掉:即使归档的后半步崩溃过、recover() 还没运行,
        也不会把已送达的消息再次交给 broker。
        """
        if dst is not None:
            _check_agent_id(dst)
        return self._pending_unlocked(dst)

    def pending_targets(self) -> List[str]:
        """当前有待投递消息的目标 agent_id。"""
        return sorted({m.dst for m in self.pending()})

    def queue_targets(self) -> List[str]:
        """有待投递消息、有未放行槽位、或队列状态文件读不出的目标(broker 要处理的全部目标)。"""
        targets = set(self.pending_targets())
        if self.queues_dir.is_dir():
            with exclusive_lock(self.lock_path):
                for qpath in self.queues_dir.glob("*.json"):
                    if AGENT_ID_RE.fullmatch(qpath.stem):
                        try:
                            queue = self._read_queue(qpath.stem)
                        except QueueStateError:
                            targets.add(qpath.stem)  # 状态读不出也要列出来,让 broker 对它 fail-closed
                            continue
                        if queue and queue["unreleased"]:
                            targets.add(qpath.stem)
        return sorted(targets)

    def _head_unlocked(self, dst: str, queue: Dict[str, Any]) -> Optional[QueueHead]:
        pending = self._pending_unlocked(dst)
        seqs = {_seq_key(m)[0] for m in pending} | {int(s) for s in queue["unreleased"]}
        if not seqs:
            return None
        head_seq = min(seqs)
        active = next((m for m in pending if _seq_key(m)[0] == head_seq), None)
        last_terminal = None
        entry = queue["unreleased"].get(str(head_seq))
        if entry is not None and self._done_path(entry["msg_id"]).exists():
            last_terminal = self._load(self._done_path(entry["msg_id"]))
        if active is None and last_terminal is None:
            # 未放行记录指向的消息既不在 pending/ 也不在 done/:状态自相矛盾,不能猜(fail-closed)
            raise QueueStateError(f"{dst} 的槽位 {head_seq} 记录的消息 {entry and entry['msg_id']} 找不到")
        return QueueHead(dst=dst, queue_seq=head_seq, active=active, last_terminal=last_terminal)

    def head(self, dst: str) -> Optional[QueueHead]:
        """目标的队列头(08 §7.1);队列为空且没有未放行槽位时返回 None。"""
        _check_agent_id(dst)
        with exclusive_lock(self.lock_path):
            return self._head_unlocked(dst, self._queue_or_init(dst))

    def done(self) -> List[Message]:
        if not self.done_dir.is_dir():
            return []
        return sorted((self._load(p) for p in self.done_dir.glob("*.json")), key=lambda m: m.msg_id)
