"""单条消息投递引擎(阶段 5b;协议见 herdr/claude/08-protocol.md §3–§6)。

DeliveryEngine.deliver(msg_id) 把一条处于 QUEUED / WAITING_TARGET / RETRYING 的队列头消息
推进到下面之一,然后返回:
  * DELIVERED(evidence = accepted_and_observed)
  * DELIVERY_UNCERTAIN(调用 herdr 之后结果不明;默认不自动重发,等操作员裁定)
  * TARGET_BLOCKED / TARGET_MISSING / TIMEOUT / FAILED(确定失败)
已处于 DELIVERY_UNCERTAIN 或终态的消息原样返回,不做任何事。
它只处理一条消息,不负责队列头的选择、暂停与放行(那是 broker,阶段 5c)。

流程(每一步都写审计):
  1. 找目标:注册表 dst -> 登记的 agent 名字(没有名字时用 pane_id)。
  2. 复核目标(**进入 DISPATCHING 之前**,08 §4 规则 6):
       idle / done -> 可以投递;blocked -> TARGET_BLOCKED;不存在 -> TARGET_MISSING;
       working / unknown -> WAITING_TARGET,在 herdr 服务端等待后再复核;
       查询失败 -> 留在当前状态退避重查,连续失败或总时限到 -> TIMEOUT。
  3. 写前标记 DISPATCHING(落盘后才调用 herdr)。
  4. `agent prompt --wait --until working --until blocked`:由 herdr 观察"这次提交之后"目标是否开始处理。
       观察到 -> DELIVERED;其余按 policy.classify_prompt_result 分类(08 §5)。
       未提交 -> RETRYING,退避后回到第 2 步。
持有 Spool 锁时从不等待 herdr:Spool 的每个方法只在短的文件操作期间持锁。

限制(v1):总等待时限从本次 deliver() 开始计时,broker 重启后重新计时。
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, Iterator, Optional, Protocol

from .audit import AuditLog
from .errors import HerdrError, HerdrNotFound, HerdrTimeout
from .herdr_client import READY_OR_BLOCKED, READY_STATUSES
from .messages import (DELIVERED, DELIVERY_UNCERTAIN, DISPATCHING, FAILED, QUEUED, RETRYING, TARGET_BLOCKED,
                       TARGET_MISSING, TERMINAL_STATES, TIMEOUT, WAITING_TARGET, Message)
from .policy import (ACCEPTED, NOT_SUBMITTED, OUTCOME_TO_STATE, BrokerConfig, backoff_delays,
                     classify_prompt_result)
from .registry import AgentNotRegisteredError, Registry
from .spool import Spool

EVIDENCE_ACCEPTED_AND_OBSERVED = "accepted_and_observed"
_STARTABLE = (QUEUED, WAITING_TARGET, RETRYING)
_OBSERVED = ("working", "blocked")


class HerdrLike(Protocol):
    session: Optional[str]

    def agent_get(self, target: str) -> Dict[str, Any]: ...

    def agent_wait(self, target: str, *, until: Any = None, timeout_ms: Optional[int] = None) -> Dict[str, Any]: ...

    def agent_prompt(self, target: str, text: str, *, wait: bool = False, until: Any = None,
                     timeout_ms: Optional[int] = None) -> Dict[str, Any]: ...


class _Finished(Exception):
    """内部:消息已经迁到某个状态,deliver() 应当返回它。"""

    def __init__(self, message: Message) -> None:
        self.message = message


class DeliveryEngine:
    def __init__(
        self,
        spool: Spool,
        registry: Registry,
        audit: AuditLog,
        client: HerdrLike,
        *,
        config: Optional[BrokerConfig] = None,
        semantics_verified: bool,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.spool = spool
        self.registry = registry
        self.audit = audit
        self.client = client
        self.config = config or BrokerConfig()
        self.semantics_verified = semantics_verified
        self._sleep = sleep
        self._monotonic = monotonic

    # ------------------------------------------------------------------
    def deliver(self, msg_id: str) -> Message:
        message = self.spool.get(msg_id)
        if message.state in TERMINAL_STATES or message.state == DELIVERY_UNCERTAIN:
            return message
        if message.state == DISPATCHING:
            # 只可能是上次崩溃留下的写前标记:prompt 可能已经发出(08 §8),不能当作没发过
            return self._move(message, DELIVERY_UNCERTAIN, "发现停在 DISPATCHING 的消息(上次可能已发出)")
        if message.state not in _STARTABLE:
            raise ValueError(f"消息 {msg_id} 处于 {message.state},不能投递")
        deadline = self._monotonic() + self.config.wait_ready_timeout_s
        retry_delays = backoff_delays(self.config.query_backoff_initial_s, self.config.query_backoff_max_s)
        try:
            while True:
                message, target = self._wait_until_ready(message, deadline)
                message = self._dispatch(message, target, deadline, retry_delays)
        except _Finished as done:
            return done.message

    # ---- 第 1 步:找目标 -------------------------------------------------
    def _target_of(self, message: Message) -> "tuple[str, str]":
        """返回 (herdr 寻址用的目标, 登记的 pane_id)。目标没有登记 -> TARGET_MISSING。"""
        if self.client.session and message.session != self.client.session:
            raise _Finished(self._move(message, FAILED,
                                       f"消息属于会话 {message.session!r},broker 在 {self.client.session!r};配置错误"))
        try:
            record = self.registry.get(message.dst)
        except AgentNotRegisteredError as exc:  # 注册表读不出等其他错误向上抛:消息不动,由 broker 告警
            raise _Finished(self._move(message, TARGET_MISSING, f"投递前复核:{message.dst} 没有登记({exc})"))
        if record.lifecycle != "running":
            raise _Finished(self._move(message, TARGET_MISSING,
                                       f"投递前复核:{message.dst} 的 lifecycle 是 {record.lifecycle}"))
        return (record.agent_name or record.pane_id), record.pane_id

    # ---- 第 2 步:复核目标,直到可投递 ------------------------------------
    def _wait_until_ready(self, message: Message, deadline: float) -> "tuple[Message, str]":
        target, pane_id = self._target_of(message)
        failures = 0
        delays = backoff_delays(self.config.query_backoff_initial_s, self.config.query_backoff_max_s)
        while True:
            if self._monotonic() >= deadline:
                raise _Finished(self._move(message, TIMEOUT,
                                           f"等待 {message.dst} 可投递超过 {self.config.wait_ready_timeout_s:g} 秒"))
            try:
                agent = self.client.agent_get(target)
            except HerdrNotFound as exc:
                # 调用 prompt 之前的独立复核:prompt 还没发出,可以确定目标不存在(08 §5)
                raise _Finished(self._move(message, TARGET_MISSING, f"投递前复核:{target} 不存在({exc.code})"))
            except Exception as exc:  # 查询失败:留在当前状态,退避重查(08 §3 末段)
                failures += 1
                message = self._note(message, f"查询目标状态失败第 {failures} 次:{_describe(exc)}")
                if failures >= self.config.query_max_consecutive_failures:
                    raise _Finished(self._move(message, TIMEOUT, f"目标状态连续 {failures} 次查询失败"))
                self._sleep(min(next(delays), max(0.0, deadline - self._monotonic())))
                continue
            failures = 0
            if agent.get("pane_id") and agent["pane_id"] != pane_id:
                raise _Finished(self._move(message, TARGET_MISSING,
                                           f"投递前复核:{target} 现在位于 {agent['pane_id']},"
                                           f"与登记的 {pane_id} 不一致"))
            status = agent.get("agent_status")
            if status in READY_STATUSES:
                return message, target
            if status == "blocked":
                raise _Finished(self._move(message, TARGET_BLOCKED, "投递前复核:目标 blocked(D8:立即失败)"))
            # working / unknown:等待。unknown 不是 READY(08 §4 规则 8)
            if message.state != WAITING_TARGET:
                message = self._move(message, WAITING_TARGET, f"目标 {status},等待可投递")
            remaining_ms = int((deadline - self._monotonic()) * 1000)
            if remaining_ms <= 0:
                continue
            try:
                self.client.agent_wait(target, until=READY_OR_BLOCKED, timeout_ms=remaining_ms)
            except HerdrTimeout:
                continue  # 回到循环顶部,由总时限判 TIMEOUT
            except HerdrNotFound as exc:
                raise _Finished(self._move(message, TARGET_MISSING, f"等待期间 {target} 消失({exc.code})"))
            except Exception as exc:
                failures += 1
                message = self._note(message, f"等待目标失败第 {failures} 次:{_describe(exc)}")
                if failures >= self.config.query_max_consecutive_failures:
                    raise _Finished(self._move(message, TIMEOUT, f"目标状态连续 {failures} 次查询失败"))
                self._sleep(min(next(delays), max(0.0, deadline - self._monotonic())))
            # 无论等到什么都回到顶部再复核一次:复核放在进入 DISPATCHING 之前

    # ---- 第 3、4 步:写前标记 + 调用 herdr + 分类 --------------------------
    def _dispatch(self, message: Message, target: str, deadline: float, retry_delays: Iterator[float]) -> Message:
        message = self._move(message, DISPATCHING, f"复核通过,向 {target} 发出 prompt", attempts=message.attempts + 1)
        error: Optional[BaseException] = None
        result: Dict[str, Any] = {}
        try:
            result = self.client.agent_prompt(target, message.text, wait=True, until=list(_OBSERVED),
                                              timeout_ms=int(self.config.observe_window_s * 1000))
        except Exception as exc:  # noqa: BLE001 —— 任何异常都要分类,不能让消息停在 DISPATCHING
            # (进程被终止时消息会留在 DISPATCHING,重启后按 08 §8 转 DELIVERY_UNCERTAIN)
            error = exc
        outcome = classify_prompt_result(error, semantics_verified=self.semantics_verified)
        if outcome == ACCEPTED:
            status = result.get("agent_status")
            if status in _OBSERVED:
                raise _Finished(self._move(message, DELIVERED, f"herdr 接受,观察到目标进入 {status}",
                                           evidence=EVIDENCE_ACCEPTED_AND_OBSERVED))
            raise _Finished(self._move(message, DELIVERY_UNCERTAIN,
                                       f"herdr 接受,但没有观察到目标开始处理(状态 {status!r})"))
        state = OUTCOME_TO_STATE[outcome]
        detail = f"agent prompt 返回 {_describe(error)} -> {outcome}"
        if not self.semantics_verified and outcome != NOT_SUBMITTED:
            detail += "(当前 herdr 版本不在实测范围,按保守分类)"
        message = self._move(message, state, detail)
        if state != RETRYING:
            raise _Finished(message)
        delay = min(next(retry_delays), max(0.0, deadline - self._monotonic()))
        self._sleep(delay)
        return message  # 回到 deliver() 的循环:RETRYING -> 复核 -> ...

    # ---- 状态与审计 ------------------------------------------------------
    def _move(self, message: Message, state: str, detail: str, *, attempts: Optional[int] = None,
              evidence: Optional[str] = None) -> Message:
        updated = self.spool.update(message.msg_id, state=state, detail=detail, attempts=attempts)
        self._audit(updated, detail, evidence=evidence)
        return updated

    def _note(self, message: Message, detail: str) -> Message:
        """同一状态内更新 detail(不算迁移),也写审计。"""
        updated = self.spool.update(message.msg_id, detail=detail)
        self._audit(updated, detail)
        return updated

    def _audit(self, message: Message, detail: str, *, evidence: Optional[str] = None) -> None:
        event: Dict[str, Any] = {
            "msg_id": message.msg_id, "edge_id": message.edge_id, "src": message.src, "dst": message.dst,
            "state": message.state, "detail": detail, "session": message.session,
            "topology_revision": message.topology_revision, "queue_seq": message.queue_seq,
            "attempts": message.attempts,
        }
        if evidence:
            event["evidence"] = evidence
        self.audit.record(event)


def _describe(exc: Optional[BaseException]) -> str:
    if exc is None:
        return "成功"
    if isinstance(exc, HerdrError):
        return f"[{exc.code or type(exc).__name__}] {exc.message}"[:300]
    return f"{type(exc).__name__}: {exc}"[:300]
