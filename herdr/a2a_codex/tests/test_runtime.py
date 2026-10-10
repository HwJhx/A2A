from __future__ import annotations

import fcntl
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from a2a_codex.audit import AuditLog
from a2a_codex.broker import DeliveryResult
from a2a_codex.messages import (
    DELIVERED,
    DELIVERY_UNCERTAIN,
    DISPATCHING,
    QUEUED,
    TARGET_BLOCKED,
    Message,
    new_msg_id,
    now_iso,
)
from a2a_codex.runtime import BrokerAlreadyRunningError, BrokerRuntime, DispatchHaltedError
from a2a_codex.spool import Spool


def make_message(dst: str, *, state="QUEUED", updated_at="t1") -> Message:
    return Message(msg_id="0000000000000001-abcdef", created_at="t0", edge_id="edge",
                   src="dv_uart", dst=dst, project_id="soc_a", ip_id="uart",
                   session="test1", text="hello", state=state,
                   topology_revision="r1", updated_at=updated_at, queue_seq=1)


class FakeSpool:
    def __init__(self, root: Path, targets=()) -> None:
        self.root = root
        self.heads = {target: make_message(target) for target in targets}
        self.control = {"version": 1, "halted": False, "incident_id": None, "reason": ""}

    def queue_targets(self):
        return sorted(self.heads)

    def queue_head(self, dst):
        return self.heads.get(dst)

    def recover(self):
        return None

    def dispatch_control(self):
        return dict(self.control)

    def halt_dispatch(self, reason, *, incident_id=None):
        self.control.update({"halted": True, "incident_id": incident_id or "halt-test",
                             "reason": reason})
        return dict(self.control)

    def clear_dispatch_halt(self, incident_id):
        if self.control.get("incident_id") != incident_id:
            raise AssertionError("incident mismatch")
        self.control.update({"halted": False, "incident_id": None, "reason": ""})
        return True


class FakeAudit:
    def __init__(self) -> None:
        self.events = []

    def record(self, event):
        self.events.append(dict(event))

    def read(self, *, strict=False):
        return list(self.events)


class FakeBroker:
    def __init__(self, spool, handler) -> None:
        self.spool = spool
        self.audit = FakeAudit()
        self.handler = handler
        self.recovered = False

    def recover_startup(self):
        self.recovered = True

    def process_target(self, dst):
        if dst not in self.spool.heads:
            return None
        return self.handler(dst)


class BrokerRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_runtime_uses_nonblocking_single_instance_lock(self):
        spool = FakeSpool(self.root)
        broker = FakeBroker(spool, lambda _dst: None)
        runtime = BrokerRuntime(broker)
        runtime.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with runtime.lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BrokerAlreadyRunningError):
                runtime.run(stop_event=threading.Event())
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def test_targets_run_concurrently_and_each_target_remains_serial(self):
        spool = FakeSpool(self.root, ("sw_uart", "sw_spi"))
        uart_started = threading.Event()
        spi_started = threading.Event()
        release_uart = threading.Event()
        stop = threading.Event()
        calls = {"sw_uart": 0, "sw_spi": 0}

        def process(dst):
            calls[dst] += 1
            if dst == "sw_uart":
                uart_started.set()
                release_uart.wait(2)
            else:
                spi_started.set()
            spool.heads.pop(dst, None)
            return DeliveryResult("id", dst, DELIVERED)

        runtime = BrokerRuntime(FakeBroker(spool, process), poll_interval_s=0.01)
        runner = threading.Thread(target=runtime.run, args=(stop,))
        runner.start()
        self.assertTrue(uart_started.wait(1))
        self.assertTrue(spi_started.wait(1), "另一个目标不应被 UART 的等待阻塞")
        release_uart.set()
        stop.set()
        runner.join(2)
        self.assertFalse(runner.is_alive())
        self.assertEqual(calls, {"sw_uart": 1, "sw_spi": 1})

    def test_paused_queue_head_is_not_reprocessed_in_a_busy_loop(self):
        spool = FakeSpool(self.root, ("sw_uart",))
        calls = []
        stop = threading.Event()
        first_call = threading.Event()

        def process(dst):
            calls.append(dst)
            first_call.set()
            return DeliveryResult("id", dst, TARGET_BLOCKED, "operator action required")

        runtime = BrokerRuntime(FakeBroker(spool, process), poll_interval_s=0.01)
        runner = threading.Thread(target=runtime.run, args=(stop,))
        runner.start()
        self.assertTrue(first_call.wait(1))
        time.sleep(0.08)
        stop.set()
        runner.join(2)
        self.assertFalse(runner.is_alive())
        self.assertEqual(calls, ["sw_uart"])

    def test_worker_failure_halts_all_dispatch_and_is_audited(self):
        spool = FakeSpool(self.root, ("sw_uart",))
        broker = FakeBroker(spool, lambda _dst: (_ for _ in ()).throw(ValueError("spool damaged")))
        runtime = BrokerRuntime(broker, poll_interval_s=0.01)

        with self.assertRaisesRegex(RuntimeError, "fail-closed"):
            runtime.run()
        self.assertTrue(any(event.get("event") == "DISPATCH_HALTED"
                            for event in broker.audit.events))

    def test_keyboard_interrupt_is_graceful_and_does_not_persist_dispatch_halt(self):
        spool = FakeSpool(self.root)
        broker = FakeBroker(spool, lambda _dst: None)
        runtime = BrokerRuntime(broker, poll_interval_s=0.01)

        def interrupt():
            raise KeyboardInterrupt()

        runtime._schedule_workers = interrupt
        runtime.run()
        self.assertFalse(spool.control["halted"])
        self.assertFalse(any(event.get("event") == "DISPATCH_HALTED"
                             for event in broker.audit.events))


class QueueAlertTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.spool = Spool(root / "spool")
        self.audit = AuditLog(root / "audit.jsonl")
        now = now_iso()
        self.message = self.spool.enqueue(Message(
            msg_id=new_msg_id(), created_at=now, edge_id="dv_done", src="dv_uart",
            dst="sw_uart", project_id="soc_a", ip_id="uart", session="test1",
            text="uncertain delivery", state=QUEUED, topology_revision="r1", updated_at=now,
        ))
        self.spool.update(self.message.msg_id, state=DISPATCHING)
        self.spool.update(self.message.msg_id, state=DELIVERY_UNCERTAIN,
                          detail="operator action required")
        self.cycle_started = datetime(2025, 1, 1, tzinfo=timezone.utc)
        self.audit.record({"event": "STATE_TRANSITION", "msg_id": self.message.msg_id,
                           "state": DELIVERY_UNCERTAIN,
                           "ts": self.cycle_started.isoformat()})
        self.broker = SimpleNamespace(spool=self.spool, audit=self.audit)

    def make_runtime(self):
        return BrokerRuntime(self.broker, alert_reminder_s=3600,
                             alert_escalation_s=86400)

    def assert_uncertain_slot_unchanged(self):
        self.assertEqual(self.spool.get(self.message.msg_id).state, DELIVERY_UNCERTAIN)
        self.assertFalse(self.spool.slot_released("sw_uart", self.message.queue_seq))

    def queue_alerts(self):
        return [event for event in self.audit.read()
                if event.get("event") == "QUEUE_ALERT"
                and event.get("msg_id") == self.message.msg_id]

    def test_alert_thresholds_reminder_and_escalation_do_not_change_state_or_slot(self):
        runtime = self.make_runtime()
        self.assert_uncertain_slot_unchanged()

        self.assertEqual(runtime._emit_due_alerts(
            self.cycle_started + timedelta(seconds=3599)), 0)
        self.assertEqual(self.queue_alerts(), [])
        self.assert_uncertain_slot_unchanged()

        self.assertEqual(runtime._emit_due_alerts(
            self.cycle_started + timedelta(seconds=3600)), 1)
        self.assertEqual([event["level"] for event in self.queue_alerts()], ["reminder"])
        self.assert_uncertain_slot_unchanged()

        self.assertEqual(runtime._emit_due_alerts(
            self.cycle_started + timedelta(seconds=86400)), 1)
        self.assertEqual([event["level"] for event in self.queue_alerts()],
                         ["reminder", "escalation"])
        self.assert_uncertain_slot_unchanged()

    def test_alerts_are_idempotent_across_repeated_scans_and_runtime_restart(self):
        first_runtime = self.make_runtime()
        one_hour = self.cycle_started + timedelta(seconds=3600)
        self.assertEqual(first_runtime._emit_due_alerts(one_hour), 1)
        self.assertEqual(first_runtime._emit_due_alerts(one_hour), 0)

        restarted_runtime = self.make_runtime()
        self.assertEqual(restarted_runtime._emit_due_alerts(one_hour), 0)
        self.assertEqual(restarted_runtime._emit_due_alerts(
            self.cycle_started + timedelta(seconds=86400)), 1)
        self.assertEqual(self.make_runtime()._emit_due_alerts(
            self.cycle_started + timedelta(seconds=86400)), 0)
        self.assertEqual([event["level"] for event in self.queue_alerts()],
                         ["reminder", "escalation"])
        self.assert_uncertain_slot_unchanged()

    def test_new_uncertain_cycle_gets_a_new_alert_window(self):
        runtime = self.make_runtime()
        self.assertEqual(runtime._emit_due_alerts(
            self.cycle_started + timedelta(seconds=3600)), 1)

        next_cycle = self.cycle_started + timedelta(seconds=7200)
        self.audit.record({"event": "STATE_TRANSITION", "msg_id": self.message.msg_id,
                           "state": DELIVERY_UNCERTAIN, "transition_id": "cycle-two",
                           "ts": next_cycle.isoformat()})
        self.spool.update(self.message.msg_id, detail="second uncertain attempt",
                          transition_id="cycle-two")
        self.assertEqual(runtime._emit_due_alerts(next_cycle + timedelta(seconds=3600)), 1)
        self.assertEqual([event.get("transition_id") for event in self.queue_alerts()],
                         [None, "cycle-two"])


if __name__ == "__main__":
    unittest.main()
