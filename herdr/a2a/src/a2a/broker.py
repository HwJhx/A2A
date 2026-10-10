"""Broker 进程(阶段 5c;协议见 herdr/claude/08-protocol.md §6–§8)。

一个状态目录、一个 herdr 会话、一个 broker:
  * 单实例:<状态目录>/broker.lock 上的非阻塞 flock,第二个 broker 启动即失败。
  * 每个目标一个串行 worker(线程),**只处理队列头**(08 §7.1):
      - 队列头有未终结消息 -> 交给 DeliveryEngine 投递;
      - 队列头是 DELIVERY_UNCERTAIN -> 暂停该目标,等操作员裁定(定时告警);
      - 队列头已到终态:DELIVERED -> 放行;确定失败 -> 暂停该目标,等操作员"重试"或"放弃并继续"。
    暂停只影响这一个目标。没有任何由时间触发的放行(08 §6)。
  * 启动恢复按 08 §8:recover() -> DISPATCHING 转 DELIVERY_UNCERTAIN -> 对账操作员裁定(5d)
    -> 检查各目标队列状态(损坏的目标 fail-closed)-> 从队列头继续。
  * 读不出目标归属的损坏数据 -> 全局停止投递(08 §7.2),写 <状态目录>/dispatch.halted,
    操作员处理后用 `a2a dispatch resume`(5d)恢复。
  * herdr 服务不在时整体暂停投递(不消耗查询失败次数),避免整批消息因服务重启而 TIMEOUT。
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from ._fsutil import atomic_write_text
from .audit import AuditLog
from .delivery import DeliveryEngine, HerdrLike
from .messages import DELIVERED, DELIVERY_UNCERTAIN, DISPATCHING, Message
from .policy import BrokerConfig
from . import rulings
from .registry import Registry
from .spool import QueueHead, QueueStateError, Spool, SpoolError

log = logging.getLogger("a2a.broker")

QUEUE_PAUSED = "QUEUE_PAUSED"
QUEUE_RELEASED = "QUEUE_RELEASED"
DISPATCH_HALTED = "DISPATCH_HALTED"
BROKER_STARTED = "BROKER_STARTED"
ALERT = "ALERT"


class BrokerAlreadyRunning(RuntimeError):
    pass


def _parse_iso(value: str) -> Optional[float]:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


class Broker:
    def __init__(
        self,
        *,
        spool: Spool,
        registry: Registry,
        audit: AuditLog,
        client: HerdrLike,
        session: str,
        state_dir: Path,
        semantics_verified: bool,
        herdr_version: Optional[str] = None,
        config: Optional[BrokerConfig] = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if not session:
            raise ValueError("broker 必须绑定一个 herdr 会话")
        self.spool, self.registry, self.audit, self.client = spool, registry, audit, client
        self.session = session
        self.state_dir = Path(state_dir)
        self.lock_path = self.state_dir / "broker.lock"
        self.halt_path = self.state_dir / "dispatch.halted"
        self.config = config or BrokerConfig()
        self.semantics_verified = semantics_verified
        self.herdr_version = herdr_version
        self._sleep, self._monotonic, self._wall = sleep, monotonic, wall_clock
        self.stop_event = threading.Event()
        # 真实运行时,投递引擎里的等待用 stop_event.wait,停机时立刻醒来;测试注入的假 sleep 保持原样
        engine_sleep = (lambda s: self.stop_event.wait(s)) if sleep is time.sleep else sleep
        # 停机或全局停止投递(08 §7.2)时,在途的 worker 也不再开始新的投递:引擎在等待循环每一轮、
        # 写 DISPATCHING 之前、重试退避之后检查这里,消息保持原状态返回。已经发出的 prompt 照常收尾。
        # 局限:检查与写 DISPATCHING 之间仍有极短窗口,其间写入的停止标记要到下一条消息才生效。
        self.engine = DeliveryEngine(spool, registry, audit, client, config=self.config,
                                     semantics_verified=semantics_verified, sleep=engine_sleep,
                                     monotonic=monotonic,
                                     stopping=lambda: self.stop_event.is_set() or self.halted() is not None)
        self._lock_handle: Any = None
        self._state_lock = threading.Lock()
        self._paused: Dict[str, Tuple[int, str, str]] = {}       # dst -> (queue_seq, msg_id, state)
        self._alerted: Dict[Tuple[str, str], str] = {}           # (dst, msg_id) -> 已发出的最高告警级别
        self._cooldown: Dict[str, float] = {}                    # dst -> 冷却结束时间(monotonic)
        self._foreign_warned: set = set()
        self._workers: Dict[str, threading.Thread] = {}
        self._server_down_logged = False

    # ---- 单实例 --------------------------------------------------------
    def acquire(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise BrokerAlreadyRunning(f"已有 broker 在使用状态目录 {self.state_dir}") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self._lock_handle = handle

    def release_lock(self) -> None:
        if self._lock_handle is not None:
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
            self._lock_handle.close()
            self._lock_handle = None

    # ---- 启动恢复(08 §8) ------------------------------------------------
    def recover(self) -> Dict[str, Any]:
        self.audit.record({"state": BROKER_STARTED, "session": self.session, "pid": os.getpid(),
                           "herdr_version": self.herdr_version, "semantics_verified": self.semantics_verified,
                           "detail": "broker 启动"})
        if not self.semantics_verified:
            log.warning("herdr 版本 %s 不在 08 §5 的实测范围,错误码一律按保守分类", self.herdr_version)
        summary: Dict[str, Any] = {"spool": self.spool.recover(), "dispatching_to_uncertain": 0,
                                   "rulings_reconciled": 0, "failed_closed_targets": []}
        try:
            pending = self.spool.pending()
        except SpoolError as exc:
            self.halt(f"启动恢复时读不出待投递消息: {exc}")
            return summary
        for message in pending:
            if message.state == DISPATCHING and message.session == self.session:
                updated = self.spool.update(message.msg_id, state=DELIVERY_UNCERTAIN,
                                            detail="broker 启动恢复:上次停在 DISPATCHING,prompt 可能已发出")
                self._audit_message(updated, updated.detail)
                summary["dispatching_to_uncertain"] += 1
        summary["rulings_reconciled"] = self.reconcile_rulings()
        try:
            targets = self.spool.queue_targets()
        except SpoolError as exc:
            self.halt(f"启动恢复时读不出目标队列: {exc}")
            return summary
        for dst in targets:
            try:
                self.spool.head(dst)
            except SpoolError as exc:
                self._pause_broken(dst, exc)
                summary["failed_closed_targets"].append(dst)
        return summary

    def reconcile_rulings(self) -> int:
        """08 §8 第 3 步:补做已落盘但未生效的操作员裁定;读不出的裁定或补做失败 -> 全局停止(fail-closed)。"""
        with rulings.lock(self.state_dir):  # 与并发的 a2a resolve / ruling void 串行
            count, problem = rulings.reconcile(self.spool, self.audit)
        if problem:
            self.halt(problem)
        return count

    # ---- 全局停止 --------------------------------------------------------
    def halted(self) -> Optional[str]:
        if not self.halt_path.exists():
            return None
        try:
            return json.loads(self.halt_path.read_text(encoding="utf-8")).get("reason", "未知原因")
        except (OSError, ValueError):
            return "dispatch.halted 存在(内容无法解析)"

    def halt(self, reason: str) -> None:
        if self.halted() is None:
            atomic_write_text(self.halt_path, json.dumps(
                {"reason": reason, "at": datetime.now(timezone.utc).isoformat(), "pid": os.getpid()},
                ensure_ascii=False) + "\n")
            self.audit.record({"state": DISPATCH_HALTED, "session": self.session, "detail": reason})
            log.error("全局停止投递:%s(处理后执行 a2a dispatch resume)", reason)

    # ---- 一个目标的串行处理 -------------------------------------------------
    def process_target(self, dst: str) -> str:
        """处理一个目标,直到队列空、暂停或该目标不归本 broker。返回结束原因。"""
        while not self.stop_event.is_set():
            if self.halted() is not None:
                return "halted"
            try:
                head = self.spool.head(dst)
            except SpoolError as exc:
                self._pause_broken(dst, exc)
                return "paused"
            if head is None:
                self._unpause(dst)
                return "empty"
            if head.active is not None:
                message = head.active
                if message.session != self.session:
                    if dst not in self._foreign_warned:
                        self._foreign_warned.add(dst)
                        log.warning("目标 %s 的消息属于会话 %s,本 broker 服务 %s,跳过", dst, message.session,
                                    self.session)
                    return "foreign"
                if message.state == DELIVERY_UNCERTAIN:
                    self._pause(dst, head, message, "队列头投递结果不确定,等待操作员裁定(a2a resolve)")
                    return "paused"
                self._unpause(dst)
                try:
                    self.engine.deliver(message.msg_id)
                except Exception as exc:  # broker 内部错误:消息原样留着,冷却后再试
                    log.exception("投递 %s 时出错", message.msg_id)
                    self._audit_message(message, f"broker 内部错误,{self.config.error_cooldown_s:g} 秒后重试: {exc}")
                    with self._state_lock:
                        self._cooldown[dst] = self._monotonic() + self.config.error_cooldown_s
                    return "error"
                continue
            last = head.last_terminal
            assert last is not None  # spool.head 保证:无活动消息时一定有终态消息,否则已抛 QueueStateError
            if last.state == DELIVERED:
                rid = self._delivered_ruling(last)
                record = self.spool.release(dst, head.queue_seq, reason="delivered", ruling_id=rid)
                if record.get("already_released"):
                    continue  # 裁定线程刚好先放行了;谁实际放行谁写审计,这里不再记
                self._audit_queue(QUEUE_RELEASED, dst, head, last,
                                  "操作员裁定为已送达,自动放行" if rid else "队列头已送达,自动放行",
                                  record.get("ruling_id"))
                continue
            self._pause(dst, head, last, f"队列头确定失败({last.state}),等待操作员重试或放弃并继续")
            return "paused"
        return "stopped"

    # ---- 扫描与调度 --------------------------------------------------------
    def scan(self, *, threaded: bool = True) -> Dict[str, str]:
        """扫描一遍:为需要处理的目标启动 worker(threaded=False 时同步处理,测试用)。"""
        results: Dict[str, str] = {}
        if self.halted() is not None:
            return results
        if not self._server_up():
            return results
        try:
            targets = self.spool.queue_targets()
        except SpoolError as exc:
            self.halt(f"读不出目标队列: {exc}")
            return results
        now = self._monotonic()
        for dst in targets:
            with self._state_lock:
                if self._cooldown.get(dst, 0) > now:
                    continue
                worker = self._workers.get(dst)
                if worker is not None and worker.is_alive():
                    continue
                if dst in self._paused and not self._pause_changed(dst):
                    continue
            if threaded:
                worker = threading.Thread(target=self._worker, args=(dst,), name=f"a2a-{dst}", daemon=True)
                with self._state_lock:
                    self._workers[dst] = worker
                worker.start()
            else:
                results[dst] = self.process_target(dst)
        self.check_alerts()
        return results

    def _worker(self, dst: str) -> None:
        try:
            self.process_target(dst)
        except Exception:
            log.exception("目标 %s 的 worker 异常退出", dst)
            with self._state_lock:
                self._cooldown[dst] = self._monotonic() + self.config.error_cooldown_s

    def run_forever(self) -> None:
        log.info("broker 开始服务会话 %s(herdr %s,实测版本=%s)", self.session, self.herdr_version,
                 self.semantics_verified)
        while not self.stop_event.is_set():
            reason = self.halted()
            if reason is not None:
                log.error("投递已全局停止:%s", reason)
            self.scan()
            self.stop_event.wait(self.config.scan_interval_s)
        self._join_workers()

    def _join_workers(self) -> None:
        """停机收尾:等投递线程结束。等待中的会立刻返回;已调用 prompt 的最长等观察窗口加调用余量。
        超时仍未结束就记日志后退出,该消息停在 DISPATCHING,下次启动按 08 §8 转不确定。"""
        deadline = self._monotonic() + self.config.observe_window_s + 30
        with self._state_lock:
            workers = list(self._workers.items())
        for dst, worker in workers:
            worker.join(timeout=max(0.0, deadline - self._monotonic()))
            if worker.is_alive():
                log.error("停机时目标 %s 的投递还没结束,直接退出;正在投递的消息下次启动会转为 DELIVERY_UNCERTAIN", dst)

    def _server_up(self) -> bool:
        checker = getattr(self.client, "is_server_running", None)
        if checker is None:
            return True
        up = bool(checker())
        if not up and not self._server_down_logged:
            log.warning("herdr 会话 %s 的服务不在,暂停投递直到它恢复", self.session)
        self._server_down_logged = not up
        return up

    # ---- 暂停与告警 --------------------------------------------------------
    def _pause(self, dst: str, head: QueueHead, message: Message, reason: str) -> None:
        signature = (head.queue_seq, message.msg_id, message.state)
        with self._state_lock:
            if self._paused.get(dst) == signature:
                return
            self._paused[dst] = signature
        self._audit_queue(QUEUE_PAUSED, dst, head, message, reason, None)
        log.warning("暂停目标 %s:%s(msg_id=%s,queue_seq=%s)", dst, reason, message.msg_id, head.queue_seq)

    def _pause_broken(self, dst: str, exc: Exception) -> None:
        signature = (-1, "", type(exc).__name__)
        with self._state_lock:
            if self._paused.get(dst) == signature:
                return
            self._paused[dst] = signature
        self.audit.record({"state": QUEUE_PAUSED, "dst": dst, "session": self.session,
                           "detail": f"队列状态无法读取,停止该目标的投递(fail-closed): {exc}"})
        log.error("目标 %s 的队列状态无法读取,停止该目标的投递:%s", dst, exc)

    def _unpause(self, dst: str) -> None:
        with self._state_lock:
            self._paused.pop(dst, None)

    def _pause_changed(self, dst: str) -> bool:
        """暂停的目标,队列头是否已经变化(例如操作员已裁定)。调用方持有 _state_lock。"""
        signature = self._paused[dst]
        try:
            head = self.spool.head(dst)
        except SpoolError:
            return signature[0] != -1
        if head is None:
            return True
        current = head.active or head.last_terminal
        return (head.queue_seq, current.msg_id if current else "", current.state if current else "") != signature

    def check_alerts(self) -> None:
        """不确定的队列头:满 alert_remind_s 提醒,满 alert_escalate_s 升级。只告警,不改变任何状态(08 §6)。"""
        with self._state_lock:
            paused = dict(self._paused)
        now = self._wall()
        for dst, (seq, msg_id, state) in paused.items():
            if state != DELIVERY_UNCERTAIN:
                continue
            try:
                message = self.spool.get(msg_id)
            except SpoolError:
                continue
            since = _parse_iso(message.updated_at)
            if since is None:
                continue
            age = now - since
            level = ("escalate" if age >= self.config.alert_escalate_s
                     else "remind" if age >= self.config.alert_remind_s else None)
            if level is None or self._alerted.get((dst, msg_id)) in (level, "escalate"):
                continue
            self._alerted[(dst, msg_id)] = level
            waiting = len([m for m in self.spool.pending(dst) if m.queue_seq != seq])
            self.audit.record({
                "state": ALERT, "level": level, "dst": dst, "msg_id": msg_id, "queue_seq": seq,
                "session": self.session, "paused_messages_behind": waiting,
                "detail": (f"{dst} 的队列头已不确定 {age / 3600:.1f} 小时,其后 {waiting} 条消息被暂停;"
                           f"请裁定:a2a resolve {msg_id} delivered | retry | abandon"),
            })
            (log.error if level == "escalate" else log.warning)(
                "[%s] %s 的队列头 %s 已不确定 %.1f 小时,其后 %d 条被暂停", level, dst, msg_id, age / 3600, waiting)

    # ---- 审计 ------------------------------------------------------------
    def _audit_message(self, message: Message, detail: str) -> None:
        self.audit.record({"msg_id": message.msg_id, "edge_id": message.edge_id, "src": message.src,
                           "dst": message.dst, "state": message.state, "detail": detail,
                           "session": message.session, "topology_revision": message.topology_revision,
                           "queue_seq": message.queue_seq})

    def _delivered_ruling(self, message: Message) -> Optional[str]:
        """消息是被操作员裁定为已送达的,返回该裁定的 ruling_id(放行要与它关联,08 §7.3);否则 None。
        消息上的 ruling_id 也可能来自更早的"未送达,重试"裁定,所以要查审计确认裁定类型。"""
        if not message.ruling_id:
            return None
        for entry in self.audit.read():
            if (entry.get("state") == rulings.OPERATOR_RULING and entry.get("ruling_id") == message.ruling_id
                    and entry.get("ruling") == rulings.DELIVERED_RULING):
                return message.ruling_id
        return None

    def _audit_queue(self, event: str, dst: str, head: QueueHead, message: Message, reason: str,
                     ruling_id: Optional[str]) -> None:
        entry: Dict[str, Any] = {"state": event, "dst": dst, "msg_id": message.msg_id, "queue_seq": head.queue_seq,
                                 "head_state": message.state, "session": self.session, "detail": reason}
        if ruling_id:
            entry["ruling_id"] = ruling_id
        self.audit.record(entry)
