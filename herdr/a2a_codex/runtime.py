"""常驻 Broker 调度器：单实例运行、每目标独立串行 worker。"""
from __future__ import annotations

import fcntl
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from .broker import DeliveryBroker, DeliveryResult
from .messages import DELIVERED, DELIVERY_UNCERTAIN, Message


class BrokerAlreadyRunningError(RuntimeError):
    """同一状态目录已有 Broker 持有单实例锁。"""


class DispatchHaltedError(RuntimeError):
    """全局派发处于持久停止状态，必须由操作员显式恢复。"""


_LOG = logging.getLogger(__name__)


@dataclass
class _Worker:
    thread: threading.Thread
    blocked_signature: Optional[tuple[str, str, str]] = None
    error: Optional[BaseException] = None


class BrokerRuntime:
    """轮询持久队列并为每个目标维持至多一个串行 worker。

    worker 只在 Spool API 的短调用期间持有文件锁；等待 Herdr 时不持 Spool 锁。
    ``stop_event`` 可用于优雅退出；正在执行的 Herdr 调用会先返回，再停止 worker。
    """

    def __init__(self, broker: DeliveryBroker, *, poll_interval_s: float = 0.5,
                 alert_scan_interval_s: float = 60.0,
                 alert_reminder_s: float = 3600.0,
                 alert_escalation_s: float = 86400.0) -> None:
        if min(poll_interval_s, alert_scan_interval_s, alert_reminder_s, alert_escalation_s) <= 0:
            raise ValueError("轮询和告警时间参数必须大于 0")
        if alert_escalation_s < alert_reminder_s:
            raise ValueError("告警升级时限不能早于提醒时限")
        self.broker = broker
        self.spool = broker.spool
        self.audit = broker.audit
        self.poll_interval_s = poll_interval_s
        self.alert_scan_interval_s = alert_scan_interval_s
        self.alert_reminder_s = alert_reminder_s
        self.alert_escalation_s = alert_escalation_s
        self.lock_path = self.spool.root / "broker-runtime.lock"
        self._stop = threading.Event()
        self._workers: Dict[str, _Worker] = {}
        self._blocked: Dict[str, tuple[str, str, str]] = {}
        self._workers_lock = threading.Lock()
        self._fatal_error: Optional[BaseException] = None

    def stop(self) -> None:
        self._stop.set()

    def run(self, stop_event: Optional[threading.Event] = None) -> None:
        """阻塞运行直到收到停止事件；运行期间持有状态目录级单实例锁。

        Spool/调度扫描错误按协议 fail-closed：停止派发、尽力写入
        ``DISPATCH_HALTED`` 审计事件，然后向调用方抛出异常。
        """
        external_stop = stop_event or threading.Event()
        self._stop.clear()
        self._fatal_error = None
        self.broker.dispatch_guard = lambda: not self._stop.is_set()
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BrokerAlreadyRunningError("该状态目录已有 Broker 实例运行") from exc

            failure: Optional[BaseException] = None
            try:
                control = self.spool.dispatch_control()
                if control["halted"]:
                    events = self.audit.read(strict=True)
                    if any(event.get("event") == "DISPATCH_RESUMED"
                           and event.get("incident_id") == control["incident_id"]
                           for event in events):
                        # 恢复事件已可靠落盘、清除门闩前崩溃：按事件完成幂等清理。
                        self.spool.clear_dispatch_halt(control["incident_id"])
                    else:
                        self._ensure_halt_audited(control)
                        raise DispatchHaltedError(
                            f"派发被 incident {control['incident_id']} 停止: {control['reason']}"
                        )
                # 协议 §8 要求先恢复 Spool / DISPATCHING，再启动任何投递 worker。
                self.broker.recover_startup()
                next_alert_scan = 0.0
                while not self._stop.is_set() and not external_stop.is_set():
                    self._reap_workers()
                    self._schedule_workers()
                    now_mono = time.monotonic()
                    if now_mono >= next_alert_scan:
                        self._emit_due_alerts(datetime.now(timezone.utc))
                        next_alert_scan = now_mono + self.alert_scan_interval_s
                    if self._stop.wait(self.poll_interval_s):
                        break
                if self._fatal_error is not None:
                    raise RuntimeError("Broker worker 遇到错误，fail-closed 停止全部派发") from self._fatal_error
            except BaseException as exc:
                failure = exc
                self._stop.set()
                if not isinstance(exc, DispatchHaltedError):
                    try:
                        control = self.spool.halt_dispatch(str(exc))
                        self._ensure_halt_audited(control)
                    except Exception:
                        # 若 Spool 控制文件本身损坏，仍尽力留审计；启动时读取失败会继续 fail-closed。
                        try:
                            self.audit.record({"event": "DISPATCH_HALTED", "detail": str(exc),
                                               "reason": "Broker 调度失败，按 fail-closed 停止"})
                        except Exception:
                            pass
            finally:
                self._stop.set()
                self._join_workers()
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            if failure is not None:
                raise failure

    def resume_dispatch(self, *, actor: str, reason: str,
                        quarantine_location: str) -> dict:
        """在数据可读、裁定已对账后审计恢复，再清除持久停止门闩。"""
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("actor 必须为非空字符串")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason 必须为非空字符串")
        if not isinstance(quarantine_location, str) or not quarantine_location.strip():
            raise ValueError("必须记录隔离数据位置；没有隔离数据时填写 none")
        control = self.spool.dispatch_control()
        if not control["halted"]:
            raise DispatchHaltedError("当前没有全局停止事件")

        # 数据仍损坏或裁定不可读时，以下任一步失败都保留 halted 门闩。
        self.spool.recover()
        self.broker.recover_startup()
        events = self.audit.read(strict=True)
        applied = {event.get("ruling_id") for event in events
                   if event.get("event") == "RULING_APPLIED"}
        voided = {event.get("voided_ruling_id") for event in events
                  if event.get("event") == "RULING_VOIDED"}
        unresolved = [event.get("ruling_id") for event in events
                      if event.get("event") == "OPERATOR_RULING"
                      and event.get("ruling_id") not in applied
                      and event.get("ruling_id") not in voided]
        if unresolved:
            raise DispatchHaltedError("仍有未对账的操作员裁定，不能恢复派发")

        repaired_incidents = {event.get("incident_id") for event in events
                              if event.get("event") == "SPOOL_REPAIRED"}
        for incident in self.spool.quarantine_incidents(unresolved_only=True):
            if incident["incident_id"] in repaired_incidents:
                self.spool.mark_quarantine_resolved(incident["incident_id"])
        if self.spool.quarantine_incidents(unresolved_only=True):
            raise DispatchHaltedError("仍有未核验恢复的 Spool 隔离项，不能恢复派发")

        resumed = next((event for event in events
                        if event.get("event") == "DISPATCH_RESUMED"
                        and event.get("incident_id") == control["incident_id"]), None)
        if resumed is None:
            resumed = self.audit.record({"event": "DISPATCH_RESUMED",
                                         "incident_id": control["incident_id"],
                                         "actor": actor.strip(), "reason": reason.strip(),
                                         "quarantine_location": quarantine_location.strip()})
        self.spool.clear_dispatch_halt(control["incident_id"])
        return resumed

    def resolve_spool_corruption(self, incident_id: str, *, actor: str, reason: str,
                                 verification: str) -> dict:
        """操作员从备份恢复原文件后，核验、审计并关闭一个 Spool 隔离项。

        此动作不解除全局派发门闩；需另外显式调用 ``resume_dispatch``。
        """
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("actor 必须为非空字符串")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason 必须为非空字符串")
        if not isinstance(verification, str) or not verification.strip():
            raise ValueError("必须记录恢复数据的核验依据")
        incident = self.spool.verify_quarantine_restored(incident_id)
        if incident.get("resolved") is True:
            return incident
        events = self.audit.read(strict=True)
        prior = next((event for event in events
                      if event.get("event") == "SPOOL_REPAIRED"
                      and event.get("incident_id") == incident_id), None)
        if prior is None:
            self.audit.record({"event": "SPOOL_REPAIRED", "incident_id": incident_id,
                               "actor": actor.strip(), "reason": reason.strip(),
                               "verification": verification.strip(),
                               "original_path": incident["original_path"],
                               "quarantine_path": incident["quarantine_path"]})
        return self.spool.mark_quarantine_resolved(incident_id)

    def _emit_due_alerts(self, now: datetime) -> int:
        """为仍占据队列头的 DELIVERY_UNCERTAIN 发出持久、幂等的定时告警。"""
        events = self.audit.read(strict=True)
        transitions: dict[str, dict] = {}
        sent = {(event.get("msg_id"), event.get("cycle_started_at"), event.get("level"))
                for event in events if event.get("event") == "QUEUE_ALERT"}
        for event in events:
            if event.get("event") == "STATE_TRANSITION" and event.get("state") == "DELIVERY_UNCERTAIN":
                msg_id = event.get("msg_id")
                if isinstance(msg_id, str):
                    transitions[msg_id] = event

        emitted = 0
        for dst in self.spool.queue_targets():
            head = self.spool.queue_head(dst)
            if head is None or head.state != DELIVERY_UNCERTAIN:
                continue
            transition = transitions.get(head.msg_id, {})
            started_text = transition.get("ts") or head.updated_at
            try:
                started = datetime.fromisoformat(str(started_text).replace("Z", "+00:00"))
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                # 无可信时间戳时不猜测已达到告警时限；队列仍保持暂停。
                continue
            elapsed = max(0.0, (now - started).total_seconds())
            cycle_started_at = str(started_text)
            queued = self.spool.pending(dst) + [message for message in self.spool.done()
                                                 if message.dst == dst]
            later_count = len({(message.queue_seq, message.msg_id) for message in queued
                               if message.queue_seq is not None and head.queue_seq is not None
                               and message.queue_seq > head.queue_seq})
            levels = []
            if elapsed >= self.alert_reminder_s:
                levels.append(("reminder", "1 小时提醒"))
            if elapsed >= self.alert_escalation_s:
                levels.append(("escalation", "24 小时升级"))
            for level, label in levels:
                key = (head.msg_id, cycle_started_at, level)
                if key in sent:
                    continue
                event = {"event": "QUEUE_ALERT", "msg_id": head.msg_id, "dst": dst,
                         "queue_seq": head.queue_seq,
                         "state": head.state, "cycle_started_at": cycle_started_at,
                         "level": level, "paused_message_count": later_count,
                         "available_actions": ["delivered", "retry", "abandon"],
                         "detail": f"{label}: DELIVERY_UNCERTAIN 队列头仍未裁定；后续暂停 {later_count} 条"}
                self.audit.record(event)
                _LOG.warning("A2A 队列告警[%s]: dst=%s msg_id=%s queue_seq=%s，后续暂停 %d 条；"
                             "可执行 resolve delivered/retry/abandon",
                             label, dst, head.msg_id, head.queue_seq, later_count)
                sent.add(key)
                emitted += 1
        return emitted

    def _ensure_halt_audited(self, control: dict) -> None:
        if any(event.get("event") == "DISPATCH_HALTED"
               and event.get("incident_id") == control.get("incident_id")
               for event in self.audit.read(strict=True)):
            return
        self.audit.record({"event": "DISPATCH_HALTED", "incident_id": control["incident_id"],
                           "detail": control.get("reason", "Broker 调度失败"),
                           "reason": "Broker 调度失败，按 fail-closed 停止"})

    def _schedule_workers(self) -> None:
        # 若队列元数据或消息文件损坏，queue_targets/queue_head 会抛错；run() 随即全局停止。
        for dst in self.spool.queue_targets():
            with self._workers_lock:
                current = self._workers.get(dst)
                if current is not None and current.thread.is_alive():
                    continue
                if current is not None:
                    current.thread.join()
                    if current.error is not None:
                        raise RuntimeError(f"目标 {dst} 的 Broker worker 失败") from current.error
                    self._workers.pop(dst, None)

                head = self.spool.queue_head(dst)
                if head is None:
                    self._blocked.pop(dst, None)
                    continue
                signature = self._signature(head)
                if self._blocked.get(dst) == signature:
                    # 同一队列头仍未解决：等待状态/裁定变化，不重复查询或刷审计。
                    continue
                self._blocked.pop(dst, None)
                worker = _Worker(threading.Thread(target=self._run_target,
                                                  args=(dst,),
                                                  name="a2a-broker-" + dst,
                                                  daemon=False))
                self._workers[dst] = worker
                worker.thread.start()

    def _run_target(self, dst: str) -> None:
        worker = self._workers[dst]
        try:
            while not self._stop.is_set():
                result: Optional[DeliveryResult] = self.broker.process_target(dst)
                if result is None:
                    worker.blocked_signature = None
                    self._blocked.pop(dst, None)
                    return
                if result.state != DELIVERED:
                    head = self.spool.queue_head(dst)
                    worker.blocked_signature = self._signature(head) if head is not None else None
                    if worker.blocked_signature is not None:
                        self._blocked[dst] = worker.blocked_signature
                    return
                # DELIVERED 会持久放行当前槽位；继续该目标下一条队列头，保持 FIFO。
        except BaseException as exc:
            worker.error = exc
            self._fatal_error = exc
            self._stop.set()

    @staticmethod
    def _signature(message: Message) -> tuple[str, str, str]:
        return (message.msg_id, message.state, message.updated_at)

    def _reap_workers(self) -> None:
        with self._workers_lock:
            for dst, worker in list(self._workers.items()):
                if worker.thread is threading.current_thread() or worker.thread.is_alive():
                    continue
                worker.thread.join()
                if worker.error is not None:
                    raise RuntimeError(f"目标 {dst} 的 Broker worker 失败") from worker.error
                if worker.blocked_signature is None:
                    self._workers.pop(dst, None)

    def _join_workers(self) -> None:
        with self._workers_lock:
            workers = list(self._workers.values())
        for worker in workers:
            if worker.thread is not threading.current_thread() and worker.thread.ident is not None:
                worker.thread.join()
