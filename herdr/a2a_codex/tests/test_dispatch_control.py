from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from a2a_codex.audit import AuditLog
from a2a_codex.broker import DeliveryBroker
from a2a_codex.messages import Message, QUEUED, new_msg_id, now_iso
from a2a_codex.runtime import BrokerRuntime, DispatchHaltedError
from a2a_codex.spool import Spool, SpoolError


class FakeRegistry:
    def get(self, _agent_id):
        return SimpleNamespace(agent_id="sw_uart", project_id="soc_a", ip_id="uart",
                               session="test1", pane_id="w1:p2", lifecycle="running")


class UnusedHerdr:
    def get_agent(self, _pane_id):
        raise AssertionError("dispatch-control test must not query Herdr")

    def send_prompt(self, *_args, **_kwargs):
        raise AssertionError("dispatch-control test must not send prompts")


class DispatchControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.spool = Spool(root / "spool")
        self.audit = AuditLog(root / "audit.jsonl")
        self.broker = DeliveryBroker(UnusedHerdr(), FakeRegistry(), self.spool, self.audit)
        self.runtime = BrokerRuntime(self.broker, poll_interval_s=0.01)

    def test_halted_runtime_does_not_recover_or_dispatch(self):
        control = self.spool.halt_dispatch("audit corruption", incident_id="halt-1")
        with self.assertRaises(DispatchHaltedError):
            self.runtime.run(stop_event=threading.Event())
        self.assertTrue(self.spool.dispatch_control()["halted"])
        self.assertTrue(any(event.get("event") == "DISPATCH_HALTED"
                            and event.get("incident_id") == control["incident_id"]
                            for event in self.audit.read()))

    def test_resume_audits_before_clearing_persistent_halt(self):
        self.spool.halt_dispatch("operator requested halt", incident_id="halt-2")
        event = self.runtime.resume_dispatch(actor="jhx", reason="data verified",
                                             quarantine_location="none")
        self.assertEqual(event["event"], "DISPATCH_RESUMED")
        self.assertEqual(event["incident_id"], "halt-2")
        self.assertFalse(self.spool.dispatch_control()["halted"])

    def test_corrupt_spool_prevents_resume_and_keeps_halt(self):
        self.spool.halt_dispatch("spool corruption", incident_id="halt-3")
        self.spool.queue_meta_path.write_text("{bad json", encoding="utf-8")
        with self.assertRaises(SpoolError):
            self.runtime.resume_dispatch(actor="jhx", reason="not repaired",
                                         quarantine_location="/tmp/corrupt-spool")
        self.assertTrue(self.spool.dispatch_control()["halted"])
        self.assertFalse(any(event.get("event") == "DISPATCH_RESUMED"
                             for event in self.audit.read()))

    def test_corrupt_spool_is_quarantined_and_persistent_halt_survives_restart(self):
        corrupt_bytes = b"{broken queue metadata"
        self.spool.queue_meta_path.write_bytes(corrupt_bytes)

        with self.assertRaises(SpoolError):
            self.spool.recover()

        incidents = self.spool.quarantine_incidents(unresolved_only=True)
        self.assertEqual(len(incidents), 1)
        incident = incidents[0]
        self.assertEqual(incident["kind"], "queue_meta")
        self.assertTrue(incident["quarantined"])
        self.assertEqual((self.spool.root / incident["quarantine_path"]).read_bytes(), corrupt_bytes)
        self.assertTrue(self.spool.dispatch_control()["halted"])

        reopened = Spool(self.spool.root)
        self.assertTrue(reopened.dispatch_control()["halted"])
        self.assertEqual(reopened.quarantine_incidents(unresolved_only=True), incidents)

    def test_missing_queue_meta_with_pending_message_records_missing_original_and_halts(self):
        now = now_iso()
        queued = self.spool.enqueue(Message(
            msg_id=new_msg_id(), created_at=now, edge_id="dv_done", src="dv_uart",
            dst="sw_uart", project_id="soc_a", ip_id="uart", session="test1",
            text="queued before metadata loss", state=QUEUED, topology_revision="r1",
            updated_at=now,
        ))
        pending_path = next(self.spool.pending_dir.rglob(queued.msg_id + ".json"))
        pending_bytes = pending_path.read_bytes()
        self.spool.queue_meta_path.unlink()

        with self.assertRaises(SpoolError):
            self.spool.recover()

        incidents = self.spool.quarantine_incidents(unresolved_only=True)
        self.assertEqual(len(incidents), 1)
        incident = incidents[0]
        self.assertEqual(incident["kind"], "queue_meta")
        self.assertTrue(incident["missing_original"])
        self.assertIsNone(incident["quarantine_path"])
        self.assertFalse(self.spool.queue_meta_path.exists())
        self.assertTrue(pending_path.is_file(), "现存消息不可被误移入隔离目录")
        self.assertEqual(pending_path.read_bytes(), pending_bytes)
        self.assertTrue(self.spool.dispatch_control()["halted"])

    def test_unrestored_or_unaudited_quarantine_blocks_resume(self):
        self.spool.queue_meta_path.write_bytes(b"{broken queue metadata")
        with self.assertRaises(SpoolError):
            self.spool.recover()
        incident = self.spool.quarantine_incidents(unresolved_only=True)[0]

        with self.assertRaises(DispatchHaltedError):
            self.runtime.resume_dispatch(actor="jhx", reason="not restored",
                                         quarantine_location="backup-1")
        self.assertTrue(self.spool.dispatch_control()["halted"])

        self.spool.queue_meta_path.write_text(
            '{"version":1,"next_seq":{},"released":{}}\n', encoding="utf-8")
        with self.assertRaises(DispatchHaltedError):
            self.runtime.resume_dispatch(actor="jhx", reason="restored but not audited",
                                         quarantine_location="backup-1")
        self.assertTrue(self.spool.dispatch_control()["halted"])
        self.assertFalse(any(event.get("event") == "DISPATCH_RESUMED"
                             for event in self.audit.read()))
        self.assertEqual(self.spool.quarantine_incidents(unresolved_only=True)[0]["incident_id"],
                         incident["incident_id"])

    def test_spool_repair_is_audited_before_resolution_and_resume_is_explicit(self):
        self.spool.queue_meta_path.write_bytes(b"{broken queue metadata")
        with self.assertRaises(SpoolError):
            self.spool.recover()
        incident = self.spool.quarantine_incidents(unresolved_only=True)[0]
        self.spool.queue_meta_path.write_text(
            '{"version":1,"next_seq":{},"released":{}}\n', encoding="utf-8")

        real_mark_resolved = self.spool.mark_quarantine_resolved
        observed_order = []

        def mark_after_audit(incident_id):
            repaired = [event for event in self.audit.read(strict=True)
                        if event.get("event") == "SPOOL_REPAIRED"
                        and event.get("incident_id") == incident_id]
            self.assertEqual(len(repaired), 1, "必须先持久记录 SPOOL_REPAIRED")
            observed_order.append("audit")
            result = real_mark_resolved(incident_id)
            observed_order.append("resolved")
            return result

        with mock.patch.object(self.spool, "mark_quarantine_resolved",
                               side_effect=mark_after_audit):
            resolved = self.runtime.resolve_spool_corruption(
                incident["incident_id"], actor="jhx", reason="restored from backup",
                verification="queue metadata schema and sequence values checked")

        self.assertEqual(observed_order, ["audit", "resolved"])
        self.assertTrue(resolved["resolved"])
        self.assertTrue(self.spool.dispatch_control()["halted"],
                        "修复隔离项本身不应解除全局 halt")
        self.assertFalse(any(event.get("event") == "DISPATCH_RESUMED"
                             for event in self.audit.read()))

        resumed = self.runtime.resume_dispatch(actor="jhx", reason="repair verified",
                                               quarantine_location="backup-1")
        self.assertEqual(resumed["event"], "DISPATCH_RESUMED")
        self.assertFalse(self.spool.dispatch_control()["halted"])

    def test_restart_finishes_resume_event_written_before_control_clear(self):
        control = self.spool.halt_dispatch("recover resume", incident_id="halt-4")
        self.audit.record({"event": "DISPATCH_RESUMED", "incident_id": control["incident_id"],
                           "actor": "jhx", "reason": "already audited",
                           "quarantine_location": "none"})
        already_stopped = threading.Event()
        already_stopped.set()
        self.runtime.run(stop_event=already_stopped)
        self.assertFalse(self.spool.dispatch_control()["halted"])


if __name__ == "__main__":
    unittest.main()
