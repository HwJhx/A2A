from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from a2a_codex.audit import AuditLog
from a2a_codex.broker import DeliveryBroker, PromptDisposition, classify_prompt_result
from a2a_codex.errors import (
    HerdrAgentBlocked,
    HerdrAgentNotReady,
    HerdrAgentPromptFailed,
    HerdrError,
    HerdrNotFound,
    HerdrPromptOutcomeUnknown,
    HerdrServerNotRunning,
    HerdrUsageError,
)
from a2a_codex.messages import (
    DELIVERY_UNCERTAIN,
    DELIVERED,
    FAILED,
    QUEUED,
    TARGET_BLOCKED,
    TIMEOUT,
    Message,
    now_iso,
    new_msg_id,
)
from a2a_codex.models import Agent
from a2a_codex.spool import Spool


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class FakeRegistry:
    def get(self, _agent_id: str):
        return SimpleNamespace(agent_id="sw_uart", project_id="soc_a", ip_id="uart",
                               session="test1", pane_id="w1:p2", lifecycle="running")


class FakeHerdr:
    def __init__(self, *, status="idle", prompt_errors=(), status_errors=(), on_prompt=None) -> None:
        self.status = status
        self.prompt_errors = list(prompt_errors)
        self.status_errors = list(status_errors)
        self.on_prompt = on_prompt
        self.prompt_calls = []

    def get_agent(self, pane_id):
        if self.status_errors:
            raise self.status_errors.pop(0)
        return Agent(raw={"pane_id": pane_id, "agent_status": self.status},
                     pane_id=pane_id, status=self.status)

    def send_prompt(self, pane_id, prompt, *, wait=False):
        self.prompt_calls.append((pane_id, prompt, wait))
        if self.prompt_errors:
            error = self.prompt_errors.pop(0)
            if error:
                raise error
        if self.on_prompt:
            self.on_prompt(self)
        return Agent(raw={"pane_id": pane_id, "agent_status": self.status},
                     pane_id=pane_id, status=self.status)


class BrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.spool = Spool(root / "spool")
        self.audit = AuditLog(root / "audit.jsonl")
        now = now_iso()
        self.message = self.spool.enqueue(Message(
            msg_id=new_msg_id(), created_at=now, edge_id="dv_done", src="dv_uart",
            dst="sw_uart", project_id="soc_a", ip_id="uart", session="test1",
            text="uart 已验证", state=QUEUED, topology_revision="r1", updated_at=now,
        ))
        self.clock = Clock()

    def broker(self, herdr, *, version="0.9.3", **kwargs):
        return DeliveryBroker(herdr, FakeRegistry(), self.spool, self.audit,
                              herdr_version=version, sleep=self.clock.sleep,
                              monotonic=self.clock.monotonic, state_poll_s=1,
                              observation_window_s=2, ready_timeout_s=10, **kwargs)

    def test_prompt_error_classifier_matches_probed_herdr_093(self):
        cases = [
            (HerdrAgentBlocked("blocked", code="agent_blocked"), PromptDisposition.TARGET_BLOCKED),
            (HerdrAgentNotReady("not ready", code="agent_not_ready"),
             PromptDisposition.RETRYABLE_NOT_SUBMITTED),
            (HerdrServerNotRunning("stopped", code="server_not_running"),
             PromptDisposition.RETRYABLE_NOT_SUBMITTED),
            (HerdrNotFound("missing", code="agent_not_found"), PromptDisposition.TARGET_MISSING),
            (HerdrAgentPromptFailed("write interrupted", code="agent_prompt_failed"),
             PromptDisposition.OUTCOME_UNCERTAIN),
            (HerdrPromptOutcomeUnknown("stalled", code="agent_prompt_stalled"),
             PromptDisposition.OUTCOME_UNCERTAIN),
            (HerdrUsageError("bad invocation", code="usage"), PromptDisposition.PERMANENT_FAILURE),
            (HerdrError("new code", code="future_error"), PromptDisposition.OUTCOME_UNCERTAIN),
        ]
        for error, expected in cases:
            with self.subTest(code=error.code):
                self.assertEqual(classify_prompt_result(error, herdr_version="0.9.3"), expected)

    def test_probed_error_names_are_uncertain_on_unverified_herdr_version(self):
        error = HerdrAgentBlocked("blocked", code="agent_blocked")
        self.assertEqual(classify_prompt_result(error, herdr_version="0.9.4"),
                         PromptDisposition.OUTCOME_UNCERTAIN)
        self.assertEqual(classify_prompt_result(error, herdr_version=None),
                         PromptDisposition.OUTCOME_UNCERTAIN)

    def test_success_requires_observing_working_before_delivered(self):
        herdr = FakeHerdr(on_prompt=lambda client: setattr(client, "status", "working"))
        result = self.broker(herdr).process_target("sw_uart")
        self.assertEqual(result.state, DELIVERED)
        self.assertEqual(herdr.prompt_calls, [("w1:p2", "uart 已验证", False)])
        self.assertTrue(self.spool.slot_released("sw_uart", self.message.queue_seq))
        events = self.audit.read()
        delivered = next(event for event in events if event.get("state") == DELIVERED)
        self.assertEqual(delivered["evidence"], "accepted_and_observed")

    def test_accepted_prompt_without_observed_working_becomes_uncertain(self):
        herdr = FakeHerdr(status="idle")
        result = self.broker(herdr).process_target("sw_uart")
        self.assertEqual(result.state, DELIVERY_UNCERTAIN)
        self.assertEqual(len(herdr.prompt_calls), 1)
        self.assertFalse(self.spool.slot_released("sw_uart", self.message.queue_seq))

    def test_paused_queue_head_emits_warning_and_queue_paused_audit(self):
        herdr = FakeHerdr(status="idle")
        broker = self.broker(herdr)
        result = broker.process_target("sw_uart")
        self.assertEqual(result.state, DELIVERY_UNCERTAIN)

        # Processing the newly uncertain queue head pauses it and emits the
        # operator-facing warning/audit without releasing its queue slot.
        with self.assertLogs("a2a_codex.broker", level="WARNING") as captured:
            paused_result = broker.process_target("sw_uart")

        self.assertEqual(paused_result.state, DELIVERY_UNCERTAIN)
        self.assertTrue(any("A2A 队列暂停" in line for line in captured.output))
        paused = [event for event in self.audit.read()
                  if event.get("event") == "QUEUE_PAUSED"
                  and event.get("msg_id") == self.message.msg_id]
        self.assertEqual(len(paused), 1)
        self.assertEqual(paused[0]["state"], DELIVERY_UNCERTAIN)
        self.assertIn("pause_key", paused[0])
        self.assertFalse(self.spool.slot_released("sw_uart", self.message.queue_seq))

    def test_blocked_target_never_receives_prompt_and_pauses_slot(self):
        herdr = FakeHerdr(status="blocked")
        result = self.broker(herdr).process_target("sw_uart")
        self.assertEqual(result.state, TARGET_BLOCKED)
        self.assertEqual(herdr.prompt_calls, [])
        self.assertEqual(self.spool.queue_head("sw_uart").state, TARGET_BLOCKED)
        self.assertFalse(self.spool.slot_released("sw_uart", self.message.queue_seq))

    def test_prompt_failed_write_is_uncertain_and_never_retried(self):
        error = HerdrAgentPromptFailed("PTY actor closed during input submission",
                                       code="agent_prompt_failed")
        herdr = FakeHerdr(prompt_errors=[error])
        result = self.broker(herdr).process_target("sw_uart")
        self.assertEqual(result.state, DELIVERY_UNCERTAIN)
        self.assertEqual(len(herdr.prompt_calls), 1)
        self.assertFalse(self.spool.slot_released("sw_uart", self.message.queue_seq))

    def test_restart_recovers_dispatching_as_uncertain_without_resending(self):
        self.spool.update(self.message.msg_id, state="DISPATCHING",
                          detail="crash window before result persistence")
        herdr_after_restart = FakeHerdr(status="idle")
        restarted_broker = self.broker(herdr_after_restart)
        restarted_broker.recover_startup()
        result = restarted_broker.process_target("sw_uart")
        self.assertEqual(result.state, DELIVERY_UNCERTAIN)
        self.assertEqual(herdr_after_restart.prompt_calls, [])
        self.assertEqual(self.spool.get(self.message.msg_id).state, DELIVERY_UNCERTAIN)

    def test_proven_not_submitted_error_retries_after_backoff(self):
        error = HerdrServerNotRunning("offline", code="server_not_running")
        herdr = FakeHerdr(prompt_errors=[error, None],
                          on_prompt=lambda client: setattr(client, "status", "working"))
        result = self.broker(herdr).process_target("sw_uart")
        self.assertEqual(result.state, DELIVERED)
        self.assertEqual(len(herdr.prompt_calls), 2)
        self.assertGreaterEqual(self.clock.value, 1.0)

    def test_status_query_error_limit_transitions_to_timeout(self):
        herdr = FakeHerdr(status_errors=[HerdrError("query failed") for _ in range(5)])
        result = self.broker(herdr, max_state_failures=5).process_target("sw_uart")
        self.assertEqual(result.state, TIMEOUT)
        self.assertEqual(self.spool.queue_head("sw_uart").state, TIMEOUT)

    def test_permanent_prompt_configuration_error_fails_and_holds_slot(self):
        herdr = FakeHerdr(prompt_errors=[HerdrUsageError("bad invocation", code="usage")])
        result = self.broker(herdr).process_target("sw_uart")
        self.assertEqual(result.state, FAILED)
        self.assertFalse(self.spool.slot_released("sw_uart", self.message.queue_seq))


if __name__ == "__main__":
    unittest.main()
