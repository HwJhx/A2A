"""19 号方案 §A:按进程身份发信号。真实 /proc 与 pidfd,只在支持的系统(Linux,VM)上运行。"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from a2a.procsignal import ProcInspectError, ProcSignaler

SUPPORTED = ProcSignaler().supported


@unittest.skipUnless(SUPPORTED, "需要 Linux 的 /proc 与 pidfd(在 VM 上运行)")
class RealProcesses(unittest.TestCase):
    def setUp(self):
        self.sig = ProcSignaler()

    def spawn(self, *argv0_and_args):
        proc = subprocess.Popen(list(argv0_and_args), start_new_session=True)   # 自成进程组,组长 = 它自己
        self.addCleanup(lambda: proc.poll() is None and (os.killpg(proc.pid, signal.SIGKILL), proc.wait()))
        return proc

    def test_snapshot_reads_start_time_pgid_and_argv0(self):
        proc = self.spawn("bash", "-c", "exec -a pi sleep 30")
        deadline = time.monotonic() + 5
        snap = self.sig.snapshot(proc.pid)
        while snap.argv0 != "pi" and time.monotonic() < deadline:   # 等 exec 完成
            time.sleep(0.05)
            snap = self.sig.snapshot(proc.pid)
        self.assertEqual((snap.pgid, snap.argv0), (proc.pid, "pi"))
        self.assertEqual(self.sig.snapshot(proc.pid), snap)            # 同一个进程,快照稳定
        other = self.spawn("sleep", "30")
        self.assertNotEqual(self.sig.snapshot(other.pid).start_time, None)

    def test_snapshot_handles_a_comm_with_spaces_and_parentheses(self):
        with tempfile.TemporaryDirectory() as d:
            odd = Path(d) / "a b) (c"
            odd.symlink_to("/bin/sleep")
            proc = self.spawn(str(odd), "30")
            time.sleep(0.2)
            snap = self.sig.snapshot(proc.pid)
            self.assertEqual((snap.pgid, snap.argv0), (proc.pid, str(odd)))

    def test_snapshot_of_a_missing_process_is_none(self):
        proc = self.spawn("true")
        proc.wait()
        self.assertIsNone(self.sig.snapshot(proc.pid))

    def test_group_members_lists_live_processes_of_the_group_but_not_zombies(self):
        proc = self.spawn("bash", "-c", "sleep 30 & sleep 30 & wait")
        deadline = time.monotonic() + 5
        while len(self.sig.group_members(proc.pid)) < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(len(self.sig.group_members(proc.pid)), 3)   # bash + 两个 sleep
        zombie = self.spawn("true")
        deadline = time.monotonic() + 5
        while Path(f"/proc/{zombie.pid}/stat").read_text().split(") ")[1][0] != "Z" and time.monotonic() < deadline:
            time.sleep(0.05)                                          # 退出但未回收:僵尸
        self.assertEqual(self.sig.group_members(zombie.pid), [])
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        deadline = time.monotonic() + 5
        while self.sig.group_members(proc.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.sig.group_members(proc.pid), [])

    def test_pidfd_signals_the_process_and_reports_it_gone_after_reap(self):
        proc = self.spawn("sleep", "30")
        fd = self.sig.open(proc.pid)
        try:
            self.sig.send(fd, signal.SIGTERM)
            self.assertEqual(proc.wait(timeout=5), -signal.SIGTERM)
            with self.assertRaises(ProcessLookupError):   # 已退出并回收:不会误发给复用了 pid 的进程
                self.sig.send(fd, signal.SIGTERM)
        finally:
            self.sig.close(fd)
        with self.assertRaises(ProcessLookupError):
            self.sig.open(proc.pid)


class InspectFailures(unittest.TestCase):
    """Codex 审核:无法检查(权限、hidepid)与"进程不在"要区分,前者 fail-closed。用假的 /proc 目录。"""

    def fake_proc(self, mount_options="rw,nosuid"):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        (d / "self").mkdir()
        (d / "self" / "stat").write_text("1 (x) S 0 1 1 0")
        (d / "self" / "mountinfo").write_text(f"36 25 0:5 / {d} rw,nosuid shared:13 - proc proc {mount_options}\n")
        return d

    def add(self, root, pid, pgid, state="S"):
        p = root / str(pid)
        p.mkdir()
        fields = [state, "1", str(pgid)] + ["0"] * 16 + ["123"]
        (p / "stat").write_text(f"{pid} (sleep) " + " ".join(fields))
        (p / "cmdline").write_bytes(b"sleep\0")
        return p

    def test_members_and_zombies_from_a_fake_proc(self):
        root = self.fake_proc()
        self.add(root, 10, 10)
        self.add(root, 11, 10, state="Z")
        self.add(root, 12, 99)
        self.assertEqual(ProcSignaler(proc_root=str(root), enabled=False).group_members(10), [10])

    def test_unreadable_entry_is_an_inspect_error_not_absence(self):
        root = self.fake_proc()
        p = self.add(root, 10, 10)
        (p / "stat").unlink()
        (p / "stat").mkdir()                     # 读它会抛 IsADirectoryError(OSError,非"不存在")
        sig = ProcSignaler(proc_root=str(root), enabled=False)
        with self.assertRaises(ProcInspectError):
            sig.group_members(10)
        with self.assertRaises(ProcInspectError):
            sig.snapshot(10)
        self.assertIsNone(sig.snapshot(77))      # 真不存在:None

    def test_hidepid_mount_fails_closed(self):
        for opts, hidden in (("rw,hidepid=2", True), ("rw,hidepid=invisible", True), ("rw,hidepid=0", False),
                             ("rw,nosuid", False)):
            with self.subTest(opts=opts):
                root = self.fake_proc(opts)
                self.add(root, 10, 10)
                sig = ProcSignaler(proc_root=str(root), enabled=False)
                if hidden:
                    with self.assertRaises(ProcInspectError):
                        sig.group_members(10)
                else:
                    self.assertEqual(sig.group_members(10), [10])


class Unsupported(unittest.TestCase):
    def test_disabled_signaler_is_not_supported(self):
        self.assertFalse(ProcSignaler.disabled().supported)
        self.assertFalse(ProcSignaler(proc_root="/nonexistent").supported)


if __name__ == "__main__":
    unittest.main()
