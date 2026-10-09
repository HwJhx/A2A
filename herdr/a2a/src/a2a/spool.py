"""持久消息队列(spool)。

布局:
  <spool>/pending/<目标 agent_id>/<msg_id>.json   未到终态的消息(Router 入队,broker 消费)
  <spool>/done/<msg_id>.json                       已到终态的消息(到达终态时自动归档)

保证:
  * 入队是"先写同目录临时文件再原子替换",崩溃后不会留下半截消息。
  * msg_id 按时间排序,同一目标的待投递消息按 msg_id 排序即为入队顺序(FIFO)。
  * 状态修改只能沿 messages.ALLOWED_TRANSITIONS 迁移,终态之后不可再修改(协议见 08-protocol.md)。
  * 所有 ID 都经过严格格式校验,不会被用来穿越目录。
  * 终态归档分两步(先写 done/,再删 pending/)。两步之间崩溃会留下两份:
    pending() 在读取时就会把已有 done 记录的消息过滤掉,所以不会重复投递;
    recover() 负责真正清理(broker 启动时调用一次)。

移植自 herdr/a2a_codex 的做法:崩溃恢复、ID 严格校验、状态名校验。
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional

from ._fsutil import atomic_write_text, exclusive_lock
from .messages import (
    AGENT_ID_RE,
    ALL_STATES,
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


def _check_msg_id(msg_id: object) -> str:
    if not isinstance(msg_id, str) or not MSG_ID_RE.fullmatch(msg_id):
        raise InvalidIdError(f"msg_id 格式非法: {str(msg_id)[:40]!r}")
    return msg_id


def _check_agent_id(agent_id: object) -> str:
    if not isinstance(agent_id, str) or not AGENT_ID_RE.fullmatch(agent_id):
        raise InvalidIdError(f"目标 agent_id 格式非法: {str(agent_id)[:40]!r}")
    return agent_id


class Spool:
    def __init__(self, root: "Optional[str | Path]" = None) -> None:
        raw = Path(root) if root is not None else default_spool_dir()
        raw = raw.expanduser()
        if not raw.is_absolute():
            raise SpoolError(f"spool 目录必须是绝对路径: {raw}")
        self.root = raw
        self.pending_dir = raw / "pending"
        self.done_dir = raw / "done"
        self.lock_path = raw / ".lock"

    # ---- 路径 --------------------------------------------------------
    def _pending_path(self, dst: str, msg_id: str) -> Path:
        return self.pending_dir / _check_agent_id(dst) / f"{_check_msg_id(msg_id)}.json"

    def _done_path(self, msg_id: str) -> Path:
        return self.done_dir / f"{_check_msg_id(msg_id)}.json"

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

    # ---- 写 ----------------------------------------------------------
    def enqueue(self, message: Message) -> Message:
        """写入待投递队列。新消息必须是 QUEUED 状态;重复的 msg_id 会被拒绝。"""
        _check_msg_id(message.msg_id)
        _check_agent_id(message.dst)
        if message.state != QUEUED:
            raise SpoolError(f"新消息必须以 {QUEUED} 状态入队,收到 {message.state!r}")
        with exclusive_lock(self.lock_path):
            if self._locate(message.msg_id) is not None:
                raise SpoolError(f"msg_id 已存在: {message.msg_id}")
            atomic_write_text(self._pending_path(message.dst, message.msg_id), self._dump(message))
        return message

    def update(self, msg_id: str, *, state: Optional[str] = None, detail: Optional[str] = None,
               attempts: Optional[int] = None) -> Message:
        """修改消息。状态只能沿合法迁移表前进;到达终态时自动归档到 done/。

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
            updated = replace(current, **changes)
            if updated.state in TERMINAL_STATES:
                # 两步归档:先写 done/,再删 pending/。两步之间崩溃见 recover()
                atomic_write_text(self._done_path(msg_id), self._dump(updated))
                path.unlink(missing_ok=True)
            else:
                atomic_write_text(path, self._dump(updated))
            return updated

    def recover(self) -> Dict[str, int]:
        """崩溃恢复,**broker 启动时调用一次**:

        * done/ 里已有终态记录的消息,删除它在 pending/ 里残留的那一份(归档的后半步没做完)
        * 清理原子写入遗留的临时文件(*.tmp)

        在独占锁内执行,不会和入队、状态修改交错。返回清理数量。
        """
        removed_pending = removed_tmp = 0
        with exclusive_lock(self.lock_path):
            if self.pending_dir.is_dir():
                for path in sorted(self.pending_dir.rglob("*.json")):
                    if MSG_ID_RE.fullmatch(path.stem) and self._has_valid_done(path.stem):
                        path.unlink(missing_ok=True)
                        removed_pending += 1
            for directory in (self.pending_dir, self.done_dir):
                if directory.is_dir():
                    for path in directory.rglob("*.tmp"):
                        path.unlink(missing_ok=True)
                        removed_tmp += 1
        return {"removed_duplicate_pending": removed_pending, "removed_temp_files": removed_tmp}

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

    def pending(self, dst: Optional[str] = None) -> List[Message]:
        """未到终态的消息,按入队顺序(msg_id)排序。dst 为空时返回所有目标的。

        已经在 done/ 里有终态记录的消息会被过滤掉:即使归档的后半步崩溃过、recover() 还没运行,
        也不会把已送达的消息再次交给 broker。
        """
        if dst is not None:
            _check_agent_id(dst)
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
        return sorted(messages, key=lambda m: m.msg_id)

    def pending_targets(self) -> List[str]:
        """当前有待投递消息的目标 agent_id。"""
        return sorted({m.dst for m in self.pending()})

    def done(self) -> List[Message]:
        if not self.done_dir.is_dir():
            return []
        return sorted((self._load(p) for p in self.done_dir.glob("*.json")), key=lambda m: m.msg_id)
