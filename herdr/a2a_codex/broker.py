"""单目标串行投递引擎；完整多目标常驻 worker 与操作员裁定由后续 Broker 层提供。"""
from __future__ import annotations

import re
import logging
import secrets
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from .errors import (
    HerdrAgentBlocked,
    HerdrAgentNotReady,
    HerdrAgentPromptFailed,
    HerdrBinaryNotFound,
    HerdrError,
    HerdrNotFound,
    HerdrPromptOutcomeUnknown,
    HerdrServerNotRunning,
    HerdrUsageError,
)
from .messages import (
    DELIVERY_UNCERTAIN,
    DELIVERED,
    DISPATCHING,
    FAILED,
    QUEUED,
    RETRYING,
    TARGET_BLOCKED,
    TARGET_MISSING,
    TERMINAL_STATES,
    TIMEOUT,
    WAITING_TARGET,
    Message,
    now_iso,
)
from .registry import AgentNotRegisteredError, Registry
from .spool import Spool
from .storage import exclusive_lock

VERIFIED_ERROR_SEMANTICS_VERSION = "0.9.3"
_SAFE_AGENT_ID = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_LOG = logging.getLogger(__name__)


class PromptDisposition(str, Enum):
    ACCEPTED_CANDIDATE = "accepted_candidate"
    RETRYABLE_NOT_SUBMITTED = "retryable_not_submitted"
    TARGET_BLOCKED = "target_blocked"
    TARGET_MISSING = "target_missing"
    OUTCOME_UNCERTAIN = "outcome_uncertain"
    PERMANENT_FAILURE = "permanent_failure"


def classify_prompt_result(error: Optional[BaseException], *, herdr_version: Optional[str]) -> PromptDisposition:
    """按协议 §5 分类；实测语义只对 Herdr 0.9.3 启用，未知版本 fail-closed。"""
    if error is None:
        return PromptDisposition.ACCEPTED_CANDIDATE
    if isinstance(error, HerdrUsageError):
        return PromptDisposition.PERMANENT_FAILURE
    if isinstance(error, HerdrBinaryNotFound):
        # 子进程没有启动，请求不可能到达 Herdr。
        return PromptDisposition.RETRYABLE_NOT_SUBMITTED
    if herdr_version != VERIFIED_ERROR_SEMANTICS_VERSION:
        return PromptDisposition.OUTCOME_UNCERTAIN
    if isinstance(error, HerdrAgentBlocked):
        return PromptDisposition.TARGET_BLOCKED
    if isinstance(error, HerdrAgentNotReady) or isinstance(error, HerdrServerNotRunning):
        return PromptDisposition.RETRYABLE_NOT_SUBMITTED
    if isinstance(error, HerdrNotFound) and getattr(error, "code", None) == "agent_not_found":
        return PromptDisposition.TARGET_MISSING
    if isinstance(error, (HerdrAgentPromptFailed, HerdrPromptOutcomeUnknown)):
        return PromptDisposition.OUTCOME_UNCERTAIN
    return PromptDisposition.OUTCOME_UNCERTAIN


@dataclass(frozen=True)
class DeliveryResult:
    msg_id: str
    dst: str
    state: str
    detail: str = ""


class DeliveryBroker:
    """以目标级锁串行处理队列头，不在 Spool/Audit 文件锁内等待 Herdr。"""

    READY = frozenset({"idle", "done"})

    def __init__(self, herdr, registry: Registry, spool: Spool, audit, *,
                 herdr_version: Optional[str] = None,
                 ready_timeout_s: float = 300.0, observation_window_s: float = 30.0,
                 state_poll_s: float = 1.0, max_state_failures: int = 5,
                 retry_backoff_s: float = 1.0, retry_backoff_max_s: float = 30.0,
                 sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        if ready_timeout_s <= 0 or observation_window_s < 0 or state_poll_s <= 0:
            raise ValueError("超时和轮询参数必须为正数(观察窗口可为 0)")
        if max_state_failures < 1 or retry_backoff_s <= 0 or retry_backoff_max_s < retry_backoff_s:
            raise ValueError("失败次数和重试退避参数非法")
        self.herdr = herdr
        self.registry = registry
        self.spool = spool
        self.audit = audit
        self.herdr_version = herdr_version
        self.ready_timeout_s = ready_timeout_s
        self.observation_window_s = observation_window_s
        self.state_poll_s = state_poll_s
        self.max_state_failures = max_state_failures
        self.retry_backoff_s = retry_backoff_s
        self.retry_backoff_max_s = retry_backoff_max_s
        self._sleep = sleep
        self._monotonic = monotonic
        self.lock_dir = spool.root / "broker-locks"
        self.dispatch_guard: Callable[[], bool] = lambda: True
        self.stop_requested: Callable[[], bool] = lambda: False
        self.wait_for_stop: Callable[[float], bool] = lambda delay: (self._sleep(delay), False)[1]

    def process_target(self, dst: str) -> Optional[DeliveryResult]:
        """串行处理 dst 的队列头；返回 None 表示无待处理槽位。"""
        if not isinstance(dst, str) or not _SAFE_AGENT_ID.fullmatch(dst):
            raise ValueError("目标 agent_id 格式非法")
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.lock_dir / (dst + ".lock")
        with exclusive_lock(lock_path):
            head = self.spool.queue_head(dst)
            if head is None:
                return None
            if not self.dispatch_guard():
                return DeliveryResult(head.msg_id, dst, head.state, "全局派发已停止")
            if head.state == DISPATCHING:
                # 上次进程可能在 Herdr 接收 prompt 后、提交结果前崩溃；绝不重发。
                head = self._transition(head, DELIVERY_UNCERTAIN,
                                        "Broker 重启时发现 DISPATCHING，投递结果不确定")
            if head.state == DELIVERY_UNCERTAIN or (head.state in TERMINAL_STATES and head.state != DELIVERED):
                self._pause(head)
                return DeliveryResult(head.msg_id, dst, head.state, head.detail)
            return self._deliver(head)

    def recover_startup(self) -> None:
        """恢复 Spool，并将所有崩溃遗留 DISPATCHING 消息 fail-closed。

        必须在 Broker Runtime 开始调度 worker 前调用。Ruling 对账在操作员裁定
        子阶段接入；本方法目前只执行协议 §8 的 Spool 与 DISPATCHING 恢复步骤。
        """
        self.spool.recover()
        for message in self.spool.pending():
            if message.state == DISPATCHING:
                self._transition(message, DELIVERY_UNCERTAIN,
                                 "Broker 启动恢复发现 DISPATCHING，prompt 可能已经送达")
        from .rulings import RulingManager
        RulingManager(self).recover_pending()

    def resolve(self, msg_id: str, action: str, *, actor: str, reason: str,
                ruling_id: Optional[str] = None,
                retry_msg_id: Optional[str] = None):
        """应用协议 §7.3 操作员裁定；命令行层应提供 actor 与 reason。"""
        from .rulings import RulingManager
        return RulingManager(self).resolve(msg_id, action, actor=actor, reason=reason,
                                           ruling_id=ruling_id, retry_msg_id=retry_msg_id)

    def void_ruling(self, ruling_id: str, *, actor: str, reason: str, verified_effects):
        """停止补做一条未完成裁定；不撤销已持久化副作用。"""
        from .rulings import RulingManager
        return RulingManager(self).void(ruling_id, actor=actor, reason=reason,
                                        verified_effects=verified_effects)

    def _deliver(self, message: Message) -> DeliveryResult:
        deadline = self._monotonic() + self.ready_timeout_s
        state_failures = 0
        retry_attempt = 0
        last_query_error = ""

        while True:
            if self.stop_requested():
                current = self.spool.get(message.msg_id)
                return DeliveryResult(current.msg_id, current.dst, current.state,
                                      "Broker 正在优雅停机，保留未投递消息")
            if self._monotonic() >= deadline:
                current = self.spool.get(message.msg_id)
                updated = self._transition(current, TIMEOUT, "等待目标 READY 或重试超过总等待时限")
                self._pause(updated)
                return DeliveryResult(updated.msg_id, updated.dst, updated.state, updated.detail)

            try:
                agent = self._get_agent(message)
                status = agent.status or "unknown"
                state_failures = 0
                last_query_error = ""
            except AgentNotRegisteredError as exc:
                return self._finish(message, TARGET_MISSING, f"目标未登记: {exc}")
            except HerdrNotFound as exc:
                return self._finish(message, TARGET_MISSING, f"目标不存在: {exc}")
            except HerdrError as exc:
                state_failures += 1
                last_query_error = str(exc)
                current = self.spool.get(message.msg_id)
                self.spool.update(current.msg_id, detail=f"目标状态查询失败: {exc}",
                                  attempts=current.attempts + 1)
                if state_failures >= self.max_state_failures:
                    updated = self._transition(current, TIMEOUT,
                                               f"目标状态连续查询失败 {state_failures} 次: {last_query_error}")
                    self._pause(updated)
                    return DeliveryResult(updated.msg_id, updated.dst, updated.state, updated.detail)
                if self.wait_for_stop(min(self.state_poll_s * (2 ** (state_failures - 1)), 30.0)):
                    current = self.spool.get(message.msg_id)
                    return DeliveryResult(current.msg_id, current.dst, current.state,
                                          "Broker 正在优雅停机，保留未投递消息")
                continue

            if status == "blocked":
                return self._finish(message, TARGET_BLOCKED, "目标状态为 blocked")
            if status not in self.READY:
                current = self.spool.get(message.msg_id)
                if current.state in {QUEUED, RETRYING}:
                    self._transition(current, WAITING_TARGET, f"目标状态为 {status}，等待 idle/done")
                if self.wait_for_stop(min(self.state_poll_s, max(0.0, deadline - self._monotonic()))):
                    current = self.spool.get(message.msg_id)
                    return DeliveryResult(current.msg_id, current.dst, current.state,
                                          "Broker 正在优雅停机，保留未投递消息")
                continue

            # 再次复核 READY，复核失败时绝不先写 DISPATCHING。
            try:
                confirmed = self._get_agent(message)
            except (AgentNotRegisteredError, HerdrNotFound) as exc:
                return self._finish(message, TARGET_MISSING, f"进入 DISPATCHING 前目标消失: {exc}")
            except HerdrError as exc:
                state_failures += 1
                current = self.spool.get(message.msg_id)
                self.spool.update(current.msg_id, detail=f"DISPATCHING 前复核失败: {exc}",
                                  attempts=current.attempts + 1)
                if state_failures >= self.max_state_failures:
                    updated = self._transition(current, TIMEOUT,
                                               f"目标 READY 复核连续失败 {state_failures} 次")
                    self._pause(updated)
                    return DeliveryResult(updated.msg_id, updated.dst, updated.state, updated.detail)
                if self.wait_for_stop(min(self.state_poll_s * (2 ** (state_failures - 1)), 30.0)):
                    current = self.spool.get(message.msg_id)
                    return DeliveryResult(current.msg_id, current.dst, current.state,
                                          "Broker 正在优雅停机，保留未投递消息")
                continue
            if confirmed.status not in self.READY:
                continue

            # 其他目标触发全局 fail-closed 后，不再开始新的 prompt 调用。
            if not self.dispatch_guard():
                current = self.spool.get(message.msg_id)
                return DeliveryResult(current.msg_id, current.dst, current.state,
                                      "全局派发已停止，prompt 尚未调用")

            pane_id = self._pane_id(message)
            current = self.spool.get(message.msg_id)
            if current.state != DISPATCHING:
                current = self._transition(current, DISPATCHING, "目标复核为 READY，准备调用 agent prompt",
                                           attempts=current.attempts + 1)
            if self.stop_requested():
                current = self._transition(self.spool.get(message.msg_id), RETRYING,
                                           "Broker 在 prompt 提交前优雅停机；已确认未调用 Herdr")
                return DeliveryResult(current.msg_id, current.dst, current.state, current.detail)
            try:
                self.herdr.send_prompt(pane_id, message.text, wait=False)
                disposition = classify_prompt_result(None, herdr_version=self.herdr_version)
            except Exception as exc:
                disposition = classify_prompt_result(exc, herdr_version=self.herdr_version)
                if disposition == PromptDisposition.RETRYABLE_NOT_SUBMITTED:
                    current = self._transition(self.spool.get(message.msg_id), RETRYING,
                                               f"Herdr 证明未提交，准备退避重试: {exc}")
                    retry_attempt += 1
                    delay = min(self.retry_backoff_s * (2 ** (retry_attempt - 1)),
                                self.retry_backoff_max_s, max(0.0, deadline - self._monotonic()))
                    if self.wait_for_stop(delay):
                        current = self.spool.get(message.msg_id)
                        return DeliveryResult(current.msg_id, current.dst, current.state,
                                              "Broker 正在优雅停机，保留重试消息")
                    continue
                if disposition == PromptDisposition.TARGET_BLOCKED:
                    return self._finish(message, TARGET_BLOCKED, f"Herdr 拒绝: {exc}")
                if disposition == PromptDisposition.TARGET_MISSING:
                    return self._finish(message, TARGET_MISSING, f"Herdr 未找到目标: {exc}")
                if disposition == PromptDisposition.PERMANENT_FAILURE:
                    return self._finish(message, FAILED, f"Herdr 配置/用法错误: {exc}")
                return self._finish(message, DELIVERY_UNCERTAIN, f"prompt 结果不确定: {exc}")

            if disposition != PromptDisposition.ACCEPTED_CANDIDATE:
                return self._finish(message, DELIVERY_UNCERTAIN, "Herdr prompt 返回未识别的结果")
            return self._observe(message)

    def _observe(self, message: Message) -> DeliveryResult:
        deadline = self._monotonic() + self.observation_window_s
        while True:
            # Prompt 已被 Herdr 接受后，必须完成这个有界观察窗口；否则常规停机
            # 会把很快开始处理的消息误判为不确定并暂停队列。窗口有硬上限。
            try:
                agent = self._get_agent(message)
            except Exception as exc:
                return self._finish(message, DELIVERY_UNCERTAIN,
                                    f"Herdr 接受 prompt 后无法确认目标开始处理: {exc}")
            status = agent.status or "unknown"
            if status == "working":
                return self._finish(message, DELIVERED, "观察到目标进入 working",
                                    evidence="accepted_and_observed")
            if status == "blocked":
                self.audit.record({"event": "DELIVERY_UNCERTAIN", "msg_id": message.msg_id,
                                   "dst": message.dst, "queue_seq": message.queue_seq,
                                   "state": DELIVERY_UNCERTAIN,
                                   "detail": "prompt 已提交后观察到 blocked；不据此判定未送达"})
                return self._finish(message, DELIVERY_UNCERTAIN,
                                    "prompt 已提交后观察到 blocked，结果仍不确定")
            if self._monotonic() >= deadline:
                return self._finish(message, DELIVERY_UNCERTAIN,
                                    "Herdr 接受 prompt，但观察窗口内未观察到 working")
            self._sleep(min(self.state_poll_s, max(0.0, deadline - self._monotonic())))

    def _get_agent(self, message: Message):
        record = self._target_record(message)
        agent = self.herdr.get_agent(record.agent_name)
        if agent.pane_id != record.pane_id:
            raise AgentNotRegisteredError(
                f"登记 agent {record.agent_name} 当前映射到 pane {agent.pane_id}，"
                f"预期 {record.pane_id}"
            )
        return agent

    def _target_record(self, message: Message):
        record = self.registry.get(message.dst)
        if ((record.agent_id, record.project_id, record.ip_id, record.session) !=
                (message.dst, message.project_id, message.ip_id, message.session)):
            raise AgentNotRegisteredError("目标 Registry 身份或 session 与入队快照不匹配")
        if record.lifecycle != "running":
            raise AgentNotRegisteredError(f"目标 lifecycle={record.lifecycle}")
        return record

    def _pane_id(self, message: Message) -> str:
        # 按稳定登记名称投递，避免 pane 被其他 agent 复用时把消息发错对象。
        return self._target_record(message).agent_name

    def _transition(self, current: Message, state: str, detail: str, *,
                    attempts: Optional[int] = None, evidence: Optional[str] = None,
                    ruling_id: Optional[str] = None) -> Message:
        event = {"event": "STATE_TRANSITION", "msg_id": current.msg_id, "edge_id": current.edge_id,
                 "src": current.src, "dst": current.dst, "queue_seq": current.queue_seq,
                 "session": current.session, "topology_revision": current.topology_revision,
                 "previous_state": current.state, "state": state, "detail": detail}
        if evidence is not None:
            event["evidence"] = evidence
        if ruling_id is not None:
            event["ruling_id"] = ruling_id
        events = self.audit.read(strict=True)
        latest = next((item for item in reversed(events)
                       if item.get("event") == "STATE_TRANSITION"
                       and item.get("msg_id") == current.msg_id), None)
        pending_replay = (latest is not None
                          and latest.get("previous_state") == current.state
                          and latest.get("state") == state
                          and latest.get("detail") == detail
                          and latest.get("ruling_id") == ruling_id)
        if pending_replay:
            transition_id = latest.get("transition_id") or secrets.token_hex(16)
        else:
            transition_id = secrets.token_hex(16)
        event["transition_id"] = transition_id
        # 只复用紧邻的未完成 WAL 意图；历史上相同状态/文案的不同投递周期必须分别审计。
        if not pending_replay:
            self.audit.record(event)
        return self.spool.update(current.msg_id, state=state, detail=detail, attempts=attempts,
                                 ruling_id=ruling_id, transition_id=transition_id)

    def _finish(self, message: Message, state: str, detail: str, *,
                evidence: Optional[str] = None) -> DeliveryResult:
        current = self.spool.get(message.msg_id)
        if current.state != state:
            current = self._transition(current, state, detail, evidence=evidence)
        if state == DELIVERED or state in {
            DELIVERY_UNCERTAIN, TARGET_BLOCKED, TARGET_MISSING, TIMEOUT, FAILED
        }:
            if state != DELIVERED:
                self._pause(current)
        return DeliveryResult(current.msg_id, current.dst, current.state, current.detail)

    def _pause(self, message: Message) -> None:
        events = self.audit.read(strict=True)
        pause_key = message.transition_id or next((event.get("ts") for event in reversed(events)
                                                   if event.get("event") == "STATE_TRANSITION"
                                                   and event.get("msg_id") == message.msg_id
                                                   and event.get("state") == message.state),
                                                  message.updated_at)
        if any(event.get("event") == "QUEUE_PAUSED" and event.get("msg_id") == message.msg_id
               and event.get("pause_key") == pause_key for event in events):
            return
        self.audit.record({"event": "QUEUE_PAUSED", "msg_id": message.msg_id,
                           "dst": message.dst, "queue_seq": message.queue_seq,
                           "state": message.state, "pause_key": pause_key,
                           "transition_id": message.transition_id,
                           "detail": f"队列头 {message.state}，等待操作员处理"})
        _LOG.warning("A2A 队列暂停: dst=%s msg_id=%s queue_seq=%s state=%s; 需操作员处理",
                     message.dst, message.msg_id, message.queue_seq, message.state)
