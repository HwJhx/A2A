"""阶段 8:注册表自动恢复(18 号方案 §4.1)。假 herdr,不启动真实会话。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_lifecycle import FakeHerdr  # noqa: E402

from a2a import ipdrain  # noqa: E402
from a2a.audit import AuditLog  # noqa: E402
from a2a.broker import Broker, BrokerAlreadyRunning  # noqa: E402
from a2a.errors import HerdrError  # noqa: E402
from a2a.lifecycle import Lifecycle  # noqa: E402
from a2a.messages import QUEUED, TARGET_MISSING, Message, new_msg_id, now_iso  # noqa: E402
from a2a.recovery import BrokerRunning, Recovery, broker_lock  # noqa: E402
from a2a.registry import Registry  # noqa: E402
from a2a.spool import Spool  # noqa: E402
from a2a.topology import TopologyStore  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        d = self.dir = Path(temp.name)
        launcher = d / "fnx_fake"
        launcher.write_text("#!/bin/bash\nexport X=1\nexec /bin/sleep 100 \"$@\"\n")
        self.store = TopologyStore.create(d / "topology.yaml", {
            "version": 1, "project_id": "soc_a", "workspace_label": "SoC-A",
            "roles": {"dv": {"label": "验证", "launcher": str(launcher)},
                      "sw": {"label": "软件", "launcher": str(launcher)}},
            "ips": ["uart", "gpio"], "edges": []})
        self.registry = Registry(d / "registry.json")
        self.audit = AuditLog(d / "audit.jsonl")
        self.spool = Spool(d / "spool")
        self.herdr = FakeHerdr()
        self.life = Lifecycle(client=self.herdr, topology=self.store, registry=self.registry, audit=self.audit,
                              state_dir=d, kill=self.fake_kill, sleep=lambda s: None)
        for role in ("dv", "sw"):
            for ip in ("uart", "gpio"):
                self.life.spawn(role, ip, cwd=str(d / f"work_{ip}"))

    def fake_kill(self, pgid, sig):
        pane = next(p for p, i in self.herdr.process.items() if i and i["foreground_process_group_id"] == pgid)
        self.herdr.agents.pop(pane, None)
        self.herdr.process[pane] = None

    def recovery(self, session="s1"):
        return Recovery(lifecycle=self.life, registry=self.registry, spool=self.spool, audit=self.audit,
                        client=self.herdr, state_dir=self.dir, session=session, role_order=["dv", "sw"])

    def restart_session(self):
        """herdr 会话重启(18 号方案 §3.1 实测):pane 与 pane ID 保留,进程退出只剩 shell,agent 名字丢失。"""
        self.herdr.agents.clear()
        for pane in self.herdr.process:
            self.herdr.process[pane] = None


class Classify(Base):
    def test_after_a_session_restart_all_running_agents_are_restored_in_their_panes(self):
        panes = {r.agent_id: r.pane_id for r in self.registry.list()}
        self.restart_session()
        out = self.recovery().recover()
        self.assertEqual(set(out["agents"].values()), {f"restored({p})" for p in panes.values()})
        self.assertEqual(out["problems"], {})
        for r in self.registry.list():
            self.assertEqual((r.lifecycle, r.pane_id), ("running", panes[r.agent_id]))   # 原 pane 重启
            self.assertEqual(self.herdr.agent_get(r.agent_id)["pane_id"], r.pane_id)      # 重新改名
        self.assertEqual(len([e for e in self.audit.read() if e.get("state") == "AGENTS_RECOVERED"]), 1)

    def test_running_agents_are_left_alone(self):
        calls = len(self.herdr.calls)
        out = self.recovery().recover()
        self.assertEqual(set(out["agents"].values()), {"ok"})
        self.assertFalse(any(c[0] in ("pane_run", "agent_rename") for c in self.herdr.calls[calls:]))

    def test_classification_of_each_case(self):
        self.restart_session()
        reg = {r.agent_id: r for r in self.registry.list()}
        # 不确定:pane 前台被别的进程占用
        self.herdr.process[reg["dv_uart"].pane_id] = {"foreground_process_group_id": 5555, "shell_pid": 4000,
                                                      "foreground_processes": [{"argv": ["vim"], "pid": 5555}]}
        # 不确定:名字出现在别的 pane 上
        self.herdr.agents[reg["sw_uart"].pane_id + "x"] = {"pane_id": "w1:other", "agent": "pi",
                                                          "agent_status": "idle", "name": "sw_uart"}
        # 按原意保留:stopped / closed;报出:failed
        self.registry.update_runtime("dv_gpio", lifecycle="stopped")
        self.registry.update_runtime("sw_gpio", lifecycle="closed")
        out = self.recovery().recover()
        self.assertEqual(out["agents"], {"dv_uart": "uncertain", "sw_uart": "uncertain",
                                         "dv_gpio": "kept_stopped", "sw_gpio": "kept_closed"})
        self.assertEqual(set(out["problems"]), {"dv_uart", "sw_uart"})

    def test_include_stopped_still_checks_the_pane(self):
        self.restart_session()
        reg = {r.agent_id: r for r in self.registry.list()}
        self.registry.update_runtime("dv_gpio", lifecycle="stopped")
        self.registry.update_runtime("sw_gpio", lifecycle="stopped")
        self.herdr.process[reg["sw_gpio"].pane_id] = {"foreground_process_group_id": 4000, "shell_pid": 4000}  # 不完整
        out = self.recovery().recover(ip="gpio", include_stopped=True)
        self.assertTrue(out["agents"]["dv_gpio"].startswith("restored"))
        self.assertEqual(out["agents"]["sw_gpio"], "uncertain")
        self.assertEqual(self.registry.get("sw_gpio").lifecycle, "stopped")

    def test_other_sessions_are_not_queried_or_restored(self):
        self.restart_session()
        self.registry.update_runtime("dv_uart", session="other")
        calls = len(self.herdr.calls)
        out = self.recovery().recover(ip="uart")
        self.assertEqual(out["agents"]["dv_uart"], "other_session")
        self.assertEqual(self.registry.get("dv_uart").lifecycle, "running")
        self.assertFalse(any(c[0] == "agent_rename" and c[2] == "dv_uart" for c in self.herdr.calls[calls:]))

    def test_draining_ip_is_skipped(self):
        self.restart_session()
        with ipdrain.drain_exclusive(self.dir, "gpio"):
            ipdrain.write_marker(self.dir, "gpio", "op1", "测试")
        out = self.recovery().recover()
        self.assertEqual({out["agents"]["dv_gpio"], out["agents"]["sw_gpio"]}, {"skipped_draining"})
        self.assertTrue(out["agents"]["dv_uart"].startswith("restored"))

    def test_failed_records_are_reported_and_one_failure_does_not_stop_others(self):
        self.restart_session()
        self.registry.update_runtime("dv_uart", lifecycle="failed")
        original = self.life.restore

        def flaky(role, ip, cwd=None):
            if (role, ip) == ("sw", "uart"):
                raise RuntimeError("启动失败")
            return original(role, ip, cwd=cwd)
        self.life.restore = flaky
        out = self.recovery().recover()
        self.assertEqual(out["agents"]["dv_uart"], "failed_record")
        self.assertTrue(out["agents"]["sw_uart"].startswith("restore_failed"))
        self.assertTrue(out["agents"]["dv_gpio"].startswith("restored"))
        self.assertTrue(out["agents"]["sw_gpio"].startswith("restored"))
        self.assertEqual(set(out["problems"]), {"dv_uart", "sw_uart"})

    def test_dry_run_changes_nothing(self):
        self.restart_session()
        before = [(r.agent_id, r.lifecycle, r.pane_id) for r in self.registry.list()]
        calls = len(self.herdr.calls)
        out = self.recovery().recover(dry_run=True)
        self.assertTrue(out["dry_run"])
        self.assertEqual(set(out["agents"].values()), {"restore"})
        self.assertEqual([(r.agent_id, r.lifecycle, r.pane_id) for r in self.registry.list()], before)
        self.assertFalse(any(c[0] in ("pane_run", "agent_rename") for c in self.herdr.calls[calls:]))

    def test_queue_hints_list_paused_heads(self):
        self.restart_session()
        m = self.spool.enqueue(Message(msg_id=new_msg_id(), created_at=now_iso(), edge_id="e", src="dv_uart",
                                       dst="sw_uart", project_id="soc_a", ip_id="uart", session="s1", text="x",
                                       state=QUEUED, topology_revision="r"))
        self.spool.update(m.msg_id, state=TARGET_MISSING)
        out = self.recovery().recover()
        (hint,) = out["queue_hints"]
        self.assertEqual((hint["dst"], hint["msg_id"], hint["state"]), ("sw_uart", m.msg_id, TARGET_MISSING))
        self.assertIn(f"a2a resolve {m.msg_id} retry", hint["suggest"])


class PaneGone(Base):
    """Codex 审核:pane 不在时新建 pane,必须用登记的工作目录;没有登记就归为不确定。"""

    def drop_pane(self, agent_id):
        pane = self.registry.get(agent_id).pane_id
        del self.herdr.panes[pane]
        return pane

    def test_pane_gone_restores_into_a_new_pane_in_the_recorded_cwd(self):
        self.restart_session()
        old = self.drop_pane("dv_uart")
        out = self.recovery().recover(ip="uart")
        new = self.registry.get("dv_uart").pane_id
        self.assertNotEqual(new, old)
        self.assertEqual(out["agents"]["dv_uart"], f"restored({new})")
        self.assertEqual(self.herdr.panes[new]["cwd"], str(self.dir / "work_uart"))
        self.assertIn("新建 pane", out["notes"]["dv_uart"])

    def test_pane_gone_without_a_recorded_cwd_is_uncertain(self):
        self.restart_session()
        self.drop_pane("dv_uart")
        data = json.loads(self.registry.path.read_text())       # 旧注册表:记录里没有 cwd
        del data["agents"]["dv_uart"]["cwd"]
        self.registry.path.write_text(json.dumps(data))
        panes = set(self.herdr.panes)
        out = self.recovery().recover(ip="uart")
        self.assertEqual(out["agents"]["dv_uart"], "uncertain")
        self.assertIn("工作目录", out["notes"]["dv_uart"])
        self.assertIn("dv_uart", out["problems"])
        self.assertEqual(self.registry.get("dv_uart").lifecycle, "running")
        self.assertTrue(out["agents"]["sw_uart"].startswith("restored"))
        self.assertEqual(set(self.herdr.panes), panes)          # 没有为 dv_uart 新建 pane

    def test_restart_in_the_original_pane_ignores_the_cwd_field(self):
        # herdr 会话重启后保留 pane 的工作目录(实测),原 pane 重启不需要 cwd;旧记录也能恢复
        self.restart_session()
        data = json.loads(self.registry.path.read_text())
        del data["agents"]["dv_uart"]["cwd"]
        self.registry.path.write_text(json.dumps(data))
        self.assertTrue(self.recovery().recover(ip="uart")["agents"]["dv_uart"].startswith("restored"))


class ClassificationErrors(Base):
    """Codex 审核:查询 herdr 临时出错只影响这一个 agent,其他继续,审计照写。"""

    def test_one_herdr_error_does_not_abort_the_run(self):
        self.restart_session()
        original = self.herdr.pane_process_info
        target = self.registry.get("sw_uart").pane_id

        def flaky(pane_id):
            if pane_id == target:
                raise HerdrError("socket 临时错误")
            return original(pane_id)
        self.herdr.pane_process_info = flaky
        out = self.recovery().recover()
        self.assertEqual(out["agents"]["sw_uart"], "classification_failed")
        self.assertIn("socket 临时错误", out["notes"]["sw_uart"])
        self.assertIn("sw_uart", out["problems"])
        for agent_id in ("dv_uart", "dv_gpio", "sw_gpio"):
            self.assertTrue(out["agents"][agent_id].startswith("restored"), out["agents"][agent_id])
        (event,) = [e for e in self.audit.read() if e.get("state") == "AGENTS_RECOVERED"]
        self.assertEqual(event["agents"]["sw_uart"], "classification_failed")

    def test_dry_run_also_reports_classification_errors(self):
        self.restart_session()

        def broken(name):
            raise HerdrError("socket 临时错误")
        self.herdr.agent_get = broken
        out = self.recovery().recover(dry_run=True)
        self.assertEqual(set(out["agents"].values()), {"classification_failed"})


class BrokerLock(Base):
    def test_refused_while_a_broker_holds_the_lock(self):
        broker = Broker(spool=self.spool, registry=self.registry, audit=self.audit, client=self.herdr,
                        session="s1", state_dir=self.dir, semantics_verified=True)
        broker.acquire()
        self.addCleanup(broker.release_lock)
        self.restart_session()
        with self.assertRaises(BrokerRunning):
            self.recovery().recover()
        self.assertEqual(self.registry.get("dv_uart").lifecycle, "running")
        self.assertEqual(set(self.recovery().recover(dry_run=True)["agents"].values()), {"restore"})  # dry-run 不拿锁

    def test_broker_cannot_start_while_recover_holds_the_lock(self):
        broker = Broker(spool=self.spool, registry=self.registry, audit=self.audit, client=self.herdr,
                        session="s1", state_dir=self.dir, semantics_verified=True)
        with broker_lock(self.dir):
            with self.assertRaises(BrokerAlreadyRunning):
                broker.acquire()
        broker.acquire()
        broker.release_lock()


if __name__ == "__main__":
    unittest.main()
