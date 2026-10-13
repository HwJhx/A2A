"""路线图 189:spool 终态记录归档、存储用量统计。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from a2a import storage
from a2a.archive import ArchiveError, archive, archived_members, find_archived, referenced_by_rulings
from a2a.audit import AuditLog
from a2a.messages import FAILED, QUEUED, Message, new_msg_id, now_iso
from a2a.rulings import OPERATOR_RULING, RULING_VOIDED
from a2a.spool import MessageNotFoundError, Spool

SRC = Path(__file__).resolve().parent.parent / "src"
NOW = datetime(2026, 10, 13, tzinfo=timezone.utc)


class Crash(Exception):
    pass


class Archive(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.spool = Spool(self.state / "spool")
        self.audit = AuditLog(self.state / "audit.jsonl")

    def finish(self, days_ago, *, release=True, dst="sw_uart"):
        """一条到终态 days_ago 天的消息;release=False 时槽位不放行。"""
        msg = self.spool.enqueue(Message(msg_id=new_msg_id(), created_at=now_iso(), edge_id="dv_done", src="dv_uart",
                                         dst=dst, project_id="p", ip_id="uart", session="s", text="t", state=QUEUED,
                                         topology_revision="r"))
        self.spool.update(msg.msg_id, state=FAILED, detail="x")
        if release:
            self.spool.release(dst, msg.queue_seq, reason="test")
        path = self.spool.done_dir / f"{msg.msg_id}.json"
        data = json.loads(path.read_text())
        data["updated_at"] = (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")
        path.write_text(json.dumps(data))
        return msg.msg_id

    def run_archive(self, days=30, **kw):
        return archive(self.spool, days, now=NOW, **kw)

    def test_volumes_are_per_month_of_reaching_the_terminal_state(self):
        self.finish(40)
        self.finish(45)                                                  # 8 月底
        self.assertEqual(self.run_archive()["volumes"], ["2026-08-1.tar.gz", "2026-09-1.tar.gz"])

    def test_old_terminal_records_move_into_a_volume_and_stay_queryable(self):
        old = [self.finish(40), self.finish(41)]
        young = self.finish(5)
        result = self.run_archive()
        self.assertEqual((result["archived"], result["volumes"]), (2, ["2026-09-1.tar.gz"]))
        for msg_id in old:
            with self.assertRaises(MessageNotFoundError):
                self.spool.get(msg_id)
            self.assertEqual(json.loads(find_archived(self.spool, msg_id))["msg_id"], msg_id)
        self.assertEqual(self.spool.get(young).msg_id, young)

    def test_records_that_must_stay(self):
        unreleased = self.finish(40, release=False, dst="sw_gpio")       # 槽位未放行 / 队列头 last_terminal
        ruled = self.finish(40)
        in_pending = self.finish(40)
        (self.spool.pending_dir / "sw_uart").mkdir(parents=True, exist_ok=True)
        (self.spool.pending_dir / "sw_uart" / f"{in_pending}.json").write_text("{}")   # 终态归档中断留下的
        result = self.run_archive(referenced={ruled})
        self.assertEqual(result["archived"], 0)
        self.assertEqual(set(result["skipped"]), {unreleased, ruled, in_pending})
        self.assertIn("未放行", result["skipped"][unreleased])
        for msg_id in (unreleased, ruled):
            self.assertEqual(self.spool.get(msg_id).msg_id, msg_id)

    def test_referenced_by_rulings_ignores_voided_ones(self):
        entries = [{"state": OPERATOR_RULING, "ruling_id": "r1", "msg_id": "a", "retry_msg_id": "b"},
                   {"state": OPERATOR_RULING, "ruling_id": "r2", "msg_id": "c"},
                   {"state": RULING_VOIDED, "voided_ruling_id": "r2"}]
        self.assertEqual(referenced_by_rulings(entries), {"a", "b"})

    def test_retention_shorter_than_the_idempotency_key_is_refused(self):
        self.finish(40)
        with self.assertRaisesRegex(ArchiveError, "幂等键有效期"):
            self.run_archive(days=6)

    def test_dry_run_changes_nothing(self):
        self.finish(40)
        result = self.run_archive(dry_run=True)
        self.assertEqual(result["would_archive"], 1)
        self.assertEqual(len(self.spool.done()), 1)
        self.assertFalse((self.spool.root / "archive").exists())

    def test_crash_after_packing_before_deleting_is_completed_without_duplicates(self):
        ids = [self.finish(40), self.finish(41)]

        def hook(name):
            raise Crash(name)
        with self.assertRaises(Crash):
            self.run_archive(crash_hook=hook)
        self.assertEqual(len(self.spool.done()), 2)                      # 包已发布,原文件还在
        result = self.run_archive()
        self.assertEqual((result["archived"], result["already_archived"]), (0, 2))
        self.assertEqual(self.spool.done(), [])
        self.assertEqual(sorted(archived_members(self.spool)), sorted(f"{i}.json" for i in ids))
        self.assertEqual(len(list((self.spool.root / "archive").glob("*.tar.gz"))), 1)

    def test_new_runs_add_volumes_and_never_rewrite_old_ones(self):
        self.finish(40)
        self.run_archive()
        first = (self.spool.root / "archive" / "2026-09-1.tar.gz").read_bytes()
        self.finish(42)
        self.assertEqual(self.run_archive()["volumes"], ["2026-09-2.tar.gz"])
        self.assertEqual((self.spool.root / "archive" / "2026-09-1.tar.gz").read_bytes(), first)
        self.assertEqual(len(archived_members(self.spool)), 2)

    def test_a_broken_volume_stops_archiving(self):
        self.finish(40)
        self.run_archive()
        (self.spool.root / "archive" / "2026-09-1.tar.gz").write_bytes(b"broken")
        self.finish(41)
        with self.assertRaisesRegex(ArchiveError, "读不出"):
            self.run_archive()
        self.assertEqual(len(self.spool.done()), 1)                      # 原文件未删

    def test_cli_refuses_to_archive_when_a_ruling_line_is_unreadable(self):
        self.finish(400)
        with open(self.audit.path, "a") as f:
            f.write('{"state": "OPERATOR_RULING", "ruling_id": "r1", "msg_id": BROKEN\n')
        env = dict(os.environ, A2A_STATE_DIR=str(self.state), PYTHONPATH=str(SRC), PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.run([sys.executable, "-m", "a2a.cli", "spool", "archive"], env=env, capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(proc.returncode, 6, proc.stderr)
        self.assertIn("读不出的裁定", json.loads(proc.stderr)["error"])
        self.assertEqual(len(self.spool.done()), 1)

    def test_archive_waits_for_the_rulings_lock(self):
        # Codex 审核:引用集合与归档要在裁定锁内,与 resolve 串行
        import threading
        from a2a import rulings
        from a2a.archive import archive_guarded
        self.finish(400)
        done = threading.Event()
        with rulings.lock(self.state):
            t = threading.Thread(target=lambda: (archive_guarded(self.state, self.spool, self.audit, 30, now=NOW),
                                                 done.set()))
            t.start()
            self.assertFalse(done.wait(0.5))                             # 裁定进行中:归档等待
            # 持锁期间写一条引用该消息的裁定:归档拿到锁后必须看到它
            self.audit.record({"state": OPERATOR_RULING, "ruling_id": "r9", "msg_id": self.spool.done()[0].msg_id})
        t.join(10)
        self.assertTrue(done.is_set())
        self.assertEqual(len(self.spool.done()), 1)                      # 被新裁定引用:没有归档

    def test_usage_tolerates_files_vanishing_during_the_scan(self):
        self.finish(40)
        real_stat = Path.stat
        calls = {"n": 0}

        def flaky_stat(path, *a, **k):                                 # 模拟统计时文件刚被归档 / 轮转删除
            calls["n"] += 1
            if path.suffix == ".json" and calls["n"] % 2 == 0:
                raise FileNotFoundError(path)
            return real_stat(path, *a, **k)
        Path.stat = flaky_stat
        try:
            storage.usage(self.state, self.spool, self.audit, now=NOW)
        finally:
            Path.stat = real_stat

    # ---- 存储用量 ------------------------------------------------------
    def test_usage_counts_archive_audit_segments_and_idempotency_records(self):
        for _ in range(3):
            self.finish(40)
        self.audit.record({"state": "TEST", "pad": "x" * 2000})
        self.audit.rotate(0)
        self.audit.record({"state": "TEST"})
        (self.state / "idempotency" / "dv_uart").mkdir(parents=True)
        (self.state / "idempotency" / "dv_uart" / "k.json").write_text("{}")
        before = storage.usage(self.state, self.spool, self.audit, now=NOW)
        self.assertEqual(before["done"]["older_than_retention"], 3)
        self.assertTrue(any("a2a spool archive" in h for h in before["hints"]))
        self.run_archive()
        after = storage.usage(self.state, self.spool, self.audit, now=NOW)
        self.assertEqual(after["done"]["files"], 0)
        self.assertGreater(after["archive_bytes"], 0)
        self.assertGreater(after["audit"]["total_bytes"], after["audit"]["current_bytes"])   # 分段计入
        self.assertEqual(after["idempotency"]["records"], 1)
        self.assertGreaterEqual(after["state_dir_bytes"], after["archive_bytes"] + after["audit"]["total_bytes"])

    def test_cli_status_archive_and_rotate(self):
        msg_id = self.finish(400)
        env = dict(os.environ, A2A_STATE_DIR=str(self.state), PYTHONPATH=str(SRC), PYTHONDONTWRITEBYTECODE="1")

        def a2a(*argv):
            proc = subprocess.run([sys.executable, "-m", "a2a.cli", *argv], env=env, capture_output=True, text=True,
                                  timeout=60)
            return proc.returncode, proc.stdout, proc.stderr
        rc, out, err = a2a("spool", "archive", "--older-than", "30")
        self.assertEqual((rc, json.loads(out)["archived"]), (0, 1), err)
        rc, out, _ = a2a("status", msg_id)
        self.assertEqual((rc, json.loads(out)["archived"]), (0, True))
        rc, out, _ = a2a("status")
        self.assertIn("hints", json.loads(out))
        rc, _, err = a2a("spool", "archive", "--older-than", "3")
        self.assertEqual(rc, 6, err)
        self.audit.record({"state": "TEST"})
        rc, out, _ = a2a("audit", "rotate", "--max-size", "0.000001")
        self.assertEqual((rc, json.loads(out)["rotated"]), (0, True))


if __name__ == "__main__":
    unittest.main()
