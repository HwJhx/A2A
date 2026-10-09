"""持久队列与审计日志测试。"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from a2a import (ALLOWED_TRANSITIONS, AuditLog, IllegalTransitionError, InvalidIdError, Message,
                 MessageNotFoundError, Spool, SpoolError)
from a2a.messages import (ALL_STATES, DELIVERED, DELIVERY_UNCERTAIN, DISPATCHING, FAILED, QUEUED, REJECTED,
                          RETRYING, TARGET_BLOCKED, TARGET_MISSING, TERMINAL_STATES, TIMEOUT, WAITING_TARGET,
                          new_msg_id, now_iso)
import pathlib


def make_message(dst="sw_uart", state=QUEUED, msg_id=None):
    return Message(msg_id=msg_id or new_msg_id(), created_at=now_iso(), edge_id="dv_done", src="dv_uart",
                   dst=dst, project_id="soc_a", ip_id="uart", session="walk1", text="uart 完成",
                   state=state, topology_revision="abc123", updated_at=now_iso())


class SpoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "spool"
        self.spool = Spool(self.root)

    def test_enqueue_get_and_pending(self):
        m = self.spool.enqueue(make_message())
        self.assertEqual(self.spool.get(m.msg_id), m)
        self.assertEqual(self.spool.pending("sw_uart"), [m])
        self.assertEqual(self.spool.pending(), [m])
        self.assertEqual(self.spool.pending("sw_gpio"), [])
        self.assertEqual(self.spool.pending_targets(), ["sw_uart"])

    def test_message_ids_sort_in_creation_order(self):
        ids = [new_msg_id() for _ in range(200)]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(set(ids)), 200)

    def test_pending_is_fifo_by_msg_id_across_targets_files(self):
        messages = [self.spool.enqueue(make_message()) for _ in range(10)]
        self.assertEqual([m.msg_id for m in self.spool.pending("sw_uart")], [m.msg_id for m in messages])

    def test_duplicate_id_and_terminal_state_rejected(self):
        m = self.spool.enqueue(make_message())
        with self.assertRaises(SpoolError):
            self.spool.enqueue(m)
        with self.assertRaises(SpoolError):
            self.spool.enqueue(make_message(state=DELIVERED))

    def test_non_terminal_update_stays_pending(self):
        m = self.spool.enqueue(make_message())
        updated = self.spool.update(m.msg_id, state=WAITING_TARGET, detail="目标忙", attempts=1)
        self.assertEqual((updated.state, updated.detail, updated.attempts), (WAITING_TARGET, "目标忙", 1))
        self.assertEqual(self.spool.pending("sw_uart")[0].state, WAITING_TARGET)
        self.assertEqual(self.spool.done(), [])

    def test_terminal_update_archives_the_message(self):
        m = self.spool.enqueue(make_message())
        self.spool.update(m.msg_id, state=DISPATCHING)
        self.spool.update(m.msg_id, state=DELIVERED, detail="ok")
        self.assertEqual(self.spool.pending(), [])
        self.assertEqual(self.spool.pending_targets(), [])
        self.assertEqual([x.msg_id for x in self.spool.done()], [m.msg_id])
        self.assertEqual(self.spool.get(m.msg_id).state, DELIVERED)  # 归档后仍可按 ID 查询
        for state in (TARGET_BLOCKED, FAILED):
            n = self.spool.enqueue(make_message())
            self.spool.update(n.msg_id, state=state)
            self.assertEqual(self.spool.get(n.msg_id).state, state)

    def test_unknown_message(self):
        with self.assertRaises(MessageNotFoundError):
            self.spool.get("0000000000000000-000000")
        with self.assertRaises(MessageNotFoundError):
            self.spool.update("0000000000000000-000000", state=FAILED)

    def test_relative_root_rejected(self):
        with self.assertRaises(SpoolError):
            Spool("spool")

    # ---- P3:ID 严格校验,防路径穿越 -----------------------------------
    def test_malicious_ids_are_rejected_everywhere(self):
        secret = Path(self.temp.name) / "secret.json"           # 位于 spool 目录之外
        secret.write_text(json.dumps(make_message().to_dict()), encoding="utf-8")
        before = secret.read_bytes()
        for bad in ("../../secret", "../secret", "/etc/passwd", "a/b", "", "nope", "0000000000000000-00000g",
                    "0000000000000000-000000/../x", "0000000000000000-000000\n", None, 5):
            with self.subTest(msg_id=bad):
                with self.assertRaises(InvalidIdError):
                    self.spool.get(bad)
                with self.assertRaises(InvalidIdError):
                    self.spool.update(bad, state=FAILED)
        for bad in ("../x", "..", "A", "sw uart", "", "x" * 40, "sw/uart"):
            with self.subTest(dst=bad):
                with self.assertRaises(InvalidIdError):
                    self.spool.pending(bad)
                with self.assertRaises(InvalidIdError):
                    self.spool.enqueue(make_message(dst=bad))
        self.assertEqual(secret.read_bytes(), before)           # 外部文件没有被读取后改写
        self.assertFalse((Path(self.temp.name) / "x.json").exists())

    # ---- P2:归档两步之间崩溃,不能重复投递 ----------------------------
    def test_crash_between_archive_steps_never_redelivers(self):
        m = self.spool.enqueue(make_message())
        self.spool.update(m.msg_id, state=DISPATCHING)
        script = textwrap.dedent("""
            import os, pathlib, sys
            from a2a import Spool
            from a2a.messages import DELIVERED
            real = pathlib.Path.unlink
            def dying(self, *a, **k):
                if self.parent.parent.name == "pending":      # 归档的后半步:删除 pending 里那份时崩溃
                    os._exit(137)
                return real(self, *a, **k)
            pathlib.Path.unlink = dying
            Spool(sys.argv[1]).update(sys.argv[2], state=DELIVERED)
        """)
        proc = subprocess.run([sys.executable, "-c", script, str(self.root), m.msg_id], env=os.environ.copy())
        self.assertEqual(proc.returncode, 137)
        stale = self.root / "pending" / "sw_uart" / f"{m.msg_id}.json"
        self.assertTrue(stale.exists())                          # 崩溃确实留下了两份
        self.assertTrue((self.root / "done" / f"{m.msg_id}.json").exists())
        # 即使 recover() 还没运行,broker 也看不到这条已送达的消息
        self.assertEqual(self.spool.pending(), [])
        self.assertEqual(self.spool.pending("sw_uart"), [])
        self.assertEqual(self.spool.pending_targets(), [])
        self.assertEqual(self.spool.get(m.msg_id).state, DELIVERED)
        # recover() 清理残留,且幂等
        self.assertEqual(self.spool.recover(), {"removed_duplicate_pending": 1, "removed_temp_files": 0})
        self.assertFalse(stale.exists())
        self.assertEqual(self.spool.recover(), {"removed_duplicate_pending": 0, "removed_temp_files": 0})
        self.assertEqual(self.spool.get(m.msg_id).state, DELIVERED)

    def test_recover_removes_temp_files_but_keeps_real_pending_messages(self):
        keep = self.spool.enqueue(make_message())
        (self.root / "pending" / "sw_uart" / "leftover.json.tmp").write_text("x", encoding="utf-8")
        (self.root / "done").mkdir(exist_ok=True)
        (self.root / "done" / "leftover.json.tmp").write_text("x", encoding="utf-8")
        self.assertEqual(self.spool.recover(), {"removed_duplicate_pending": 0, "removed_temp_files": 2})
        self.assertEqual(self.spool.pending("sw_uart"), [keep])

    def test_recover_ignores_an_unreadable_done_file(self):
        m = self.spool.enqueue(make_message())
        (self.root / "done").mkdir(exist_ok=True)
        (self.root / "done" / f"{m.msg_id}.json").write_text("{broken", encoding="utf-8")
        # done 里的记录不可信,不能据此删掉 pending 里的那份
        self.assertEqual(self.spool.recover()["removed_duplicate_pending"], 0)
        self.assertTrue((self.root / "pending" / "sw_uart" / f"{m.msg_id}.json").exists())

    # ---- P6 / P7:状态名与合法迁移 -------------------------------------
    PATHS = {
        QUEUED: [], WAITING_TARGET: [WAITING_TARGET], DISPATCHING: [DISPATCHING],
        RETRYING: [DISPATCHING, RETRYING], DELIVERY_UNCERTAIN: [DISPATCHING, DELIVERY_UNCERTAIN],
    }

    def at_state(self, state):
        m = self.spool.enqueue(make_message())
        for step in self.PATHS[state]:
            self.spool.update(m.msg_id, state=step)
        self.assertEqual(self.spool.get(m.msg_id).state, state)
        return m

    def test_every_transition_in_the_table_is_accepted_and_every_other_one_is_refused(self):
        for old, targets in ALLOWED_TRANSITIONS.items():
            for new in sorted(ALL_STATES):
                if new == old:
                    continue
                with self.subTest(old=old, new=new):
                    m = self.at_state(old)
                    if new in targets:
                        self.assertEqual(self.spool.update(m.msg_id, state=new).state, new)
                    else:
                        with self.assertRaises(IllegalTransitionError):
                            self.spool.update(m.msg_id, state=new)
                        self.assertEqual(self.spool.get(m.msg_id).state, old)   # 拒绝后原状态不变

    def test_unknown_state_names_are_refused(self):
        m = self.spool.enqueue(make_message())
        for bad in ("BOGUS", "queued", "", "DELIVERED ", None.__class__.__name__):
            with self.subTest(state=bad):
                with self.assertRaises(SpoolError):
                    self.spool.update(m.msg_id, state=bad)
        self.assertEqual(self.spool.get(m.msg_id).state, QUEUED)

    def test_terminal_messages_are_immutable(self):
        reach = {DELIVERED: [DISPATCHING, DELIVERED], TARGET_BLOCKED: [TARGET_BLOCKED], TARGET_MISSING: [TARGET_MISSING],
                 TIMEOUT: [TIMEOUT], FAILED: [FAILED]}
        for terminal, steps in reach.items():
            m = self.spool.enqueue(make_message())
            for step in steps:
                self.spool.update(m.msg_id, state=step)
            frozen = self.spool.get(m.msg_id)
            self.assertEqual(frozen.state, terminal)
            for kwargs in ({"state": QUEUED}, {"state": DISPATCHING}, {"state": terminal},
                           {"detail": "改写"}, {"attempts": 9}):
                with self.subTest(terminal=terminal, change=kwargs):
                    with self.assertRaises(IllegalTransitionError):
                        self.spool.update(m.msg_id, **kwargs)
                    self.assertEqual(self.spool.get(m.msg_id), frozen)           # 一个字节都没变

    def test_rejected_is_unreachable_and_not_enqueueable(self):
        with self.assertRaises(SpoolError):
            self.spool.enqueue(make_message(state=REJECTED))
        for old in ALLOWED_TRANSITIONS:
            self.assertNotIn(REJECTED, ALLOWED_TRANSITIONS[old])

    def test_only_queued_messages_can_be_enqueued(self):
        for state in sorted(ALL_STATES - {QUEUED}):
            with self.subTest(state=state):
                with self.assertRaises(SpoolError):
                    self.spool.enqueue(make_message(state=state))
        self.assertEqual(self.spool.pending(), [])

    def test_updating_detail_and_attempts_within_the_same_state_is_allowed(self):
        m = self.spool.enqueue(make_message())
        updated = self.spool.update(m.msg_id, detail="目标忙", attempts=2)
        self.assertEqual((updated.state, updated.detail, updated.attempts), (QUEUED, "目标忙", 2))
        updated = self.spool.update(m.msg_id, state=QUEUED, detail="仍在排队")
        self.assertEqual((updated.state, updated.detail), (QUEUED, "仍在排队"))

    def test_corrupt_message_file_raises_spool_error(self):
        m = self.spool.enqueue(make_message())
        (self.root / "pending" / "sw_uart" / f"{m.msg_id}.json").write_text("{bad", encoding="utf-8")
        with self.assertRaises(SpoolError):
            self.spool.get(m.msg_id)

    def test_crash_during_enqueue_leaves_no_message(self):
        script = textwrap.dedent("""
            import os, sys
            sys.path.insert(0, sys.argv[2])
            from test_spool_audit import make_message
            from a2a import Spool
            os.replace = lambda src, dst: os._exit(137)    # 临时文件已写好、还没替换到位时被杀
            Spool(sys.argv[1]).enqueue(make_message())
        """)
        tests_dir = str(Path(__file__).resolve().parent)
        proc = subprocess.run([sys.executable, "-c", script, str(self.root), tests_dir], env=os.environ.copy())
        self.assertEqual(proc.returncode, 137)
        self.assertEqual(self.spool.pending(), [])                  # 半截消息不可见
        m = self.spool.enqueue(make_message())                      # 之后仍可正常入队
        self.assertEqual(self.spool.pending("sw_uart"), [m])

    def test_concurrent_enqueue_from_many_processes(self):
        script = textwrap.dedent("""
            import sys
            sys.path.insert(0, sys.argv[2])
            from test_spool_audit import make_message
            from a2a import Spool
            spool = Spool(sys.argv[1])
            for _ in range(10):
                spool.enqueue(make_message())
        """)
        tests_dir = str(Path(__file__).resolve().parent)
        procs = [subprocess.Popen([sys.executable, "-c", script, str(self.root), tests_dir],
                                  env=os.environ.copy(), stderr=subprocess.PIPE) for _ in range(6)]
        for proc in procs:
            _, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err.decode())
        pending = self.spool.pending("sw_uart")
        self.assertEqual(len(pending), 60)
        self.assertEqual(len({m.msg_id for m in pending}), 60)


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.audit = AuditLog(Path(self.temp.name) / "logs" / "audit.jsonl")

    def test_record_read_and_tail(self):
        self.assertEqual(self.audit.read(), [])
        for n in range(5):
            self.audit.record({"msg_id": str(n), "state": "QUEUED", "detail": f"第 {n} 条"})
        entries = self.audit.read()
        self.assertEqual([e["msg_id"] for e in entries], ["0", "1", "2", "3", "4"])
        self.assertTrue(all(e["ts"].endswith("Z") for e in entries))
        self.assertEqual([e["msg_id"] for e in self.audit.tail(2)], ["3", "4"])
        self.assertIn("第 3 条", self.audit.path.read_text(encoding="utf-8"))   # 中文没有被转义

    def test_one_json_object_per_line(self):
        self.audit.record({"detail": "含\n换行\n的内容"})
        lines = self.audit.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["detail"], "含\n换行\n的内容")

    def test_file_is_private(self):
        self.audit.record({"x": 1})
        mode = stat.S_IMODE(os.stat(self.audit.path).st_mode)
        self.assertEqual(mode & 0o077, 0, oct(mode))   # 日志里可能有提示词,不应让其他用户可读

    def test_corrupt_line_is_reported_not_fatal(self):
        self.audit.record({"msg_id": "a"})
        with self.audit.path.open("a", encoding="utf-8") as fh:
            fh.write("{broken\n")
        self.audit.record({"msg_id": "b"})
        states = [e.get("state") for e in self.audit.read()]
        self.assertEqual(states.count("CORRUPT_LINE"), 1)
        self.assertEqual([e.get("msg_id") for e in self.audit.read() if e.get("msg_id")], ["a", "b"])

    def test_relative_path_rejected(self):
        with self.assertRaises(ValueError):
            AuditLog("audit.jsonl")

    # ---- P1:崩溃留下半行 ---------------------------------------------
    def test_partial_line_is_quarantined_not_merged_into_the_next_record(self):
        self.audit.record({"n": 1})
        with open(self.audit.path, "ab") as fh:
            fh.write(b'{"n": 2, "pa')                           # 写到一半崩溃:没有换行
        self.audit.record({"n": 3})
        entries = self.audit.read()
        self.assertEqual([e["n"] for e in entries if "n" in e], [1, 3])          # n=3 没有被拼进坏行丢掉
        self.assertEqual([e["state"] for e in entries if "state" in e], ["AUDIT_REPAIRED"])
        self.assertNotIn("CORRUPT_LINE", [e.get("state") for e in entries])
        self.assertIn('{"n": 2, "pa', self.audit.corrupt_path.read_text(encoding="utf-8"))   # 证据被保留
        self.assertEqual(stat.S_IMODE(os.stat(self.audit.corrupt_path).st_mode) & 0o077, 0)
        self.assertTrue(self.audit.path.read_bytes().endswith(b"\n"))

    def test_real_crash_in_the_middle_of_a_write(self):
        self.audit.record({"n": 1})
        script = textwrap.dedent("""
            import os, sys
            from a2a import AuditLog
            real = os.write
            def half(fd, data):
                data = bytes(data)
                real(fd, data[: len(data) // 2])               # 只写出一半
                os._exit(137)
            os.write = half
            AuditLog(sys.argv[1]).record({"n": 2, "pad": "x" * 60})
        """)
        proc = subprocess.run([sys.executable, "-c", script, str(self.audit.path)], env=os.environ.copy())
        self.assertEqual(proc.returncode, 137)
        self.assertFalse(self.audit.path.read_bytes().endswith(b"\n"))          # 确实留下了半行
        self.audit.record({"n": 3})
        entries = self.audit.read()
        self.assertEqual([e["n"] for e in entries if "n" in e], [1, 3])
        self.assertEqual([e["state"] for e in entries if "state" in e], ["AUDIT_REPAIRED"])
        self.assertIn('"n": 2', self.audit.corrupt_path.read_text(encoding="utf-8"))

    def test_log_consisting_only_of_a_fragment_is_repaired(self):
        self.audit.path.parent.mkdir(parents=True, exist_ok=True)
        self.audit.path.write_bytes(b'{"half')
        self.audit.record({"n": 1})
        self.assertEqual([e["n"] for e in self.audit.read() if "n" in e], [1])
        self.assertIn('{"half', self.audit.corrupt_path.read_text(encoding="utf-8"))

    def test_clean_log_is_never_touched_by_repair(self):
        self.audit.record({"n": 1})
        self.audit.record({"n": 2})
        self.assertFalse(self.audit.corrupt_path.exists())
        self.assertEqual([e.get("state") for e in self.audit.read() if "state" in e], [])

    def test_read_never_modifies_the_file(self):
        self.audit.record({"n": 1})
        with open(self.audit.path, "ab") as fh:
            fh.write(b'{"n": 2, "pa')
        before = self.audit.path.read_bytes()
        states = [e.get("state") for e in self.audit.read()]
        self.assertIn("CORRUPT_LINE", states)                     # 读的时候只报告,不修复
        self.assertEqual(self.audit.path.read_bytes(), before)
        self.assertFalse(self.audit.corrupt_path.exists())

    def test_short_writes_are_completed(self):
        real = os.write

        def stingy(fd, data):
            return real(fd, bytes(data)[:7])                       # 每次最多写 7 个字节

        from unittest import mock
        with mock.patch("a2a.audit.os.write", side_effect=stingy):
            self.audit.record({"msg": "一条很长的、会被拆成很多次短写入的审计记录" * 3})
        self.assertEqual(len(self.audit.read()), 1)
        self.assertTrue(self.audit.path.read_bytes().endswith(b"\n"))

    def test_concurrent_appends_never_interleave(self):
        script = textwrap.dedent("""
            import sys
            from a2a import AuditLog
            audit = AuditLog(sys.argv[1])
            for n in range(40):
                audit.record({"worker": sys.argv[2], "n": n, "pad": "中" * 200})
        """)
        procs = [subprocess.Popen([sys.executable, "-c", script, str(self.audit.path), str(w)],
                                  env=os.environ.copy(), stderr=subprocess.PIPE) for w in range(6)]
        for proc in procs:
            _, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err.decode())
        entries = self.audit.read()
        self.assertEqual(len(entries), 240)
        self.assertNotIn("CORRUPT_LINE", [e.get("state") for e in entries])
        for worker in range(6):
            self.assertEqual([e["n"] for e in entries if e["worker"] == str(worker)], list(range(40)))


if __name__ == "__main__":
    unittest.main()
