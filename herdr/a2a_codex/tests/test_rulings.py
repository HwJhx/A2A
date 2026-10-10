from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from a2a_codex.audit import AuditLog
from a2a_codex.broker import DeliveryBroker
from a2a_codex.messages import (
    DELIVERY_UNCERTAIN,
    DELIVERED,
    DISPATCHING,
    FAILED,
    QUEUED,
    RETRYING,
    TARGET_BLOCKED,
    Message,
    new_msg_id,
    now_iso,
)
from a2a_codex.rulings import RulingError, RulingManager
from a2a_codex.spool import Spool


class FakeRegistry:
    def get(self, _agent_id):
        return SimpleNamespace(agent_id="sw_uart", project_id="soc_a", ip_id="uart",
                               session="test1", pane_id="w1:p2", lifecycle="running")


class UnusedHerdr:
    def get_agent(self, _pane_id):
        raise AssertionError("ruling tests must not call Herdr")

    def send_prompt(self, *_args, **_kwargs):
        raise AssertionError("ruling tests must not send prompt")


class RulingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.spool = Spool(root / "spool")
        self.audit = AuditLog(root / "audit.jsonl")
        self.broker = DeliveryBroker(UnusedHerdr(), FakeRegistry(), self.spool, self.audit)
        now = now_iso()
        self.message = self.spool.enqueue(Message(
            msg_id=new_msg_id(), created_at=now, edge_id="dv_done", src="dv_uart",
            dst="sw_uart", project_id="soc_a", ip_id="uart", session="test1",
            text="uart 已验证", state=QUEUED, topology_revision="r1", updated_at=now,
        ))

    def make_uncertain(self):
        self.spool.update(self.message.msg_id, state=DISPATCHING, detail="before prompt")
        return self.spool.update(self.message.msg_id, state=DELIVERY_UNCERTAIN,
                                 detail="prompt result unknown")

    def make_failed(self):
        return self.spool.update(self.message.msg_id, state=TARGET_BLOCKED,
                                 detail="target blocked")

    def resolve(self, action, *, ruling_id="r-test-001", **kwargs):
        return self.broker.resolve(self.message.msg_id, action, actor="jhx", reason="manual check",
                                   ruling_id=ruling_id, **kwargs)

    def test_uncertain_delivered_releases_slot_with_ruling_audit(self):
        self.make_uncertain()
        self.resolve("delivered")
        self.assertEqual(self.spool.get(self.message.msg_id).state, DELIVERED)
        self.assertTrue(self.spool.slot_released("sw_uart", self.message.queue_seq))
        events = self.audit.read()
        released = next(event for event in events if event.get("event") == "QUEUE_RELEASED")
        self.assertEqual(released["ruling_id"], "r-test-001")
        self.assertEqual(sum(event.get("event") == "RULING_APPLIED" for event in events), 1)

    def test_uncertain_retry_keeps_same_message_and_slot_active(self):
        self.make_uncertain()
        self.resolve("retry")
        self.assertEqual(self.spool.get(self.message.msg_id).state, RETRYING)
        self.assertFalse(self.spool.slot_released("sw_uart", self.message.queue_seq))
        self.assertEqual(self.spool.queue_head("sw_uart").msg_id, self.message.msg_id)

    def test_uncertain_abandon_marks_failed_then_releases(self):
        self.make_uncertain()
        self.resolve("abandon")
        self.assertEqual(self.spool.get(self.message.msg_id).state, FAILED)
        self.assertTrue(self.spool.slot_released("sw_uart", self.message.queue_seq))

    def test_terminal_retry_creates_one_authorized_message_in_same_slot(self):
        self.make_failed()
        retry_id = "0000000000000002-abcdef"
        self.resolve("retry", retry_msg_id=retry_id)
        retry = self.spool.queue_head("sw_uart")
        self.assertEqual(retry.msg_id, retry_id)
        self.assertEqual(retry.retry_of, self.message.msg_id)
        self.assertEqual(retry.queue_seq, self.message.queue_seq)
        self.assertEqual(retry.text, self.message.text)
        self.assertEqual(retry.topology_revision, self.message.topology_revision)
        self.resolve("retry", retry_msg_id=retry_id)
        self.assertEqual(sum(item.retry_of == self.message.msg_id for item in self.spool.pending()), 1)

    def test_terminal_abandon_and_continue_releases_without_mutating_terminal(self):
        self.make_failed()
        self.resolve("abandon_and_continue")
        self.assertEqual(self.spool.get(self.message.msg_id).state, TARGET_BLOCKED)
        self.assertTrue(self.spool.slot_released("sw_uart", self.message.queue_seq))

    def test_audit_failure_before_ruling_has_no_side_effect(self):
        self.make_uncertain()
        original_record = self.audit.record

        def fail_ruling(event):
            if event.get("event") == "OPERATOR_RULING":
                raise OSError("audit unavailable")
            return original_record(event)

        self.audit.record = fail_ruling
        with self.assertRaisesRegex(OSError, "audit unavailable"):
            self.resolve("abandon")
        self.assertEqual(self.spool.get(self.message.msg_id).state, DELIVERY_UNCERTAIN)
        self.assertFalse(self.spool.slot_released("sw_uart", self.message.queue_seq))

    def test_recovery_completes_crash_after_release_before_applied_idempotently(self):
        self.make_uncertain()
        original_record = self.audit.record
        failed_once = {"value": False}

        def fail_after_effects(event):
            if event.get("event") == "RULING_APPLIED" and not failed_once["value"]:
                failed_once["value"] = True
                raise OSError("simulated crash before applied marker")
            return original_record(event)

        self.audit.record = fail_after_effects
        with self.assertRaisesRegex(OSError, "simulated crash"):
            self.resolve("abandon", ruling_id="r-crash")
        self.audit.record = original_record

        manager = RulingManager(self.broker)
        self.assertEqual(manager.recover_pending(), 1)
        self.assertEqual(manager.recover_pending(), 0)
        events = self.audit.read()
        self.assertEqual(sum(event.get("event") == "RULING_APPLIED"
                             and event.get("ruling_id") == "r-crash" for event in events), 1)
        self.assertEqual(self.spool.get(self.message.msg_id).state, FAILED)
        self.assertTrue(self.spool.slot_released("sw_uart", self.message.queue_seq))

    def test_void_stops_pending_replay_without_reverting_persisted_effects(self):
        self.make_uncertain()
        ruling = self.audit.record({"event": "OPERATOR_RULING", "ruling_id": "r-void",
                                    "actor": "jhx", "ruling": "abandon",
                                    "reason": "manual", "msg_id": self.message.msg_id,
                                    "dst": self.message.dst, "queue_seq": self.message.queue_seq,
                                    "previous_state": DELIVERY_UNCERTAIN,
                                    "new_state": FAILED})
        self.broker._transition(self.spool.get(self.message.msg_id), FAILED,
                                "部分生效", ruling_id="r-void")
        manager = RulingManager(self.broker)
        manager.void("r-void", actor="jhx", reason="state checked",
                     verified_effects={"state": FAILED, "slot_released": False})
        self.assertEqual(manager.recover_pending(), 0)
        self.assertEqual(self.spool.get(self.message.msg_id).state, FAILED)
        self.assertFalse(self.spool.slot_released("sw_uart", self.message.queue_seq))
        self.assertTrue(any(event.get("event") == "RULING_VOIDED"
                            and event.get("voided_ruling_id") == ruling["ruling_id"]
                            for event in self.audit.read()))

    def test_applied_ruling_cannot_be_voided(self):
        self.make_uncertain()
        self.resolve("abandon", ruling_id="r-applied")
        with self.assertRaisesRegex(RulingError, "不可作废"):
            self.broker.void_ruling("r-applied", actor="jhx", reason="too late",
                                    verified_effects={"slot_released": True})

    def test_only_current_queue_head_can_be_ruled(self):
        self.make_uncertain()
        later = self.spool.enqueue(Message(
            msg_id=new_msg_id(), created_at=now_iso(), edge_id="dv_done", src="dv_uart",
            dst="sw_uart", project_id="soc_a", ip_id="uart", session="test1",
            text="later", state=QUEUED, topology_revision="r1", updated_at=now_iso(),
        ))
        with self.assertRaisesRegex(RulingError, "当前目标队列头"):
            self.broker.resolve(later.msg_id, "abandon_and_continue", actor="jhx",
                                reason="not head", ruling_id="r-not-head")
        self.assertEqual(self.spool.get(later.msg_id).state, QUEUED)


if __name__ == "__main__":
    unittest.main()
