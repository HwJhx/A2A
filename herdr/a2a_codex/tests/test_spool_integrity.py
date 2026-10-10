from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from a2a_codex.messages import QUEUED, TIMEOUT, Message, new_msg_id, now_iso
from a2a_codex.spool import QueueSequenceGapError, Spool


class SpoolSequenceIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "spool"
        self.spool = Spool(self.root)

    def message(self, text: str) -> Message:
        now = now_iso()
        return Message(msg_id=new_msg_id(), created_at=now, edge_id="dv_done",
                       src="dv_uart", dst="sw_uart", project_id="soc_a", ip_id="uart",
                       session="test1", text=text, state=QUEUED,
                       topology_revision="r1", updated_at=now)

    def test_missing_unreleased_terminal_message_fails_closed(self):
        first = self.spool.enqueue(self.message("first"))
        self.spool.update(first.msg_id, state=TIMEOUT, detail="failed before delivery")
        self.spool.enqueue(self.message("second"))
        (self.spool.done_dir / (first.msg_id + ".json")).unlink()

        with self.assertRaises(QueueSequenceGapError):
            self.spool.queue_head("sw_uart")
        with self.assertRaises(QueueSequenceGapError):
            self.spool.recover()
        self.assertTrue(self.spool.dispatch_control()["halted"])

    def test_enqueue_crash_after_message_write_recovers_sequence_without_a_gap(self):
        original_save = self.spool._save_queue_meta_unlocked
        saves = {"count": 0}

        def fail_after_message_write(meta):
            saves["count"] += 1
            if saves["count"] == 2:
                raise OSError("simulated crash before next_seq persist")
            return original_save(meta)

        with mock.patch.object(self.spool, "_save_queue_meta_unlocked",
                               side_effect=fail_after_message_write):
            with self.assertRaisesRegex(OSError, "simulated crash"):
                self.spool.enqueue(self.message("first"))

        recovered = Spool(self.root)
        first = recovered.queue_head("sw_uart")
        self.assertIsNotNone(first)
        self.assertEqual(first.queue_seq, 1)
        self.assertEqual(recovered.enqueue(self.message("second")).queue_seq, 2)


if __name__ == "__main__":
    unittest.main()
