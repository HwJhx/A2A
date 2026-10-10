"""原子持久化的消息队列，供 Router 入队和后续 Broker 消费。"""
from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import replace
from pathlib import Path
from typing import List, Optional

from .messages import (
    ALL_STATES,
    DELIVERED,
    QUEUED,
    REJECTED,
    TERMINAL_STATES,
    Message,
    can_transition,
    now_iso,
)
from .paths import absolute_path, default_state_dir
from .storage import atomic_write_text, exclusive_lock

_SAFE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_MSG_ID = re.compile(r"^[0-9a-f]{16}-[0-9a-f]{6}$")


class SpoolError(RuntimeError):
    pass


class SpoolCorruptionError(SpoolError):
    """Spool 持久化文件损坏；path 指向可供隔离和人工恢复的原文件。"""

    def __init__(self, path: Path, detail: str) -> None:
        self.path = path
        super().__init__(f"无法读取 Spool 文件 {path}: {detail}")


class MessageNotFoundError(SpoolError):
    pass


class QueueSequenceGapError(SpoolError):
    """已分配且未放行的 queue_seq 缺少消息记录，必须 fail-closed。"""


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
        self.queue_meta_path = path / "queue-meta.json"
        self.dispatch_control_path = path / "dispatch-control.json"
        with exclusive_lock(self.lock_path):
            try:
                self._recover_unlocked(check_gaps=False)
            except SpoolCorruptionError as exc:
                self._quarantine_unlocked(exc.path, str(exc))
                self._halt_unlocked("Spool 数据损坏，等待操作员恢复")
                raise

    def recover(self) -> None:
        """显式执行启动恢复；Broker 必须在调度任何目标之前调用。"""
        with exclusive_lock(self.lock_path):
            try:
                self._recover_unlocked(check_gaps=True)
            except SpoolCorruptionError as exc:
                self._quarantine_unlocked(exc.path, str(exc))
                self._halt_unlocked("Spool 数据损坏，等待操作员恢复")
                raise
            except QueueSequenceGapError as exc:
                self._halt_unlocked(str(exc))
                raise

    def dispatch_control(self) -> dict:
        with exclusive_lock(self.lock_path):
            try:
                return dict(self._load_dispatch_control_unlocked())
            except SpoolCorruptionError as exc:
                self._quarantine_unlocked(exc.path, str(exc))
                return dict(self._halt_unlocked("dispatch-control 损坏，等待操作员恢复"))

    def halt_dispatch(self, reason: str, *, incident_id: Optional[str] = None) -> dict:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("全局停止原因必须为非空字符串")
        with exclusive_lock(self.lock_path):
            try:
                current = self._load_dispatch_control_unlocked()
            except SpoolCorruptionError as exc:
                self._quarantine_unlocked(exc.path, str(exc))
                current = {"halted": False}
            if current["halted"]:
                return dict(current)
            value = {"version": 1, "halted": True,
                     "incident_id": incident_id or ("halt-" + secrets.token_hex(12)),
                     "reason": reason.strip(), "updated_at": now_iso()}
            self._save_dispatch_control_unlocked(value)
            return dict(value)

    def clear_dispatch_halt(self, incident_id: str) -> bool:
        if not isinstance(incident_id, str) or not incident_id:
            raise ValueError("incident_id 必须为非空字符串")
        with exclusive_lock(self.lock_path):
            current = self._load_dispatch_control_unlocked()
            if not current["halted"]:
                return False
            if current["incident_id"] != incident_id:
                raise SpoolError("DISPATCH_RESUMED incident_id 与当前停止事件不匹配")
            self._save_dispatch_control_unlocked({"version": 1, "halted": False,
                                                  "incident_id": None, "reason": "",
                                                  "updated_at": now_iso()})
            return True

    def _recover_unlocked(self, *, check_gaps: bool = False) -> None:
        """清理崩溃遗留临时文件，并以合法终态归档消除 pending/done 双份。"""
        queue_meta_was_missing = not self.queue_meta_path.exists()
        valid_done_ids = set()
        messages: list[Message] = []
        if self.done_dir.is_dir():
            for path in self.done_dir.glob("*.json"):
                # 终态记录决定是否仍有未放行槽位；损坏时不能静默忽略并误放后续消息。
                message = self._load_checked(path)
                if message.state in TERMINAL_STATES:
                    valid_done_ids.add(message.msg_id)
                    messages.append(message)
                    if message.state == DELIVERED:
                        self._release_slot_unlocked(message.dst, message.queue_seq,
                                                    reason="DELIVERED")

        if self.pending_dir.is_dir():
            for path in self.pending_dir.rglob("*.json"):
                if path.stem in valid_done_ids:
                    path.unlink(missing_ok=True)
                    continue
                messages.append(self._load_checked(path))
            for path in self.pending_dir.rglob("*.tmp"):
                path.unlink(missing_ok=True)
        if self.done_dir.is_dir():
            for path in self.done_dir.rglob("*.tmp"):
                path.unlink(missing_ok=True)

        meta = self._load_queue_meta_unlocked()
        if queue_meta_was_missing and messages:
            raise SpoolCorruptionError(
                self.queue_meta_path,
                "已有消息但 queue metadata 丢失，无法证明此前哪些槽位已放行"
            )
        minimum_next: dict[str, int] = {}
        for message in messages:
            minimum_next[message.dst] = max(minimum_next.get(message.dst, 1), message.queue_seq + 1)
        for dst, released in meta["released"].items():
            if released:
                minimum_next[dst] = max(minimum_next.get(dst, 1), max(map(int, released)) + 1)
        old_next = dict(meta["next_seq"])
        for dst, minimum in minimum_next.items():
            current_next = int(meta["next_seq"].get(dst, 1))
            if current_next < minimum:
                # 新入队顺序先写消息、再推进 next_seq；若两步之间崩溃，
                # 只会落后一格，可依据已持久化消息安全向前恢复且不复用序号。
                if minimum != current_next + 1:
                    raise SpoolCorruptionError(
                        self.queue_meta_path,
                        f"{dst} next_seq={current_next} 与已持久化 queue_seq={minimum - 1} 不连续"
                    )
                meta["next_seq"][dst] = minimum
        if meta["next_seq"] != old_next:
            self._save_queue_meta_unlocked(meta)

        if check_gaps:
            all_by_dst: dict[str, set[int]] = {}
            for message in messages:
                all_by_dst.setdefault(message.dst, set()).add(message.queue_seq)
            for dst in set(meta["next_seq"]) | set(meta["released"]) | set(all_by_dst):
                self._assert_sequence_integrity_unlocked(dst, all_by_dst.get(dst, set()), meta)

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
            message = Message.from_dict(data)
            if message.state not in ALL_STATES or message.state == REJECTED:
                raise ValueError(f"Spool 中的消息状态非法: {message.state!r}")
            if (not isinstance(message.queue_seq, int) or isinstance(message.queue_seq, bool)
                    or message.queue_seq < 1):
                raise ValueError("消息缺少有效的 queue_seq；需人工迁移旧队列，不能猜测 FIFO 顺序")
            if message.retry_of is not None and not _MSG_ID.fullmatch(message.retry_of):
                raise ValueError("retry_of 格式非法")
            if message.ruling_id is not None and not isinstance(message.ruling_id, str):
                raise ValueError("ruling_id 必须为字符串或 null")
            if message.transition_id is not None and not isinstance(message.transition_id, str):
                raise ValueError("transition_id 必须为字符串或 null")
            return message
        except (OSError, ValueError, TypeError) as exc:
            raise SpoolCorruptionError(path, str(exc)) from exc

    def quarantine_incidents(self, *, unresolved_only: bool = False) -> list[dict]:
        """列出隔离记录；不修改消息状态，也不解除全局停止。"""
        root = self.root / "corrupt"
        if not root.is_dir():
            return []
        incidents = []
        with exclusive_lock(self.lock_path):
            for manifest_path in sorted(root.glob("*/manifest.json")):
                try:
                    incident = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if not isinstance(incident, dict) or not isinstance(incident.get("incident_id"), str):
                        raise ValueError("manifest schema 错误")
                except (OSError, ValueError, TypeError) as exc:
                    raise SpoolError(f"无法读取隔离记录 {manifest_path}: {exc}") from exc
                if not unresolved_only or incident.get("resolved") is not True:
                    incidents.append(incident)
        return incidents

    def verify_quarantine_restored(self, incident_id: str) -> dict:
        """确认操作员已在原位置恢复有效数据；只验证，不记账或解除暂停。"""
        incident = self._quarantine_manifest(incident_id)
        if incident.get("resolved") is True:
            return incident
        relative_original = Path(incident.get("original_path", ""))
        if relative_original.is_absolute() or ".." in relative_original.parts:
            raise SpoolError("隔离记录中的原始路径非法")
        original = self.root / relative_original
        if not original.is_file():
            raise SpoolError(f"损坏文件尚未恢复到原位置: {original}")
        kind = incident.get("kind")
        with exclusive_lock(self.lock_path):
            if kind == "message":
                self._load_checked(original)
            elif kind == "queue_meta":
                self._load_queue_meta_unlocked()
            elif kind == "dispatch_control":
                self._load_dispatch_control_unlocked()
            else:
                raise SpoolError(f"不支持的隔离文件类型: {kind!r}")
            self._recover_unlocked(check_gaps=True)
        return incident

    def mark_quarantine_resolved(self, incident_id: str) -> dict:
        """在外层已写入操作员审计后，持久标记隔离项已核验恢复。"""
        incident = self.verify_quarantine_restored(incident_id)
        if incident.get("resolved") is True:
            return incident
        incident["resolved"] = True
        incident["resolved_at"] = now_iso()
        manifest_path = self._quarantine_manifest_path(incident_id)
        with exclusive_lock(self.lock_path):
            atomic_write_text(manifest_path, json.dumps(incident, ensure_ascii=False,
                                                         sort_keys=True, indent=2) + "\n")
        return incident

    def _load_checked(self, path: Path) -> Message:
        return self._load(path)

    def _quarantine_manifest_path(self, incident_id: str) -> Path:
        if not isinstance(incident_id, str) or not re.fullmatch(r"[a-f0-9]{24}", incident_id):
            raise SpoolError("隔离 incident_id 格式非法")
        return self.root / "corrupt" / incident_id / "manifest.json"

    def _quarantine_manifest(self, incident_id: str) -> dict:
        path = self._quarantine_manifest_path(incident_id)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("incident_id") != incident_id:
                raise ValueError("manifest schema 错误")
            return value
        except (OSError, ValueError, TypeError) as exc:
            raise SpoolError(f"无法读取隔离记录 {path}: {exc}") from exc

    def _quarantine_unlocked(self, path: Path, reason: str) -> dict:
        try:
            relative = path.resolve().relative_to(self.root.resolve())
        except ValueError as exc:
            raise SpoolError(f"拒绝隔离 Spool 根目录之外的文件: {path}") from exc
        incident_id = secrets.token_hex(12)
        directory = self.root / "corrupt" / incident_id
        stored = directory / "data" / relative
        original_exists = path.is_file()
        if original_exists:
            stored.parent.mkdir(parents=True, exist_ok=True)
        if relative.parts and relative.parts[0] in ("pending", "done") and relative.suffix == ".json":
            kind = "message"
        elif relative.name == "queue-meta.json":
            kind = "queue_meta"
        elif relative.name == "dispatch-control.json":
            kind = "dispatch_control"
        else:
            kind = "unknown"
        incident = {"version": 1, "incident_id": incident_id, "kind": kind,
                    "original_path": str(relative),
                    "quarantine_path": str(stored.relative_to(self.root)) if original_exists else None,
                    "missing_original": not original_exists,
                    "reason": reason, "resolved": False, "created_at": now_iso()}
        if kind == "message" and relative.parts[0] == "pending":
            incident["target_hint"] = relative.parts[1]
            incident["msg_id_hint"] = Path(relative.name).stem
        manifest_path = directory / "manifest.json"
        # 先落隔离索引，再移动原件；崩溃后即使停在两步之间也能定位并修复。
        atomic_write_text(manifest_path,
                          json.dumps(incident, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        if original_exists:
            os.replace(path, stored)
            incident["quarantined"] = True
            atomic_write_text(manifest_path,
                              json.dumps(incident, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        return incident

    def _halt_unlocked(self, reason: str) -> dict:
        try:
            current = self._load_dispatch_control_unlocked()
        except SpoolCorruptionError as exc:
            # 停止门闩本身损坏时先保留原始文件，再以新的 fail-closed 门闩替代。
            if exc.path.exists():
                self._quarantine_unlocked(exc.path, str(exc))
            current = {"halted": False}
        if current.get("halted"):
            return current
        value = {"version": 1, "halted": True, "incident_id": "halt-" + secrets.token_hex(12),
                 "reason": reason, "updated_at": now_iso()}
        self._save_dispatch_control_unlocked(value)
        return value

    def enqueue(self, message: Message) -> Message:
        self._validate_ids(message.dst, message.msg_id)
        if message.state != QUEUED:
            raise SpoolError(f"新消息必须以 {QUEUED} 状态入队")
        if message.queue_seq is not None or message.retry_of is not None:
            raise SpoolError("普通入队不可指定 queue_seq 或 retry_of")
        with exclusive_lock(self.lock_path):
            if self._locate(message.msg_id) is not None:
                raise SpoolError(f"msg_id 已存在: {message.msg_id}")
            meta = self._load_queue_meta_unlocked()
            all_messages = self._all_messages_unlocked()
            self._assert_sequence_integrity_unlocked(
                message.dst,
                {item.queue_seq for item in all_messages if item.dst == message.dst},
                meta,
            )
            next_seq = int(meta["next_seq"].get(message.dst, 1))
            persisted = replace(message, queue_seq=next_seq)
            # 先确保旧的 next_seq 已独立持久化，再写消息，最后推进 next_seq。
            # 因此消息写入与推进之间崩溃时，恢复最多只需安全补进一格。
            self._save_queue_meta_unlocked(meta)
            atomic_write_text(self._pending_path(persisted.dst, persisted.msg_id), self._dump(persisted))
            meta["next_seq"][message.dst] = next_seq + 1
            self._save_queue_meta_unlocked(meta)
        return persisted

    def enqueue_retry(self, message: Message, *, original_msg_id: str) -> Message:
        """受信任重试入口：继承已终结原消息的授权快照和 queue_seq。"""
        self._validate_ids(message.dst, message.msg_id)
        self._validate_ids(message.dst, original_msg_id)
        if message.state != QUEUED or message.retry_of != original_msg_id:
            raise SpoolError("重试消息必须以 QUEUED 入队且 retry_of 指向原消息")
        with exclusive_lock(self.lock_path):
            original_path = self._locate(original_msg_id)
            if original_path is None:
                raise MessageNotFoundError(f"原消息不存在: {original_msg_id}")
            original = self._load(original_path)
            if original.state not in TERMINAL_STATES or original.state == DELIVERED:
                raise SpoolError("只有未放行的确定失败终态消息可创建终态重试消息")
            inherited = ("edge_id", "src", "dst", "project_id", "ip_id", "session", "text",
                         "topology_revision", "queue_seq")
            if any(getattr(message, key) != getattr(original, key) for key in inherited):
                raise SpoolError("重试消息必须继承原消息的授权快照、文本和 queue_seq")
            existing = self._locate(message.msg_id)
            if existing is not None:
                current = self._load(existing)
                immutable = ("msg_id", "created_at", "edge_id", "src", "dst", "project_id", "ip_id",
                             "session", "text", "topology_revision", "queue_seq", "retry_of")
                if any(getattr(current, key) != getattr(message, key) for key in immutable):
                    raise SpoolError("retry_msg_id 已存在但内容不匹配")
                return current
            if self._slot_released_unlocked(original.dst, original.queue_seq):
                raise SpoolError("原消息槽位已放行，不能插回历史队列")
            for candidate in self._slot_messages_unlocked(original.dst, original.queue_seq):
                if candidate.state not in TERMINAL_STATES:
                    raise SpoolError("同一 queue_seq 已有活动消息，不能并行创建重试")
            atomic_write_text(self._pending_path(message.dst, message.msg_id), self._dump(message))
            return message

    def update(self, msg_id: str, *, state: Optional[str] = None, detail: Optional[str] = None,
               attempts: Optional[int] = None, ruling_id: Optional[str] = None,
               transition_id: Optional[str] = None) -> Message:
        self._validate_ids("agent", msg_id)
        with exclusive_lock(self.lock_path):
            path = self._locate(msg_id)
            if path is None:
                raise MessageNotFoundError(f"消息不存在: {msg_id}")
            current = self._load(path)
            if current.state in TERMINAL_STATES:
                raise SpoolError(f"终态消息不可修改: {msg_id} ({current.state})")

            requested_state = current.state if state is None else state
            if requested_state not in ALL_STATES or requested_state == REJECTED:
                raise SpoolError(f"未知或不可入队的消息状态: {requested_state}")
            if requested_state != current.state and not can_transition(current.state, requested_state):
                raise SpoolError(f"非法状态迁移: {current.state} -> {requested_state}")

            if (state is None and detail is None and attempts is None and ruling_id is None
                    and transition_id is None):
                return current
            changes = {"updated_at": now_iso()}
            if state is not None:
                changes["state"] = state
            if detail is not None:
                changes["detail"] = detail
            if attempts is not None:
                changes["attempts"] = attempts
            if ruling_id is not None:
                changes["ruling_id"] = ruling_id
            if transition_id is not None:
                changes["transition_id"] = transition_id
            updated = replace(current, **changes)
            if updated.state in TERMINAL_STATES:
                atomic_write_text(self._done_path(msg_id), self._dump(updated))
                path.unlink(missing_ok=True)
                if updated.state == DELIVERED:
                    self._release_slot_unlocked(updated.dst, updated.queue_seq, reason="DELIVERED",
                                                ruling_id=ruling_id)
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
            return sorted(messages, key=lambda item: (item.dst, item.queue_seq or 0, item.msg_id))

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
                          key=lambda item: (item.dst, item.queue_seq or 0, item.msg_id))

    def queue_targets(self) -> List[str]:
        """返回仍有未放行槽位的目标，包括消息已归档但暂停的目标。"""
        with exclusive_lock(self.lock_path):
            messages = self._all_messages_unlocked()
            meta = self._load_queue_meta_unlocked()
            by_dst: dict[str, set[int]] = {}
            for message in messages:
                by_dst.setdefault(message.dst, set()).add(message.queue_seq)
            for dst in set(meta["next_seq"]) | set(meta["released"]) | set(by_dst):
                self._assert_sequence_integrity_unlocked(dst, by_dst.get(dst, set()), meta)
            targets = {message.dst for message in messages
                       if not self._slot_released_unlocked(message.dst, message.queue_seq)}
            return sorted(targets)

    def queue_head(self, dst: str) -> Optional[Message]:
        if not isinstance(dst, str) or not _SAFE_ID.fullmatch(dst):
            raise SpoolError("目标 agent_id 格式非法")
        with exclusive_lock(self.lock_path):
            messages = self._all_messages_unlocked()
            meta = self._load_queue_meta_unlocked()
            pending = [message for message in messages if message.dst == dst]
            self._assert_sequence_integrity_unlocked(
                dst, {message.queue_seq for message in pending}, meta
            )
            by_seq: dict[int, list[Message]] = {}
            for message in pending:
                by_seq.setdefault(message.queue_seq, []).append(message)
            for queue_seq in sorted(by_seq):
                if self._slot_released_unlocked(dst, queue_seq):
                    continue
                slot_messages = by_seq[queue_seq]
                parent_ids = {message.retry_of for message in slot_messages if message.retry_of}
                leaves = [message for message in slot_messages if message.msg_id not in parent_ids]
                if len(leaves) != 1:
                    raise SpoolError(f"queue_seq {queue_seq} 存在分叉重试链，拒绝调度")
                leaf = leaves[0]
                active = [message for message in slot_messages if message.state not in TERMINAL_STATES]
                if len(active) > 1:
                    raise SpoolError(f"queue_seq {queue_seq} 存在多个活动消息，拒绝调度")
                return leaf
            return None

    def release_slot(self, dst: str, queue_seq: int, *, ruling_id: Optional[str] = None,
                     reason: str) -> bool:
        """持久放行一个目标槽位；重复调用幂等。"""
        if not isinstance(dst, str) or not _SAFE_ID.fullmatch(dst):
            raise SpoolError("目标 agent_id 格式非法")
        if not isinstance(queue_seq, int) or isinstance(queue_seq, bool) or queue_seq < 1:
            raise SpoolError("queue_seq 必须为正整数")
        with exclusive_lock(self.lock_path):
            meta = self._load_queue_meta_unlocked()
            if not any(message.dst == dst and message.queue_seq == queue_seq
                       for message in self._all_messages_unlocked()):
                raise MessageNotFoundError(f"目标 {dst} 的 queue_seq {queue_seq} 不存在")
            records = meta["released"].setdefault(dst, {})
            key = str(queue_seq)
            if key in records:
                existing = records[key]
                if ruling_id and existing.get("ruling_id") not in (None, ruling_id):
                    raise SpoolError("槽位已由另一 ruling_id 放行")
                if ruling_id and existing.get("ruling_id") is None:
                    existing["ruling_id"] = ruling_id
                    self._save_queue_meta_unlocked(meta)
                    return True
                return False
            records[key] = {"reason": reason, "ruling_id": ruling_id}
            self._save_queue_meta_unlocked(meta)
            return True

    def slot_released(self, dst: str, queue_seq: int) -> bool:
        with exclusive_lock(self.lock_path):
            return self._slot_released_unlocked(dst, queue_seq)

    def _all_messages_unlocked(self) -> List[Message]:
        found: dict[str, Message] = {}
        for directory in (self.pending_dir, self.done_dir):
            if not directory.is_dir():
                continue
            for path in directory.rglob("*.json"):
                message = self._load(path)
                current = found.get(message.msg_id)
                if current is not None and current != message:
                    # pending/done 双份只允许终态归档副本覆盖较旧 pending 副本。
                    if path.parent == self.done_dir and message.state in TERMINAL_STATES:
                        found[message.msg_id] = message
                    elif current.state not in TERMINAL_STATES:
                        raise SpoolError(f"msg_id {message.msg_id} 存在冲突副本")
                else:
                    found[message.msg_id] = message
        return list(found.values())

    def _slot_messages_unlocked(self, dst: str, queue_seq: Optional[int]) -> List[Message]:
        return [message for message in self._all_messages_unlocked()
                if message.dst == dst and (queue_seq is None or message.queue_seq == queue_seq)]

    def _assert_sequence_integrity_unlocked(self, dst: str, message_seqs: set[int], meta: dict) -> None:
        next_seq = int(meta["next_seq"].get(dst, 1))
        released = {int(value) for value in meta["released"].get(dst, {})}
        highest = max(message_seqs | released, default=0)
        if next_seq <= highest:
            raise QueueSequenceGapError(
                f"目标 {dst} 的 next_seq={next_seq} 不大于已知 queue_seq={highest}，停止投递"
            )
        known = sorted(message_seqs | released)
        expected = 1
        for sequence in known:
            if sequence < expected:
                continue
            if sequence > expected:
                raise QueueSequenceGapError(
                    f"目标 {dst} 的 queue_seq={expected} 未放行且消息记录缺失，拒绝跳过队列头"
                )
            expected += 1
        if expected < next_seq:
            raise QueueSequenceGapError(
                f"目标 {dst} 的 queue_seq={expected} 未放行且消息记录缺失，拒绝跳过队列头"
            )

    def _load_queue_meta_unlocked(self) -> dict:
        if not self.queue_meta_path.exists():
            return {"version": 1, "next_seq": {}, "released": {}}
        try:
            value = json.loads(self.queue_meta_path.read_text(encoding="utf-8"))
            if (not isinstance(value, dict) or value.get("version") != 1
                    or not isinstance(value.get("next_seq"), dict)
                    or not isinstance(value.get("released"), dict)):
                raise ValueError("queue metadata schema 错误")
            for dst, seq in value["next_seq"].items():
                if not _SAFE_ID.fullmatch(dst) or not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
                    raise ValueError("queue metadata next_seq 非法")
            for dst, records in value["released"].items():
                if not _SAFE_ID.fullmatch(dst) or not isinstance(records, dict):
                    raise ValueError("queue metadata released 非法")
                if any(not key.isdecimal() or not isinstance(item, dict) for key, item in records.items()):
                    raise ValueError("queue metadata released entry 非法")
            return value
        except (OSError, ValueError, TypeError) as exc:
            raise SpoolCorruptionError(self.queue_meta_path, f"queue metadata schema/data 错误: {exc}") from exc

    def _save_queue_meta_unlocked(self, meta: dict) -> None:
        atomic_write_text(self.queue_meta_path,
                          json.dumps(meta, ensure_ascii=False, sort_keys=True, indent=2) + "\n")

    def _load_dispatch_control_unlocked(self) -> dict:
        if not self.dispatch_control_path.exists():
            return {"version": 1, "halted": False, "incident_id": None,
                    "reason": "", "updated_at": ""}
        try:
            value = json.loads(self.dispatch_control_path.read_text(encoding="utf-8"))
            if (not isinstance(value, dict) or value.get("version") != 1
                    or not isinstance(value.get("halted"), bool)
                    or (value["halted"] and (not isinstance(value.get("incident_id"), str)
                                             or not value["incident_id"]))):
                raise ValueError("dispatch control schema 错误")
            return value
        except (OSError, ValueError, TypeError) as exc:
            raise SpoolCorruptionError(self.dispatch_control_path,
                                       f"dispatch-control schema/data 错误: {exc}") from exc

    def _save_dispatch_control_unlocked(self, value: dict) -> None:
        atomic_write_text(self.dispatch_control_path,
                          json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")

    def _slot_released_unlocked(self, dst: str, queue_seq: int) -> bool:
        meta = self._load_queue_meta_unlocked()
        return str(queue_seq) in meta["released"].get(dst, {})

    def _release_slot_unlocked(self, dst: str, queue_seq: int, *, reason: str,
                               ruling_id: Optional[str] = None) -> bool:
        meta = self._load_queue_meta_unlocked()
        records = meta["released"].setdefault(dst, {})
        key = str(queue_seq)
        if key in records:
            existing = records[key]
            if ruling_id and existing.get("ruling_id") is None:
                existing["ruling_id"] = ruling_id
                self._save_queue_meta_unlocked(meta)
                return True
            if ruling_id and existing.get("ruling_id") != ruling_id:
                raise SpoolError("槽位已由另一 ruling_id 放行")
            return False
        records[key] = {"reason": reason, "ruling_id": ruling_id}
        self._save_queue_meta_unlocked(meta)
        return True
