"""阶段 5b:单条消息投递引擎,用假 HerdrClient 覆盖 08-protocol.md §3 的每一条迁移。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, List

from a2a.audit import AuditLog
from a2a.delivery import DeliveryEngine
from a2a.errors import (HerdrAgentBlocked, HerdrAgentNotReady, HerdrError, HerdrNotFound, HerdrPromptFailed,
                        HerdrPromptStalled, HerdrServerNotRunning, HerdrTimeout, HerdrUsageError)
from a2a.identity import AgentIdentity
from a2a.messages import (ALLOWED_TRANSITIONS, DELIVERED, DELIVERY_UNCERTAIN, DISPATCHING, FAILED, QUEUED,
                          RETRYING, TARGET_BLOCKED, TARGET_MISSING, TIMEOUT, WAITING_TARGET, Message, new_msg_id,
                          now_iso)
from a2a.policy import BrokerConfig
from a2a.registry import Registry
from a2a.spool import Spool

SESSION = "walk1"


def agent(status, pane="w1:p2"):
    return {"agent_status": status, "pane_id": pane, "name": "sw_uart"}


def e(cls, code):
    return cls("x", code=code)


class FakeHerdr:
    """按脚本返回结果。每个列表元素是 dict(返回)或异常(抛出)。列表用完后重复最后一个。"""

    def __init__(self, gets=(), waits=(), prompts=(), clock=None):
        self.session = SESSION
        self.gets, self.waits, self.prompts = list(gets), list(waits), list(prompts)
        self.calls: List[Any] = []
        self.clock = clock

    def _next(self, seq, name, *args, **kw):
        self.calls.append((name, args, kw))
        if not seq:
            raise AssertionError(f"没有为 {name} 准备结果")
        item = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(*args, **kw)
        return item

    def agent_get(self, target):
        return self._next(self.gets, "get", target)

    def agent_wait(self, target, *, until=None, timeout_ms=None):
        if self.clock is not None and timeout_ms:
            self.clock.advance(min(timeout_ms / 1000.0, 10.0))
        return self._next(self.waits, "wait", target, until=until, timeout_ms=timeout_ms)

    def agent_prompt(self, target, text, *, wait=False, until=None, timeout_ms=None):
        return self._next(self.prompts, "prompt", target, text, wait=wait, until=until, timeout_ms=timeout_ms)

    def count(self, name):
        return sum(1 for c in self.calls if c[0] == name)


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.slept: List[float] = []

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class Base(unittest.TestCase):
    verified = True

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.spool = Spool(root / "spool")
        self.registry = Registry(root / "registry.json")
        self.audit = AuditLog(root / "audit.jsonl")
        self.registry.register(AgentIdentity("soc_a", "uart", "sw", "sw_uart"), session=SESSION,
                               workspace_id="w1", tab_id="w1:t2", pane_id="w1:p2")
        self.clock = Clock()
        self.msg = self.spool.enqueue(Message(
            msg_id=new_msg_id(), created_at=now_iso(), edge_id="dv_done", src="dv_uart", dst="sw_uart",
            project_id="soc_a", ip_id="uart", session=SESSION, text="uart 验证完成", state=QUEUED,
            topology_revision="rev1"))

    def run_engine(self, herdr, config=None):
        herdr.clock = self.clock
        engine = DeliveryEngine(self.spool, self.registry, self.audit, herdr, config=config,
                                semantics_verified=self.verified, sleep=self.clock.sleep,
                                monotonic=self.clock.monotonic)
        return engine.deliver(self.msg.msg_id)

    def states(self):
        return [ev["state"] for ev in self.audit.read() if ev.get("msg_id") == self.msg.msg_id]

    def assert_path_is_legal(self):
        path = [QUEUED] + [s for s in self.states() if s != QUEUED]
        compressed = [s for i, s in enumerate(path) if i == 0 or s != path[i - 1]]
        for old, new in zip(compressed, compressed[1:]):
            self.assertIn(new, ALLOWED_TRANSITIONS[old], f"{old} -> {new}")


class HappyPath(Base):
    def test_idle_target_delivered_with_observed_evidence(self):
        herdr = FakeHerdr(gets=[agent("idle")], prompts=[agent("working")])
        result = self.run_engine(herdr)
        self.assertEqual(result.state, DELIVERED)
        self.assertEqual(self.states(), [DISPATCHING, DELIVERED])  # 入队事件由 Router 记录,这里直接入队
        delivered = [ev for ev in self.audit.read() if ev.get("state") == DELIVERED][0]
        self.assertEqual(delivered["evidence"], "accepted_and_observed")
        _, args, kw = [c for c in herdr.calls if c[0] == "prompt"][0]
        self.assertEqual(args, ("sw_uart", "uart 验证完成"))  # 用登记的 agent 名字寻址
        self.assertEqual((kw["wait"], kw["until"], kw["timeout_ms"]), (True, ["working", "blocked"], 30000))
        self.assertEqual(result.attempts, 1)

    def test_done_is_ready_too(self):
        self.assertEqual(self.run_engine(FakeHerdr(gets=[agent("done")], prompts=[agent("working")])).state,
                         DELIVERED)

    def test_blocked_right_after_submission_counts_as_observed(self):
        # 收到 prompt 后开始干活又停在审批:说明已经开始处理
        self.assertEqual(self.run_engine(FakeHerdr(gets=[agent("idle")], prompts=[agent("blocked")])).state,
                         DELIVERED)


class WaitingForTarget(Base):
    def test_busy_target_waits_then_rechecks_before_dispatch(self):
        herdr = FakeHerdr(gets=[agent("working"), agent("idle")], waits=[agent("idle")], prompts=[agent("working")])
        self.assertEqual(self.run_engine(herdr).state, DELIVERED)
        self.assertEqual(self.states(), [WAITING_TARGET, DISPATCHING, DELIVERED])
        self.assertEqual(herdr.count("get"), 2)  # 等到 READY 之后仍然复核一次
        self.assert_path_is_legal()

    def test_unknown_is_not_ready(self):
        herdr = FakeHerdr(gets=[agent("unknown")], waits=[HerdrTimeout("t", code="timeout")])
        result = self.run_engine(herdr, BrokerConfig(wait_ready_timeout_s=20))
        self.assertEqual(result.state, TIMEOUT)
        self.assertEqual(herdr.count("prompt"), 0)
        self.assert_path_is_legal()

    def test_target_becomes_blocked_while_waiting(self):
        herdr = FakeHerdr(gets=[agent("working"), agent("blocked")], waits=[agent("blocked")])
        self.assertEqual(self.run_engine(herdr).state, TARGET_BLOCKED)
        self.assertEqual(herdr.count("prompt"), 0)
        self.assert_path_is_legal()

    def test_target_disappears_while_waiting(self):
        herdr = FakeHerdr(gets=[agent("working")], waits=[e(HerdrNotFound, "agent_not_found")])
        self.assertEqual(self.run_engine(herdr).state, TARGET_MISSING)
        self.assert_path_is_legal()


class PreDispatchChecks(Base):
    def test_blocked_fails_immediately_without_prompt(self):
        herdr = FakeHerdr(gets=[agent("blocked")])
        self.assertEqual(self.run_engine(herdr).state, TARGET_BLOCKED)
        self.assertEqual((herdr.count("prompt"), herdr.count("wait")), (0, 0))

    def test_missing_agent_is_definite_before_any_prompt(self):
        herdr = FakeHerdr(gets=[e(HerdrNotFound, "agent_not_found")])
        self.assertEqual(self.run_engine(herdr).state, TARGET_MISSING)
        self.assertEqual(herdr.count("prompt"), 0)

    def test_unregistered_or_stopped_target_is_missing(self):
        self.registry.unregister("sw_uart")
        self.assertEqual(self.run_engine(FakeHerdr()).state, TARGET_MISSING)

    def test_agent_moved_to_another_pane_is_missing(self):
        herdr = FakeHerdr(gets=[agent("idle", pane="w1:p9")])
        result = self.run_engine(herdr)
        self.assertEqual(result.state, TARGET_MISSING)
        self.assertIn("不一致", result.detail)

    def test_other_session_is_a_config_error(self):
        herdr = FakeHerdr()
        herdr.session = "other"
        self.assertEqual(self.run_engine(herdr).state, FAILED)

    def test_query_failures_back_off_then_time_out(self):
        herdr = FakeHerdr(gets=[e(HerdrServerNotRunning, "server_not_running")])
        result = self.run_engine(herdr)
        self.assertEqual(result.state, TIMEOUT)
        self.assertEqual(herdr.count("get"), 5)                 # 连续 5 次
        self.assertEqual(self.clock.slept, [1.0, 2.0, 4.0, 8.0])  # 每次失败后退避,第 5 次直接判定
        self.assertIn("连续 5 次", result.detail)

    def test_query_failure_then_recovery_resets_the_counter(self):
        herdr = FakeHerdr(gets=[HerdrError("瞬时"), HerdrError("瞬时"), agent("idle")], prompts=[agent("working")])
        self.assertEqual(self.run_engine(herdr).state, DELIVERED)
        notes = [ev["detail"] for ev in self.audit.read() if ev.get("state") == QUEUED and "查询" in ev.get("detail", "")]
        self.assertEqual(len(notes), 2)  # 失败只记 detail,不迁移


class PromptOutcomes(Base):
    def outcome(self, prompt_result, **kw):
        herdr = FakeHerdr(gets=[agent("idle")], prompts=[prompt_result], **kw)
        return self.run_engine(herdr), herdr

    def test_prompt_failed_was_written_so_uncertain(self):
        result, _ = self.outcome(e(HerdrPromptFailed, "agent_prompt_failed"))
        self.assertEqual(result.state, DELIVERY_UNCERTAIN)

    def test_stalled_timeout_unknown_are_uncertain(self):
        for error in (e(HerdrPromptStalled, "agent_prompt_stalled"), e(HerdrTimeout, "timeout"),
                      e(HerdrTimeout, "client_timeout"), HerdrError("新错误", code="brand_new"),
                      RuntimeError("意外")):
            self.setUp()
            self.assertEqual(self.outcome(error)[0].state, DELIVERY_UNCERTAIN, error)

    def test_accepted_without_observed_start_is_uncertain(self):
        self.assertEqual(self.outcome(agent("idle"))[0].state, DELIVERY_UNCERTAIN)

    def test_verified_rejections(self):
        self.assertEqual(self.outcome(e(HerdrAgentBlocked, "agent_blocked"))[0].state, TARGET_BLOCKED)
        self.setUp()
        self.assertEqual(self.outcome(e(HerdrNotFound, "agent_not_found"))[0].state, TARGET_MISSING)

    def test_usage_error_is_failed(self):
        self.assertEqual(self.outcome(HerdrUsageError("坏参数"))[0].state, FAILED)

    def test_not_submitted_retries_after_backoff_and_rechecks(self):
        herdr = FakeHerdr(gets=[agent("idle")], prompts=[e(HerdrAgentNotReady, "agent_not_ready"), agent("working")])
        result = self.run_engine(herdr)
        self.assertEqual(result.state, DELIVERED)
        self.assertEqual(self.states(), [DISPATCHING, RETRYING, DISPATCHING, DELIVERED])
        self.assertEqual(herdr.count("get"), 2)  # RETRYING 之后先复核再发
        self.assertEqual(result.attempts, 2)
        self.assert_path_is_legal()

    def test_retrying_target_turns_blocked_or_missing(self):
        herdr = FakeHerdr(gets=[agent("idle"), agent("blocked")], prompts=[e(HerdrServerNotRunning, "server_not_running")])
        self.assertEqual(self.run_engine(herdr).state, TARGET_BLOCKED)
        self.assert_path_is_legal()
        self.setUp()
        herdr = FakeHerdr(gets=[agent("idle"), e(HerdrNotFound, "agent_not_found")],
                          prompts=[e(HerdrServerNotRunning, "server_not_running")])
        self.assertEqual(self.run_engine(herdr).state, TARGET_MISSING)
        self.assert_path_is_legal()

    def test_retrying_ends_in_timeout_not_failed(self):
        # 用户确认:不设重试次数上限,由总等待时限兜底转 TIMEOUT
        herdr = FakeHerdr(gets=[agent("idle")], prompts=[e(HerdrAgentNotReady, "agent_not_ready")])
        result = self.run_engine(herdr, BrokerConfig(wait_ready_timeout_s=60))
        self.assertEqual(result.state, TIMEOUT)
        self.assertGreater(herdr.count("prompt"), 3)
        self.assert_path_is_legal()

    def test_retrying_target_busy_goes_back_to_waiting(self):
        herdr = FakeHerdr(gets=[agent("idle"), agent("working"), agent("idle")], waits=[agent("idle")],
                          prompts=[e(HerdrAgentNotReady, "agent_not_ready"), agent("working")])
        self.assertEqual(self.run_engine(herdr).state, DELIVERED)
        self.assertIn(WAITING_TARGET, self.states())
        self.assert_path_is_legal()


class UnverifiedHerdrVersion(PromptOutcomes):
    """herdr 版本不在实测范围时,四个实测错误码按"不确定"处理(重写对应用例)。"""

    verified = False

    def test_verified_rejections(self):
        self.assertEqual(self.outcome(e(HerdrAgentBlocked, "agent_blocked"))[0].state, DELIVERY_UNCERTAIN)
        self.setUp()
        result = self.outcome(e(HerdrNotFound, "agent_not_found"))[0]
        self.assertEqual(result.state, DELIVERY_UNCERTAIN)
        self.assertIn("不在实测范围", result.detail)

    def test_not_submitted_retries_after_backoff_and_rechecks(self):
        self.assertEqual(self.outcome(e(HerdrAgentNotReady, "agent_not_ready"))[0].state, DELIVERY_UNCERTAIN)

    def test_retrying_target_turns_blocked_or_missing(self):
        self.assertEqual(self.outcome(e(HerdrServerNotRunning, "server_not_running"))[0].state, DELIVERY_UNCERTAIN)

    def test_retrying_ends_in_timeout_not_failed(self):
        pass  # 未实测版本下不会进入 RETRYING

    def test_retrying_target_busy_goes_back_to_waiting(self):
        pass


class AlreadySettled(Base):
    def test_uncertain_and_terminal_messages_are_left_alone(self):
        self.spool.update(self.msg.msg_id, state=DISPATCHING)
        self.spool.update(self.msg.msg_id, state=DELIVERY_UNCERTAIN)
        herdr = FakeHerdr()
        self.assertEqual(self.run_engine(herdr).state, DELIVERY_UNCERTAIN)
        self.assertEqual(herdr.calls, [])

    def test_leftover_dispatching_becomes_uncertain_without_calling_herdr(self):
        self.spool.update(self.msg.msg_id, state=DISPATCHING)
        herdr = FakeHerdr()
        self.assertEqual(self.run_engine(herdr).state, DELIVERY_UNCERTAIN)
        self.assertEqual(herdr.calls, [])


if __name__ == "__main__":
    unittest.main()
