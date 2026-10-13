"""路线图 189:审计日志轮转。轮转后 read() 仍按追加顺序返回全部历史;各崩溃点不重不漏;损坏 fail-closed。"""
from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from a2a import rulings
from a2a.audit import AuditCorrupt, AuditLog

SRC = Path(__file__).resolve().parent.parent / "src"


class Crash(Exception):
    pass


class Rotation(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.audit = AuditLog(self.dir / "audit.jsonl")
        self.n = 0

    def write(self, count, **extra):
        for _ in range(count):
            self.n += 1
            self.audit.record({"state": "TEST", "n": self.n, **extra})

    def numbers(self, audit=None):
        return [e["n"] for e in (audit or self.audit).read() if e.get("state") == "TEST"]

    def segments(self):
        return sorted(p.name for p in self.audit.segment_dir.iterdir())

    def crash_at(self, point):
        def hook(name):
            if name == point:
                raise Crash(name)
        self.audit.crash_hook = hook

    # ---- 正常轮转 ------------------------------------------------------
    def test_below_the_threshold_nothing_happens(self):
        self.write(3)
        result = self.audit.rotate(10_000_000)
        self.assertFalse(result["rotated"])
        self.assertEqual(self.segments(), [])

    def test_rotation_keeps_the_whole_history_in_append_order(self):
        self.write(5)
        self.assertTrue(self.audit.rotate(0)["rotated"])
        self.assertEqual(self.segments(), ["1.jsonl.gz"])
        self.assertEqual(self.audit.path.stat().st_size, 0)        # 新的空当前文件
        self.write(3)
        self.audit.rotate(0)
        self.write(2)
        self.assertEqual(self.segments(), ["1.jsonl.gz", "2.jsonl.gz"])
        self.assertEqual(self.numbers(), list(range(1, 11)))

    def test_order_is_append_order_not_timestamps(self):
        self.audit.record({"ts": "2099-01-01T00:00:00Z", "state": "TEST", "n": 1})
        self.audit.rotate(0)
        self.audit.record({"ts": "2000-01-01T00:00:00Z", "state": "TEST", "n": 2})
        self.assertEqual(self.numbers(), [1, 2])

    def test_a_torn_tail_is_quarantined_before_rotation(self):
        self.write(2)
        with open(self.audit.path, "ab") as f:
            f.write(b'{"state": "TEST", "n": 99')                    # 上次写到一半崩溃
        self.audit.rotate(0)
        with gzip.open(self.audit.segment_dir / "1.jsonl.gz", "rt") as f:
            self.assertTrue(f.read().endswith("\n"))                  # 分段里只有完整的行
        self.assertEqual(self.numbers(), [1, 2])
        self.assertEqual(len(self.audit.quarantined()), 1)

    def test_size_counts_the_current_file_and_all_segments(self):
        self.write(20)
        before = self.audit.size()
        self.audit.rotate(0)
        self.write(1)
        self.assertGreater(self.audit.size(), self.audit.path.stat().st_size)
        self.assertLess(self.audit.size(), before + 1000)

    # ---- 崩溃点 --------------------------------------------------------
    def test_crash_after_rename_before_the_new_file(self):
        self.write(4)
        self.crash_at("after_rename")
        with self.assertRaises(Crash):
            self.audit.rotate(0)
        self.audit.crash_hook = None
        self.assertFalse(self.audit.path.exists())
        self.assertEqual(self.numbers(), [1, 2, 3, 4])               # 只在 1.jsonl 里,不重不漏
        self.write(1)                                                  # 追加时自动建当前文件
        self.assertEqual(self.numbers(), [1, 2, 3, 4, 5])
        self.audit.rotate(10_000_000)                                  # 下次轮转补完压缩
        self.assertEqual(self.segments(), ["1.jsonl.gz"])
        self.assertEqual(self.numbers(), [1, 2, 3, 4, 5])

    def test_crash_after_publishing_gz_before_deleting_the_plain_file(self):
        self.write(4)
        self.crash_at("after_publish_gz")
        with self.assertRaises(Crash):
            self.audit.rotate(0)
        self.audit.crash_hook = None
        self.assertEqual(self.segments(), ["1.jsonl", "1.jsonl.gz"])
        self.assertEqual(self.numbers(), [1, 2, 3, 4])               # 两个都在:只读 .jsonl
        self.audit.rotate(10_000_000)
        self.assertEqual(self.segments(), ["1.jsonl.gz"])
        self.assertEqual(self.numbers(), [1, 2, 3, 4])

    def test_leftover_tmp_is_ignored_and_cleaned(self):
        self.write(2)
        self.audit.rotate(0)
        (self.audit.segment_dir / "2.jsonl.gz.tmp").write_bytes(b"half")
        self.assertEqual(self.numbers(), [1, 2])
        self.audit.rotate(10_000_000)
        self.assertEqual(self.segments(), ["1.jsonl.gz"])

    # ---- 损坏 fail-closed ----------------------------------------------
    def test_corrupt_or_truncated_segment_fails_closed(self):
        self.write(50)
        self.audit.rotate(0)
        seg = self.audit.segment_dir / "1.jsonl.gz"
        good = seg.read_bytes()
        for name, bad in (("截断", good[: len(good) // 2]),
                          ("改字节", good[:-6] + bytes([good[-6] ^ 0xFF]) + good[-5:])):
            with self.subTest(name):
                seg.write_bytes(bad)
                with self.assertRaisesRegex(AuditCorrupt, "1.jsonl.gz"):
                    self.audit.read()
        seg.write_bytes(good)
        self.assertEqual(len(self.numbers()), 50)

    def test_unexpected_or_conflicting_segment_names_fail_closed(self):
        self.write(2)
        self.audit.rotate(0)
        for name in ("01.jsonl", "1.jsonl.bak", "notes.txt"):
            with self.subTest(name=name):
                extra = self.audit.segment_dir / name
                extra.write_text("")
                with self.assertRaisesRegex(AuditCorrupt, "不认识的文件"):
                    self.audit.read()
                extra.unlink()
        (self.audit.segment_dir / "1.jsonl.gz").rename(self.audit.segment_dir / "renamed.gz")
        with self.assertRaises(AuditCorrupt):                          # 被改名的分段:不能只读当前文件了事
            self.audit.read()

    def test_segment_path_that_is_not_a_directory_fails_closed(self):
        self.write(2)
        self.audit.rotate(0)
        import shutil
        shutil.rmtree(self.audit.segment_dir)
        self.audit.segment_dir.write_text("误操作留下的普通文件")
        with self.assertRaisesRegex(AuditCorrupt, "不是目录"):
            self.audit.read()

    def test_rotation_does_not_delete_unknown_tmp_files(self):
        self.write(2)
        self.audit.rotate(0)
        notes = self.audit.segment_dir / "notes.tmp"
        notes.write_text("别人的文件")
        with self.assertRaises(AuditCorrupt):
            self.audit.rotate(0)
        self.assertTrue(notes.exists())                                 # 先校验、不清理
        with self.assertRaises(AuditCorrupt):
            self.audit.read()

    def test_a_torn_tail_found_by_rotation_is_audited(self):
        self.write(1)
        with open(self.audit.path, "ab") as f:
            f.write(b'{"state": "TEST", "n": 2')
        self.audit.rotate(0)
        self.assertEqual([e["state"] for e in self.audit.read() if e["state"] == "AUDIT_REPAIRED"], ["AUDIT_REPAIRED"])

    def test_a_missing_generation_fails_closed(self):
        for _ in range(3):
            self.write(1)
            self.audit.rotate(0)
        (self.audit.segment_dir / "2.jsonl.gz").unlink()
        with self.assertRaisesRegex(AuditCorrupt, "缺口"):
            self.audit.read()

    # ---- 依赖历史的内部逻辑 -------------------------------------------
    def test_an_unapplied_ruling_in_an_old_segment_is_still_found(self):
        self.audit.record({"state": rulings.OPERATOR_RULING, "ruling_id": "r1", "ruling": "delivered", "msg_id": "m"})
        self.write(3)
        self.audit.rotate(0)
        pending, _ = rulings.unapplied(self.audit)
        self.assertEqual([p["ruling_id"] for p in pending], ["r1"])
        self.audit.record({"state": rulings.RULING_APPLIED, "ruling_id": "r1"})
        self.assertEqual(rulings.unapplied(self.audit)[0], [])

    def test_commands_that_need_history_stop_on_a_corrupt_segment(self):
        self.write(5)
        self.audit.rotate(0)
        (self.audit.segment_dir / "1.jsonl.gz").write_bytes(b"not gzip")
        with self.assertRaises(AuditCorrupt):
            rulings.unapplied(self.audit)                              # broker 启动恢复用它补做裁定
        env = dict(os.environ, A2A_STATE_DIR=str(self.dir), PYTHONPATH=str(SRC), PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.run([sys.executable, "-m", "a2a.cli", "queue"], env=env, capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(proc.returncode, 6, proc.stderr)
        self.assertIn("审计日志损坏", json.loads(proc.stderr)["error"])

    def test_reads_during_rotation_never_lose_records(self):
        self.write(200)
        seen, stop = [], threading.Event()

        def reader():
            reader_log = AuditLog(self.audit.path)
            while not stop.is_set():
                seen.append(len(self.numbers(reader_log)))

        t = threading.Thread(target=reader)
        t.start()
        try:
            for _ in range(5):
                self.write(20)
                self.audit.rotate(0)
        finally:
            stop.set()
            t.join()
        self.assertTrue(seen)
        self.assertEqual(seen, sorted(seen))                          # 读到的条数只增不减
        self.assertEqual(self.numbers(), list(range(1, 301)))


if __name__ == "__main__":
    unittest.main()
