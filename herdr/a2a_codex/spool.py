"""原子持久化的消息队列，供 Router 入队和后续 Broker 消费。"""
from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import List, Optional

from .messages import ALL_STATES, QUEUED, TERMINAL_STATES, Message, now_iso
from .paths import absolute_path, default_state_dir
from .storage import atomic_write_text, exclusive_lock

_SAFE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_MSG_ID = re.compile(r"^[0-9a-f]{16}-[0-9a-f]{6}$")


class SpoolError(RuntimeError):
    pass


class MessageNotFoundError(SpoolError):
    pass


class Spool:
    def __init__(self, root: Optional[str | Path] = None) -> None:
        try:
            path = absolute_path(root, "spool 路径") if root is not None else default_state_dir() / "spool"
        except ValueError as exc:
            raise SpoolError(str(exc)) from exc
        self.root = path
        self.pending_dir = path / "pending"
        self.done_dir = path / "done"
        self.lock_path = path / ".lock"
        with exclusive_lock(self.lock_path):
            self._recover_unlocked()

    def _recover_unlocked(self) -> None:
        """清理崩溃遗留临时文件，并以合法终态归档消除 pending/done 双份。"""
        valid_done_ids = set()
        if self.done_dir.is_dir():
            for path in self.done_dir.glob("*.json"):
                try:
                    message = self._load(path)
                except SpoolError:
                    continue
                if message.state in TERMINAL_STATES:
                    valid_done_ids.add(message.msg_id)

        if self.pending_dir.is_dir():
            for path in self.pending_dir.rglob("*.json"):
                if path.stem in valid_done_ids:
                    path.unlink(missing_ok=True)
            for path in self.pending_dir.rglob("*.tmp"):
                path.unlink(missing_ok=True)
        if self.done_dir.is_dir():
            for path in self.done_dir.rglob("*.tmp"):
                path.unlink(missing_ok=True)

    def _pending_path(self, dst: str, msg_id: str) -> Path:
        self._validate_ids(dst, msg_id)
        return self.pending_dir / dst / (msg_id + ".json")

    def _done_path(self, msg_id: str) -> Path:
        self._validate_ids("agent", msg_id)
        return self.done_dir / (msg_id + ".json")

    @staticmethod
    def _validate_ids(dst: str, msg_id: str) -> None:
        if not isinstance(dst, str) or not _SAFE_ID.fullmatch(dst):
            raise SpoolError("目标 agent_id 格式非法")
        if not isinstance(msg_id, str) or not _MSG_ID.fullmatch(msg_id):
            raise SpoolError("msg_id 格式非法")

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

    def enqueue(self, message: Message) -> Message:
        self._validate_ids(message.dst, message.msg_id)
        if message.state != QUEUED:
            raise SpoolError(f"新消息必须以 {QUEUED} 状态入队")
        with exclusive_lock(self.lock_path):
            if self._locate(message.msg_id) is not None:
                raise SpoolError(f"msg_id 已存在: {message.msg_id}")
            atomic_write_text(self._pending_path(message.dst, message.msg_id), self._dump(message))
        return message

    def update(self, msg_id: str, *, state: Optional[str] = None, detail: Optional[str] = None,
               attempts: Optional[int] = None) -> Message:
        self._validate_ids("agent", msg_id)
        with exclusive_lock(self.lock_path):
            path = self._locate(msg_id)
            if path is None:
                raise MessageNotFoundError(f"消息不存在: {msg_id}")
            current = self._load(path)
            changes = {"updated_at": now_iso()}
            if state is not None:
                changes["state"] = state
            if detail is not None:
                changes["detail"] = detail
            if attempts is not None:
                changes["attempts"] = attempts
            if changes.get("state", current.state) not in ALL_STATES:
                raise SpoolError(f"未知消息状态: {changes['state']}")
            updated = replace(current, **changes)
            if updated.state in TERMINAL_STATES:
                atomic_write_text(self._done_path(msg_id), self._dump(updated))
                path.unlink(missing_ok=True)
            else:
                atomic_write_text(path, self._dump(updated))
            return updated

    def _locate(self, msg_id: str) -> Optional[Path]:
        self._validate_ids("agent", msg_id)
        done = self._done_path(msg_id)
        if done.exists():
            return done
        if self.pending_dir.is_dir():
            for target in self.pending_dir.iterdir():
                candidate = target / (msg_id + ".json")
                if candidate.exists():
                    return candidate
        return None

    def get(self, msg_id: str) -> Message:
        with exclusive_lock(self.lock_path):
            path = self._locate(msg_id)
            if path is None:
                raise MessageNotFoundError(f"消息不存在: {msg_id}")
            return self._load(path)

    def pending(self, dst: Optional[str] = None) -> List[Message]:
        if dst is not None and (not isinstance(dst, str) or not _SAFE_ID.fullmatch(dst)):
            raise SpoolError("目标 agent_id 格式非法")
        with exclusive_lock(self.lock_path):
            if not self.pending_dir.is_dir():
                return []
            targets = [self.pending_dir / dst] if dst else sorted(self.pending_dir.iterdir())
            messages = [self._load(path) for directory in targets if directory.is_dir()
                        for path in directory.glob("*.json")]
            return sorted(messages, key=lambda item: item.msg_id)

    def pending_targets(self) -> List[str]:
        with exclusive_lock(self.lock_path):
            if not self.pending_dir.is_dir():
                return []
            return sorted(directory.name for directory in self.pending_dir.iterdir()
                          if directory.is_dir() and any(directory.glob("*.json")))

    def done(self) -> List[Message]:
        with exclusive_lock(self.lock_path):
            if not self.done_dir.is_dir():
                return []
            return sorted((self._load(path) for path in self.done_dir.glob("*.json")),
                          key=lambda item: item.msg_id)
