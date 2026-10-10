"""阶段 5a:queue_seq、队列头、槽位放行、受信任的重试入队(08-protocol.md §7)。不调用 herdr。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from dataclasses import replace
from pathlib import Path

from a2a.messages import (DELIVERED, DELIVERY_UNCERTAIN, DISPATCHING, FAILED, QUEUED, RETRYING, TARGET_BLOCKED,
                          TIMEOUT, Message, new_msg_id, now_iso)
from a2a.spool import QueueStateError, SlotError, Spool, SpoolError

SRC = Path(__file__).resolve().parents[1] / "src"


def make(dst="sw_uart", msg_id=None, text="uart 完成"):
    return Message(msg_id=msg_id or new_msg_id(), created_at=now_iso(), edge_id="dv_done", src="dv_uart",
                   dst=dst, project_id="soc_a", ip_id="uart", session="walk1", text=text,
                   state=QUEUED, topology_revision="rev1", updated_at=now_iso())


def fail(spool, msg_id, state=TIMEOUT):
    """把一条 QUEUED 消息推进到某个确定失败终态。"""
    if state == TIMEOUT:
        return spool.update(msg_id, state=TIMEOUT)
    if state == TARGET_BLOCKED:
        return spool.update(msg_id, state=TARGET_BLOCKED)
    spool.update(msg_id, state=DISPATCHING)
    return spool.update(msg_id, state=state)


def deliver(spool, msg_id):
    spool.update(msg_id, state=DISPATCHING)
    return spool.update(msg_id, state=DELIVERED)


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "spool"
        self.spool = Spool(self.root)


class QueueSeq(Base):
    def test_per_target_monotonic_and_independent(self):
        a = [self.spool.enqueue(make("sw_uart")).queue_seq for _ in range(3)]
        b = [self.spool.enqueue(make("sw_gpio")).queue_seq for _ in range(2)]
        self.assertEqual((a, b), ([1, 2, 3], [1, 2]))

    def test_order_follows_queue_seq_not_msg_id(self):
        # 模拟时钟回拨:后入队的消息 msg_id 更小,仍然排在后面
        late_id = "0000000000000001-aaaaaa"
        first = self.spool.enqueue(make())
        second = self.spool.enqueue(make(msg_id=late_id))
        self.assertLess(second.msg_id, first.msg_id)
        self.assertEqual([m.msg_id for m in self.spool.pending("sw_uart")], [first.msg_id, second.msg_id])
        self.assertEqual(self.spool.head("sw_uart").active.msg_id, first.msg_id)

    def test_only_spool_assigns_queue_seq(self):
        with self.assertRaises(SpoolError):
            self.spool.enqueue(replace(make(), queue_seq=7))
        with self.assertRaises(SpoolError):
            self.spool.enqueue(replace(make(), retry_of=new_msg_id()))

    def test_survives_restart_and_never_reuses_after_archive(self):
        m1 = self.spool.enqueue(make())
        deliver(self.spool, m1.msg_id)
        self.spool.release("sw_uart", 1, reason="delivered")
        again = Spool(self.root).enqueue(make())
        self.assertEqual(again.queue_seq, 2)

    def test_missing_queue_file_with_numbered_messages_fails_closed(self):
        # 原先按已有消息推算序号继续入队;Codex 审核指出这会丢失未放行记录,改为 fail-closed
        for _ in range(3):
            self.spool.enqueue(make())
        (self.root / "queues" / "sw_uart.json").unlink()
        with self.assertRaises(QueueStateError):
            self.spool.enqueue(make())

    def test_corrupt_queue_file_fails_closed(self):
        self.spool.enqueue(make())
        path = self.root / "queues" / "sw_uart.json"
        for bad in ("{not json", json.dumps({"next_seq": 0, "unreleased": {}}),
                    json.dumps({"next_seq": 3, "unreleased": {"9": {"msg_id": new_msg_id()}}}),
                    json.dumps({"next_seq": 3, "unreleased": {"1": {"msg_id": "../../x"}}})):
            path.write_text(bad)
            with self.assertRaises(QueueStateError, msg=bad):
                self.spool.enqueue(make())
            with self.assertRaises(QueueStateError, msg=bad):
                self.spool.head("sw_uart")

    def test_concurrent_enqueue_from_processes_gets_unique_contiguous_seqs(self):
        script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(SRC)!r})
            from a2a.messages import Message, new_msg_id, now_iso, QUEUED
            from a2a.spool import Spool
            spool = Spool({str(self.root)!r})
            for _ in range(10):
                spool.enqueue(Message(msg_id=new_msg_id(), created_at=now_iso(), edge_id="e", src="dv_uart",
                    dst="sw_uart", project_id="p", ip_id="uart", session="s", text="t", state=QUEUED,
                    topology_revision="r"))
        """)
        procs = [subprocess.Popen([sys.executable, "-c", script]) for _ in range(5)]
        for p in procs:
            self.assertEqual(p.wait(timeout=60), 0)
        seqs = sorted(m.queue_seq for m in self.spool.pending("sw_uart"))
        self.assertEqual(seqs, list(range(1, 51)))


class HeadAndRelease(Base):
    def test_empty_queue_has_no_head(self):
        self.assertIsNone(self.spool.head("sw_uart"))

    def test_head_is_the_lowest_unreleased_slot(self):
        m1, m2 = self.spool.enqueue(make()), self.spool.enqueue(make())
        head = self.spool.head("sw_uart")
        self.assertEqual((head.queue_seq, head.active.msg_id, head.awaiting_release), (1, m1.msg_id, False))

    def test_archiving_a_failed_head_does_not_release_it(self):
        m1, m2 = self.spool.enqueue(make()), self.spool.enqueue(make())
        fail(self.spool, m1.msg_id, TIMEOUT)
        self.assertEqual(self.spool.pending("sw_uart"), [self.spool.get(m2.msg_id)])  # m1 已归档
        head = self.spool.head("sw_uart")
        self.assertEqual(head.queue_seq, 1)          # 但队列头仍是 m1 的槽位
        self.assertTrue(head.awaiting_release)
        self.assertEqual(head.last_terminal.state, TIMEOUT)
        self.assertIn("sw_uart", self.spool.queue_targets())

    def test_delivered_head_also_waits_for_explicit_release(self):
        m1, m2 = self.spool.enqueue(make()), self.spool.enqueue(make())
        deliver(self.spool, m1.msg_id)
        self.assertEqual(self.spool.head("sw_uart").queue_seq, 1)
        record = self.spool.release("sw_uart", 1, reason="delivered")
        self.assertEqual((record["msg_id"], record["state"]), (m1.msg_id, DELIVERED))
        self.assertEqual(self.spool.head("sw_uart").active.msg_id, m2.msg_id)

    def test_only_the_head_can_be_released_and_only_when_settled(self):
        m1, m2 = self.spool.enqueue(make()), self.spool.enqueue(make())
        fail(self.spool, m2.msg_id, TIMEOUT)  # 第二个槽位先到终态
        with self.assertRaisesRegex(SlotError, "只能放行队列头"):
            self.spool.release("sw_uart", 2, reason="x")
        with self.assertRaisesRegex(SlotError, "没有到终态"):
            self.spool.release("sw_uart", 1, reason="x")  # 槽位 1 还在投递中
        with self.assertRaises(SlotError):
            self.spool.release("sw_uart", 99, reason="x")

    def test_release_is_idempotent_and_records_ruling(self):
        m1 = self.spool.enqueue(make())
        fail(self.spool, m1.msg_id, TARGET_BLOCKED)
        first = self.spool.release("sw_uart", 1, reason="abandon_and_continue", ruling_id="r-1")
        self.assertEqual((first["ruling_id"], first["already_released"]), ("r-1", False))
        again = self.spool.release("sw_uart", 1, reason="again")  # 补做时不报错,并说明已放行过
        self.assertTrue(again["already_released"])
        self.assertIsNone(self.spool.head("sw_uart"))

    def test_uncertain_head_keeps_the_slot_through_retrying(self):
        m1, m2 = self.spool.enqueue(make()), self.spool.enqueue(make())
        self.spool.update(m1.msg_id, state=DISPATCHING)
        self.spool.update(m1.msg_id, state=DELIVERY_UNCERTAIN)
        self.assertEqual(self.spool.head("sw_uart").active.msg_id, m1.msg_id)
        self.spool.update(m1.msg_id, state=RETRYING)       # 操作员裁定重试:同一条消息
        self.assertEqual(self.spool.head("sw_uart").active.msg_id, m1.msg_id)
        self.spool.update(m1.msg_id, state=DISPATCHING)
        self.spool.update(m1.msg_id, state=DELIVERY_UNCERTAIN)  # 再次不确定:暂停连续保持
        self.assertEqual(self.spool.head("sw_uart").queue_seq, 1)


class RetryEnqueue(Base):
    def setUp(self):
        super().setUp()
        self.m1 = self.spool.enqueue(make(text="原文"))
        self.m2 = self.spool.enqueue(make())
        fail(self.spool, self.m1.msg_id, TIMEOUT)

    def test_retry_inherits_slot_and_authorisation(self):
        rid = new_msg_id()
        retry = self.spool.enqueue_retry(self.m1.msg_id, rid)
        self.assertEqual((retry.msg_id, retry.queue_seq, retry.retry_of, retry.state), (rid, 1, self.m1.msg_id, QUEUED))
        for field in ("edge_id", "src", "dst", "project_id", "ip_id", "session", "text", "topology_revision"):
            self.assertEqual(getattr(retry, field), getattr(self.m1, field), field)
        head = self.spool.head("sw_uart")
        self.assertEqual((head.queue_seq, head.active.msg_id, head.last_terminal.msg_id), (1, rid, self.m1.msg_id))

    def test_retry_is_idempotent_by_new_msg_id(self):
        rid = new_msg_id()
        first = self.spool.enqueue_retry(self.m1.msg_id, rid)
        self.assertEqual(self.spool.enqueue_retry(self.m1.msg_id, rid), first)
        self.assertEqual(len([m for m in self.spool.pending("sw_uart") if m.queue_seq == 1]), 1)

    def test_at_most_one_active_message_per_slot(self):
        self.spool.enqueue_retry(self.m1.msg_id, new_msg_id())
        with self.assertRaisesRegex(SlotError, "未终结"):
            self.spool.enqueue_retry(self.m1.msg_id, new_msg_id())

    def test_retry_chain_must_use_the_latest_result(self):
        r1 = self.spool.enqueue_retry(self.m1.msg_id, new_msg_id())
        fail(self.spool, r1.msg_id, TIMEOUT)
        with self.assertRaisesRegex(SlotError, "最后一次"):
            self.spool.enqueue_retry(self.m1.msg_id, new_msg_id())
        r2 = self.spool.enqueue_retry(r1.msg_id, new_msg_id())
        self.assertEqual(r2.queue_seq, 1)

    def test_cannot_retry_a_released_slot(self):
        self.spool.release("sw_uart", 1, reason="abandon_and_continue")
        with self.assertRaisesRegex(SlotError, "已放行"):
            self.spool.enqueue_retry(self.m1.msg_id, new_msg_id())

    def test_cannot_retry_non_terminal_or_delivered(self):
        with self.assertRaisesRegex(SlotError, "不是终态|还不是终态"):
            self.spool.enqueue_retry(self.m2.msg_id, new_msg_id())
        other = self.spool.enqueue(make("sw_gpio"))
        deliver(self.spool, other.msg_id)
        with self.assertRaisesRegex(SlotError, "已送达"):
            self.spool.enqueue_retry(other.msg_id, new_msg_id())

    def test_new_msg_id_collision_with_unrelated_message_is_rejected(self):
        with self.assertRaisesRegex(SpoolError, "不是"):
            self.spool.enqueue_retry(self.m1.msg_id, self.m2.msg_id)


class CrashRecovery(Base):
    def test_slot_recorded_but_archive_not_done_keeps_the_head(self):
        # 模拟三步归档的第一步之后崩溃:槽位已记为未放行,但消息仍在 pending/ 且未到终态
        m1 = self.spool.enqueue(make())
        qpath = self.root / "queues" / "sw_uart.json"
        queue = json.loads(qpath.read_text())
        queue["unreleased"]["1"] = {"msg_id": m1.msg_id, "state": TIMEOUT}
        qpath.write_text(json.dumps(queue))
        head = self.spool.head("sw_uart")
        self.assertEqual((head.queue_seq, head.active.msg_id), (1, m1.msg_id))
        self.assertEqual(self.spool.recover()["removed_stale_slot_records"], 1)
        self.assertEqual(self.spool.head("sw_uart").active.msg_id, m1.msg_id)

    def test_archived_but_pending_copy_left_still_awaits_release(self):
        m1 = self.spool.enqueue(make())
        pending_copy = (self.root / "pending" / "sw_uart" / f"{m1.msg_id}.json").read_text()
        fail(self.spool, m1.msg_id, TIMEOUT)
        (self.root / "pending" / "sw_uart" / f"{m1.msg_id}.json").write_text(pending_copy)  # 删 pending 前崩溃
        head = self.spool.head("sw_uart")
        self.assertTrue(head.awaiting_release)  # 不会把残留的 pending 副本当成待投递
        self.spool.recover()
        self.assertTrue(self.spool.head("sw_uart").awaiting_release)

    def test_dangling_slot_record_fails_closed(self):
        self.spool.enqueue(make())
        qpath = self.root / "queues" / "sw_uart.json"
        queue = json.loads(qpath.read_text())
        queue["unreleased"]["1"] = {"msg_id": new_msg_id(), "state": TIMEOUT}
        qpath.write_text(json.dumps(queue))
        # 槽位 1 的消息既在 pending(活动)……这里 pending 有 m1,所以仍是活动头;删掉 pending 制造悬空
        for p in (self.root / "pending" / "sw_uart").glob("*.json"):
            p.unlink()
        with self.assertRaises(QueueStateError):
            self.spool.head("sw_uart")

    def failed_head_then_second(self):
        m1 = self.spool.enqueue(make())
        fail(self.spool, m1.msg_id, TIMEOUT)
        m2 = self.spool.enqueue(make())
        return m1, m2

    def test_missing_queue_state_file_fails_closed_instead_of_skipping_the_failed_head(self):
        # Codex 审核阻断 1:状态文件丢失时,不能当作"没有未放行槽位"而直接投递 m2
        self.failed_head_then_second()
        (self.root / "queues" / "sw_uart.json").unlink()
        with self.assertRaises(QueueStateError):
            self.spool.head("sw_uart")
        with self.assertRaises(QueueStateError):
            self.spool.enqueue(make())

    def test_recover_keeps_a_slot_whose_terminal_record_is_lost(self):
        # Codex 审核阻断 2:done/ 记录丢失、pending/ 也没有副本 -> 不能删槽位放行后续消息
        m1, _ = self.failed_head_then_second()
        (self.root / "done" / f"{m1.msg_id}.json").unlink()
        self.assertEqual(self.spool.recover()["removed_stale_slot_records"], 0)
        with self.assertRaises(QueueStateError):
            self.spool.head("sw_uart")

    def test_recover_keeps_a_slot_whose_terminal_record_is_corrupt(self):
        m1, _ = self.failed_head_then_second()
        (self.root / "done" / f"{m1.msg_id}.json").write_text("{坏")
        self.assertEqual(self.spool.recover()["removed_stale_slot_records"], 0)
        with self.assertRaises(SpoolError):
            self.spool.head("sw_uart")

    def test_legacy_message_without_queue_seq_is_not_skipped(self):
        legacy = make()
        path = self.root / "pending" / "sw_uart" / f"{legacy.msg_id}.json"
        path.parent.mkdir(parents=True)
        data = legacy.to_dict()
        data.pop("queue_seq")
        data.pop("retry_of")
        path.write_text(json.dumps(data))
        new = self.spool.enqueue(make())
        self.assertEqual([m.msg_id for m in self.spool.pending("sw_uart")], [legacy.msg_id, new.msg_id])


if __name__ == "__main__":
    unittest.main()
