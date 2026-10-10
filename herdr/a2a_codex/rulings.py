"""操作员裁定的先写审计、幂等应用与启动补做。"""
from __future__ import annotations

import secrets
import re
from typing import Any, Dict, Optional

from .broker import DeliveryBroker
from .messages import (
    DELIVERY_UNCERTAIN,
    DELIVERED,
    FAILED,
    QUEUED,
    RETRYING,
    TARGET_BLOCKED,
    TARGET_MISSING,
    TIMEOUT,
    Message,
    new_msg_id,
)
from .storage import exclusive_lock

UNCERTAIN_TERMINAL_ACTIONS = {
    "delivered": (DELIVERED, "operator_confirmed"),
    "retry": (RETRYING, None),
    "abandon": (FAILED, None),
}
DETERMINATE_FAILURES = frozenset({FAILED, TIMEOUT, TARGET_BLOCKED, TARGET_MISSING})
_SAFE_TARGET = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class RulingError(RuntimeError):
    pass


class RulingManager:
    """实现协议 §7.3 的裁定语义；审计记录是恢复待应用裁定的依据。"""

    def __init__(self, broker: DeliveryBroker) -> None:
        self.broker = broker
        self.spool = broker.spool
        self.audit = broker.audit

    def resolve(self, msg_id: str, action: str, *, actor: str, reason: str,
                ruling_id: Optional[str] = None,
                retry_msg_id: Optional[str] = None) -> Dict[str, Any]:
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("actor 必须为非空字符串")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason 必须为非空字符串")
        ruling_id = ruling_id or ("r-" + secrets.token_hex(16))

        original = self.spool.get(msg_id)
        lock_path = self.broker.lock_dir / (original.dst + ".lock")
        with exclusive_lock(lock_path):
            # 先补做此前落盘但未完成的裁定，避免同一槽位出现两个未决动作。
            self.recover_pending(dst=original.dst)
            original = self.spool.get(msg_id)
            existing = self._ruling_by_id(ruling_id)
            if existing is not None:
                expected_rulings = {"delivered": {"delivered"},
                                    "retry": {"not_delivered_retry", "retry_terminal"},
                                    "abandon": {"abandon"},
                                    "abandon_and_continue": {"abandon_and_continue"}}
                if (existing.get("msg_id") != msg_id
                        or existing.get("actor") != actor.strip()
                        or existing.get("reason") != reason.strip()
                        or existing.get("ruling") not in expected_rulings.get(action, set())):
                    raise RulingError(f"ruling_id 已用于不同裁定: {ruling_id}")
                if any(item.get("event") == "RULING_VOIDED"
                       and item.get("voided_ruling_id") == ruling_id
                       for item in self.audit.read(strict=True)):
                    raise RulingError(f"ruling_id 已作废: {ruling_id}")
                self._apply(existing, recovered=False)
                return existing

            head = self.spool.queue_head(original.dst)
            if head is None or head.msg_id != msg_id:
                raise RulingError("只能裁定当前目标队列头")

            if original.state == DELIVERY_UNCERTAIN:
                if action not in {"delivered", "retry", "abandon"}:
                    raise RulingError("不确定态只允许 delivered / retry / abandon")
                new_state, evidence = UNCERTAIN_TERMINAL_ACTIONS[action]
                ruling = {"delivered": "delivered", "retry": "not_delivered_retry",
                          "abandon": "abandon"}[action]
                if retry_msg_id is not None:
                    raise RulingError("不确定态重试不创建新消息")
            elif original.state in DETERMINATE_FAILURES:
                if action not in {"retry", "abandon_and_continue"}:
                    raise RulingError("确定失败终态只允许 retry / abandon_and_continue")
                if action == "retry":
                    ruling = "retry_terminal"
                    new_state = original.state
                    evidence = None
                    retry_msg_id = retry_msg_id or new_msg_id()
                else:
                    ruling = "abandon_and_continue"
                    new_state = original.state
                    evidence = None
                    if retry_msg_id is not None:
                        raise RulingError("放弃并继续不创建重试消息")
            else:
                raise RulingError(f"状态 {original.state} 不接受操作员裁定")

            event = {"event": "OPERATOR_RULING", "ruling_id": ruling_id,
                     "actor": actor.strip(), "ruling": ruling, "reason": reason.strip(),
                     "msg_id": original.msg_id, "dst": original.dst,
                     "queue_seq": original.queue_seq, "previous_state": original.state,
                     "new_state": new_state}
            if evidence:
                event["evidence"] = evidence
            if ruling == "retry_terminal":
                event.update({"retry_msg_id": retry_msg_id, "retry_initial_state": QUEUED})

            persisted = self.audit.record(event)  # 协议要求：裁定先可靠落盘，再执行任何副作用。
            self._apply(persisted, recovered=False)
            return persisted

    def recover_pending(self, *, dst: Optional[str] = None) -> int:
        events = self.audit.read(strict=True)
        applied = {event.get("ruling_id") for event in events
                   if event.get("event") == "RULING_APPLIED"}
        voided = {event.get("voided_ruling_id") for event in events
                  if event.get("event") == "RULING_VOIDED"}
        rulings = [event for event in events if event.get("event") == "OPERATOR_RULING"]
        count = 0
        for event in rulings:
            ruling_id = event.get("ruling_id")
            if not isinstance(ruling_id, str) or not ruling_id:
                raise RulingError("OPERATOR_RULING 缺少有效 ruling_id；停止派发")
            target = event.get("dst")
            if not isinstance(target, str) or not _SAFE_TARGET.fullmatch(target):
                raise RulingError("OPERATOR_RULING 缺少目标归属；全局停止派发")
            if ruling_id in applied or ruling_id in voided:
                continue
            if dst is not None and target != dst:
                continue
            if dst is None:
                lock_path = self.broker.lock_dir / (target + ".lock")
                with exclusive_lock(lock_path):
                    self._apply(event, recovered=True)
            else:
                self._apply(event, recovered=True)
            count += 1
        return count

    def void(self, ruling_id: str, *, actor: str, reason: str,
             verified_effects: Any) -> Dict[str, Any]:
        """作废尚未完整应用的裁定；只停止补做，不撤销已经持久化的效果。"""
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("actor 必须为非空字符串")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason 必须为非空字符串")
        if verified_effects is None or verified_effects == "":
            raise ValueError("必须记录作废前核实的已生效效果")
        ruling = self._ruling_by_id(ruling_id)
        if ruling is None:
            raise RulingError(f"找不到可作废的 OPERATOR_RULING: {ruling_id}")
        target = ruling.get("dst")
        if not isinstance(target, str) or not _SAFE_TARGET.fullmatch(target):
            raise RulingError("裁定缺少有效目标归属；全局停止派发")
        lock_path = self.broker.lock_dir / (target + ".lock")
        with exclusive_lock(lock_path):
            events = self.audit.read(strict=True)
            if any(event.get("event") == "RULING_APPLIED"
                   and event.get("ruling_id") == ruling_id for event in events):
                raise RulingError("已完整应用的裁定不可作废；按实际持久化状态重新裁定")
            existing = next((event for event in events
                             if event.get("event") == "RULING_VOIDED"
                             and event.get("voided_ruling_id") == ruling_id), None)
            if existing is not None:
                if (existing.get("actor") == actor.strip()
                        and existing.get("reason") == reason.strip()
                        and existing.get("verified_effects") == verified_effects):
                    return existing
                raise RulingError(f"ruling_id 已以不同核实记录作废: {ruling_id}")
            return self.audit.record({"event": "RULING_VOIDED",
                                      "voided_ruling_id": ruling_id,
                                      "actor": actor.strip(), "reason": reason.strip(),
                                      "verified_effects": verified_effects,
                                      "msg_id": ruling.get("msg_id"), "dst": target,
                                      "queue_seq": ruling.get("queue_seq"),
                                      "detail": "停止自动补做；不撤销已持久化效果"})

    def _apply(self, event: Dict[str, Any], *, recovered: bool) -> None:
        ruling_id = event["ruling_id"]
        msg_id = event["msg_id"]
        original = self.spool.get(msg_id)
        action = event["ruling"]
        previous = event["previous_state"]
        new_state = event["new_state"]
        steps = []

        if action in {"delivered", "not_delivered_retry", "abandon"}:
            if original.state == previous:
                detail = "操作员裁定: " + str(event.get("reason", ""))
                evidence = "operator_confirmed" if action == "delivered" else None
                self.broker._transition(original, new_state, detail,
                                        evidence=evidence, ruling_id=ruling_id)
                steps.append("state_transition")
            elif original.state != new_state:
                raise RulingError(f"裁定状态冲突: expected {previous}/{new_state}, got {original.state}")
            if action in {"delivered", "abandon"}:
                self._release(original, ruling_id, "DELIVERED" if action == "delivered"
                              else "OPERATOR_ABANDON", recovered=recovered)
                steps.append("queue_release")
        elif action == "retry_terminal":
            if original.state != previous:
                raise RulingError(f"终态重试原消息状态已变化: {original.state}")
            retry = self._retry_message(original, event)
            self.spool.enqueue_retry(retry, original_msg_id=original.msg_id)
            self._record_once({"event": "RETRY_ENQUEUED", "ruling_id": ruling_id,
                               "msg_id": retry.msg_id, "retry_of": original.msg_id,
                               "dst": retry.dst, "queue_seq": retry.queue_seq,
                               "state": QUEUED, "detail": "操作员裁定重试入队"})
            steps.append("retry_message_enqueued")
        elif action == "abandon_and_continue":
            if original.state != previous:
                raise RulingError(f"放弃并继续原消息状态已变化: {original.state}")
            self._release(original, ruling_id, "OPERATOR_ABANDON_AND_CONTINUE",
                          recovered=recovered)
            steps.append("queue_release")
        else:
            raise RulingError(f"未知 ruling 类型: {action}")

        self._record_once({"event": "RULING_APPLIED", "ruling_id": ruling_id,
                           "msg_id": msg_id, "dst": original.dst,
                           "steps": steps, "detail": "恢复时补做" if recovered else "裁定已生效"})

    def _release(self, original: Message, ruling_id: str, reason: str, *, recovered: bool) -> None:
        self.spool.release_slot(original.dst, original.queue_seq, ruling_id=ruling_id,
                                reason=reason)
        self._record_once({"event": "QUEUE_RELEASED", "ruling_id": ruling_id,
                           "msg_id": original.msg_id, "dst": original.dst,
                           "queue_seq": original.queue_seq, "reason": reason,
                           "detail": "恢复时补做" if recovered else "操作员裁定"})

    @staticmethod
    def _retry_message(original: Message, ruling: Dict[str, Any]) -> Message:
        # ts 与 retry_msg_id 都在 OPERATOR_RULING 中固定；重启补做会重建同一份消息。
        return Message(msg_id=ruling["retry_msg_id"], created_at=ruling["ts"],
                       edge_id=original.edge_id, src=original.src, dst=original.dst,
                       project_id=original.project_id, ip_id=original.ip_id,
                       session=original.session, text=original.text, state=QUEUED,
                       topology_revision=original.topology_revision, attempts=0,
                       detail="retry_of=" + original.msg_id, updated_at=ruling["ts"],
                       queue_seq=original.queue_seq, retry_of=original.msg_id)

    def _record_once(self, event: Dict[str, Any]) -> None:
        ruling_id = event.get("ruling_id")
        event_name = event.get("event")
        msg_id = event.get("msg_id")
        if any(item.get("event") == event_name and item.get("ruling_id") == ruling_id
               and item.get("msg_id") == msg_id for item in self.audit.read(strict=True)):
            return
        self.audit.record(event)

    def _ruling_by_id(self, ruling_id: str) -> Optional[Dict[str, Any]]:
        return next((event for event in self.audit.read(strict=True)
                     if event.get("event") == "OPERATOR_RULING"
                     and event.get("ruling_id") == ruling_id), None)
