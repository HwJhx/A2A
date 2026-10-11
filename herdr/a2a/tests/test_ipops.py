"""阶段 8:按 IP 增删与排空隔离(17 号方案)。假 herdr,不启动真实会话。"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_lifecycle import FakeHerdr  # noqa: E402

from a2a import ipdrain  # noqa: E402
from a2a.audit import AuditLog  # noqa: E402
from a2a.identity import AgentIdentity, identity_env  # noqa: E402
from a2a.ipops import IpOpError, IpOps  # noqa: E402
from a2a.lifecycle import Lifecycle  # noqa: E402
from a2a.messages import DELIVERED, DISPATCHING, FAILED, QUEUED, WAITING_TARGET, Message, new_msg_id, now_iso  # noqa: E402
from a2a.registry import Registry  # noqa: E402
from a2a.router import REJECT_IP_DRAINING, Router, SendRejected  # noqa: E402
from a2a.spool import Spool  # noqa: E402
from a2a.topology import TopologyStore  # noqa: E402

SESSION = "s1"


class Herdr(FakeHerdr):
    def find_agent(self, pane_id):
        return self.agents.get(pane_id)

    def pane_process_info(self, pane_id):
        from a2a.errors import HerdrNotFound
        if pane_id not in self.panes:
            raise HerdrNotFound("x", code="pane_not_found")
        # 没启动过进程(或进程已退出)的 pane:前台只有 shell
        return self.process.get(pane_id) or {"foreground_process_group_id": 4000, "shell_pid": 4000,
                                             "foreground_processes": [{"argv": ["bash"], "pid": 4000}]}


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        d = self.dir = Path(temp.name)
        launcher = d / "fnx_fake"
        launcher.write_text("#!/bin/bash\nexport X=1\nexec /bin/sleep 100 \"$@\"\n")
        self.store = TopologyStore.create(d / "topology.yaml", {
            "version": 1, "project_id": "soc_a", "workspace_label": "SoC-A",
            "roles": {"spec": {"label": "Spec"}, "dv": {"label": "验证", "launcher": str(launcher)},
                      "sw": {"label": "软件", "launcher": str(launcher)}},
            "ips": ["uart"], "edges": [{"id": "dv_done", "from": "dv", "to": "sw", "template": "{ip} 完成"}]})
        self.registry = Registry(d / "registry.json")
        self.audit = AuditLog(d / "audit.jsonl")
        self.spool = Spool(d / "spool")
        self.herdr = Herdr()
        self.life = Lifecycle(client=self.herdr, topology=self.store, registry=self.registry, audit=self.audit,
                              state_dir=d, sleep=lambda s: None)
        self.points = []
        self.fail_at = None
        self.ops = self.make_ops()

    def make_ops(self, **kw):
        def checkpoint(name):
            self.points.append(name)
            if name == self.fail_at:
                raise RuntimeError(f"模拟中断:{name}")
        return IpOps(lifecycle=self.life, topology=self.store, registry=self.registry, spool=self.spool,
                     audit=self.audit, client=self.herdr, state_dir=self.dir, poll_s=0.01,
                     checkpoint=checkpoint, **kw)

    def events(self, state):
        return [e for e in self.audit.read() if e.get("state") == state]

    def router(self):
        return Router(self.store, self.registry, self.spool, self.audit, session=SESSION)

    def send_env(self, role, ip):
        record = self.registry.find(role, ip)
        return dict(identity_env("soc_a", role, ip), HERDR_PANE_ID=record.pane_id, HERDR_SESSION=SESSION)

    def enqueue(self, ip="gpio", state=QUEUED):
        m = self.spool.enqueue(Message(msg_id=new_msg_id(), created_at=now_iso(), edge_id="dv_done", src=f"dv_{ip}",
                                       dst=f"sw_{ip}", project_id="soc_a", ip_id=ip, session=SESSION, text="x",
                                       state=QUEUED, topology_revision="r"))
        if state != QUEUED:
            self.spool.update(m.msg_id, state=state)
        return m


class Add(Base):
    def test_adds_ip_and_spawns_roles_with_launchers_in_order(self):
        out = self.ops.add("gpio")
        self.assertIn("gpio", self.store.current().ips)
        self.assertEqual(out["roles"], {"spec": "no_launcher", "dv": "spawned", "sw": "spawned"})
        self.assertEqual([r.agent_id for r in self.registry.list() if r.ip_id == "gpio"], ["dv_gpio", "sw_gpio"])
        self.assertEqual(len(self.events("IP_ADDED")), 1)
        self.assertEqual(len(self.events("TOPOLOGY_CHANGED")), 1)

    def test_rerun_skips_running_and_reports_stopped_without_restoring(self):
        self.ops.add("gpio")
        self.registry.update_runtime("sw_gpio", lifecycle="stopped")
        out = self.ops.add("gpio")
        self.assertEqual(out["roles"]["dv"], "already_running")
        self.assertTrue(out["roles"]["sw"].startswith("registered_stopped"))
        self.assertEqual(self.registry.get("sw_gpio").lifecycle, "stopped")

    def test_failure_stops_without_rollback_and_a_rerun_completes(self):
        original = self.life.spawn
        calls = []

        def flaky(role, ip, cwd=None):
            calls.append(role)
            if role == "sw" and calls.count("sw") == 1:
                raise RuntimeError("启动失败")
            return original(role, ip, cwd=cwd)
        self.life.spawn = flaky
        with self.assertRaises(IpOpError) as ctx:
            self.ops.add("gpio")
        self.assertTrue(ctx.exception.detail["roles"]["sw"].startswith("failed"))
        self.assertIn("gpio", self.store.current().ips)                      # 不回滚
        self.assertIsNotNone(self.registry.find("dv", "gpio"))
        out = self.ops.add("gpio")
        self.assertEqual(out["roles"], {"spec": "no_launcher", "dv": "already_running", "sw": "spawned"})

    def test_cwd_placeholders_are_expanded_and_created(self):
        self.ops.add("gpio", roles=["dv"], cwd=str(self.dir / "work" / "{ip}" / "{role}"))
        self.assertTrue((self.dir / "work" / "gpio" / "dv").is_dir())

    def test_stale_running_record_is_reported_not_treated_as_running(self):
        # Codex 审核:注册表说 running 但 herdr 里已经没有这个 agent(进程退出)
        self.ops.add("gpio")
        self.herdr.agents.pop(self.registry.find("sw", "gpio").pane_id)
        out = self.ops.add("gpio")
        self.assertEqual(out["roles"]["dv"], "already_running")
        self.assertTrue(out["roles"]["sw"].startswith("registered_running_but_missing"))

    def test_running_record_whose_name_is_on_another_pane_is_reported(self):
        # Codex 复核:pane 上有 agent 但不是登记的那个(名字对不上 / 名字在别的 pane)
        self.ops.add("gpio")
        sw_pane = self.registry.find("sw", "gpio").pane_id
        dv_pane = self.registry.find("dv", "gpio").pane_id
        self.herdr.agents[sw_pane]["name"] = "someone_else"
        self.herdr.agents[dv_pane]["name"] = "sw_gpio"                   # 登记名字跑到了别的 pane 上
        out = self.ops.add("gpio")
        self.assertTrue(out["roles"]["sw"].startswith("registered_running_but_mismatched"), out)

    def test_add_reports_herdr_errors_other_than_not_found(self):
        # Codex 复核:herdr 服务不可用等错误不能当作 agent 不存在
        from a2a.errors import HerdrError
        self.ops.add("gpio")

        def broken(target):
            raise HerdrError("server not running", code="server_not_running")
        self.herdr.agent_get = broken
        with self.assertRaises(HerdrError):
            self.ops.add("gpio")

    def test_add_is_refused_while_draining(self):
        with ipdrain.drain_exclusive(self.dir, "gpio"):
            ipdrain.write_marker(self.dir, "gpio", "op1", "测试")
        with self.assertRaises(IpOpError):
            self.ops.add("gpio")


class Remove(Base):
    def setUp(self):
        super().setUp()
        self.ops.add("gpio")

    def test_removes_agents_then_topology_and_clears_the_marker(self):
        out = self.ops.remove("gpio")
        self.assertTrue(out["removed"])
        self.assertEqual([r for r in self.registry.list() if r.ip_id == "gpio"], [])
        self.assertNotIn("gpio", self.store.current().ips)
        self.assertFalse(ipdrain.is_draining(self.dir, "gpio"))
        self.assertEqual(self.points[-3:], ["purged:sw_gpio", "topology_removed", "audited"])
        self.assertEqual(len(self.events("IP_REMOVED")), 1)

    def test_pending_or_unreleased_messages_refuse_and_restore_normal(self):
        cases = {"待投递": QUEUED, "已归档但未放行": FAILED}
        for name, state in cases.items():
            with self.subTest(name):
                m = self.enqueue(state=state)
                with self.assertRaises(IpOpError) as ctx:
                    self.ops.remove("gpio")
                self.assertIn("sw_gpio", ctx.exception.detail["problems"])
                self.assertFalse(ipdrain.is_draining(self.dir, "gpio"))     # 撤销排空、恢复正常
                self.assertIn("gpio", self.store.current().ips)
                self.assertIsNotNone(self.registry.find("dv", "gpio"))
                if state == QUEUED:
                    self.spool.update(m.msg_id, state=DISPATCHING)
                    self.spool.update(m.msg_id, state=DELIVERED)
                self.spool.release("sw_gpio", m.queue_seq, reason="测试清理")
        self.assertEqual(len(self.events("IP_DRAIN_CANCELLED")), 2)
        self.ops.remove("gpio")                                              # 处理完后能删除

    def test_unreadable_queue_state_refuses(self):
        m = self.enqueue()
        self.spool.update(m.msg_id, state=DISPATCHING)
        self.spool.update(m.msg_id, state=DELIVERED)
        self.spool.release("sw_gpio", m.queue_seq, reason="x")
        (self.dir / "spool" / "queues" / "sw_gpio.json").write_text("{坏")
        with self.assertRaises(IpOpError):
            self.ops.remove("gpio")
        self.assertFalse(ipdrain.is_draining(self.dir, "gpio"))

    def test_dispatching_timeout_is_fail_closed(self):
        m = self.enqueue()
        self.spool.update(m.msg_id, state=DISPATCHING)
        ops = self.make_ops(dispatch_timeout_s=0.05)
        with self.assertRaises(IpOpError):
            ops.remove("gpio")
        self.assertTrue(ipdrain.is_draining(self.dir, "gpio"))             # 标记保留,IP 保持隔离
        self.assertIsNotNone(self.registry.find("dv", "gpio"))             # 没有 purge
        self.assertIn("gpio", self.store.current().ips)

    def test_busy_agent_timeout_is_fail_closed_and_force_skips_only_that(self):
        pane = self.registry.find("dv", "gpio").pane_id
        self.herdr.agents[pane]["agent_status"] = "working"
        with self.assertRaises(IpOpError):
            self.make_ops(idle_timeout_s=0.05).remove("gpio")
        self.assertTrue(ipdrain.is_draining(self.dir, "gpio"))
        self.make_ops(idle_timeout_s=0.05).remove("gpio", force=True)     # 续做;--force 不等空闲
        self.assertNotIn("gpio", self.store.current().ips)

    def test_agent_herdr_cannot_find_counts_as_busy(self):
        # Codex 审核:查不到状态不能当空闲(fail-closed);进程确实已退出时用 --force
        pane = self.registry.find("dv", "gpio").pane_id
        self.herdr.agents.pop(pane)
        self.herdr.process.pop(pane)          # 进程确实已退出:前台只剩 shell
        with self.assertRaises(IpOpError) as ctx:
            self.make_ops(idle_timeout_s=0.05).remove("gpio")
        self.assertIn("herdr 查不到", str(ctx.exception))
        self.assertIsNotNone(self.registry.find("dv", "gpio"))
        self.make_ops(idle_timeout_s=0.05).remove("gpio", force=True)
        self.assertNotIn("gpio", self.store.current().ips)

    def test_replaced_agent_on_the_pane_refuses_removal_even_with_force(self):
        # Codex 复核:pane 上换成了别的(空闲的)agent,删除会误杀它;--force 也不跳过身份核验
        pane = self.registry.find("sw", "gpio").pane_id
        self.herdr.agents[pane]["name"] = "intruder"
        for force in (False, True):
            with self.subTest(force=force):
                with self.assertRaises(IpOpError) as ctx:
                    self.make_ops(idle_timeout_s=0.05).remove("gpio", force=force)
                self.assertIn("sw_gpio", str(ctx.exception.detail["mismatched"]))
                self.assertTrue(ipdrain.is_draining(self.dir, "gpio"))     # 标记保留
                self.assertIn(pane, self.herdr.panes)                       # 没有关 pane
                self.assertIsNotNone(self.registry.find("sw", "gpio"))
        self.ops.undrain("gpio")

    def test_unrecognized_foreground_process_refuses_removal_even_with_force(self):
        # Codex 复核:herdr 认不出 agent,但 pane 前台仍有进程 -> 关 pane 会误杀它
        self.herdr.agents.pop(self.registry.find("dv", "gpio").pane_id)   # 进程还在(process 未清)
        with self.assertRaises(IpOpError) as ctx:
            self.make_ops(idle_timeout_s=0.05).remove("gpio", force=True)
        self.assertIn("dv_gpio", str(ctx.exception.detail["mismatched"]))
        self.assertTrue(ipdrain.is_draining(self.dir, "gpio"))
        self.assertIsNotNone(self.registry.find("dv", "gpio"))

    def test_stopped_record_whose_pane_is_occupied_refuses_removal(self):
        # Codex 复核:记录是 stopped,但 pane 里已经跑着别的进程
        pane = self.registry.find("sw", "gpio").pane_id
        self.registry.update_runtime("sw_gpio", lifecycle="stopped")
        self.herdr.agents.pop(pane)
        self.herdr.process[pane] = {"foreground_process_group_id": 5555, "shell_pid": 4000,
                                    "foreground_processes": [{"argv": ["vim"], "pid": 5555}]}
        with self.assertRaises(IpOpError) as ctx:
            self.make_ops(idle_timeout_s=0.05).remove("gpio", force=True)
        self.assertIn("sw_gpio", str(ctx.exception.detail["mismatched"]))
        self.assertIn(pane, self.herdr.panes)
        self.herdr.process[pane] = None       # 只剩 shell 时可以删除
        self.make_ops(idle_timeout_s=0.05).remove("gpio")
        self.assertNotIn("gpio", self.store.current().ips)

    def test_incomplete_or_contradictory_process_info_refuses_removal(self):
        # Codex 复核:只有明确确认 pane 只剩 shell 才放行;字段缺失 / 矛盾一律拒绝
        pane = self.registry.find("sw", "gpio").pane_id
        self.registry.update_runtime("sw_gpio", lifecycle="stopped")
        self.herdr.agents.pop(pane)
        cases = {
            "缺少前台进程列表": {"foreground_process_group_id": 4000, "shell_pid": 4000},
            "前台进程列表为空": {"foreground_process_group_id": 4000, "shell_pid": 4000, "foreground_processes": []},
            "进程组是 shell 但列表里有别的进程": {"foreground_process_group_id": 4000, "shell_pid": 4000,
                                          "foreground_processes": [{"argv": ["vim"], "pid": 5555}]},
            "缺少 shell_pid": {"foreground_process_group_id": 4000,
                             "foreground_processes": [{"argv": ["bash"], "pid": 4000}]},
        }
        for name, info in cases.items():
            with self.subTest(name):
                self.herdr.process[pane] = info
                with self.assertRaises(IpOpError) as ctx:
                    self.make_ops(idle_timeout_s=0.05).remove("gpio", force=True)
                self.assertIn("sw_gpio", str(ctx.exception.detail["mismatched"]))
                self.assertIn(pane, self.herdr.panes)
        self.herdr.process[pane] = {"foreground_process_group_id": 4000, "shell_pid": 4000,
                                    "foreground_processes": [{"argv": ["/bin/bash"], "pid": 4000}]}
        self.make_ops(idle_timeout_s=0.05).remove("gpio")          # 明确只剩 shell(与实测一致)才放行
        self.assertNotIn("gpio", self.store.current().ips)

    def test_force_still_waits_for_dispatching(self):
        m = self.enqueue()
        self.spool.update(m.msg_id, state=DISPATCHING)
        with self.assertRaises(IpOpError):
            self.make_ops(dispatch_timeout_s=0.05).remove("gpio", force=True)

    def test_interrupted_at_each_step_a_rerun_completes_with_one_ip_removed(self):
        for point in ("purged:dv_gpio", "topology_removed", "audited"):
            with self.subTest(point):
                if "gpio" not in self.store.current().ips:
                    self.ops.add("gpio")
                before = len(self.events("IP_REMOVED"))
                self.fail_at = point
                with self.assertRaises(RuntimeError):
                    self.ops.remove("gpio")
                self.assertTrue(ipdrain.is_draining(self.dir, "gpio"))
                self.fail_at = None
                self.ops.remove("gpio")
                self.assertNotIn("gpio", self.store.current().ips)
                self.assertFalse(ipdrain.is_draining(self.dir, "gpio"))
                self.assertEqual(len(self.events("IP_REMOVED")) - before, 1)

    def test_nonexistent_ip_is_refused(self):
        with self.assertRaises(IpOpError):
            self.ops.remove("spi")
        self.assertFalse(ipdrain.is_draining(self.dir, "spi"))

    def test_undrain_restores_normal(self):
        with ipdrain.drain_exclusive(self.dir, "gpio"):
            ipdrain.write_marker(self.dir, "gpio", "op1", "测试")
        self.assertTrue(self.ops.undrain("gpio")["undrained"])
        self.assertFalse(ipdrain.is_draining(self.dir, "gpio"))
        self.assertEqual(len(self.events("IP_UNDRAINED")), 1)


class LifecycleGate(Base):
    """Codex 审核:a2a agent spawn / restore 不能绕过 IP 排空。"""

    def test_spawn_and_restore_are_refused_while_draining(self):
        from a2a.lifecycle import LifecycleError
        self.ops.add("gpio")
        with ipdrain.drain_exclusive(self.dir, "uart"):
            ipdrain.write_marker(self.dir, "uart", "op1", "测试")
        with self.assertRaises(LifecycleError):
            self.life.spawn("dv", "uart")
        self.registry.update_runtime("sw_gpio", lifecycle="closed")
        with ipdrain.drain_exclusive(self.dir, "gpio"):
            ipdrain.write_marker(self.dir, "gpio", "op2", "测试")
        with self.assertRaises(LifecycleError):
            self.life.restore("sw", "gpio")

    def test_spawn_rereads_topology_after_taking_the_gate(self):
        # Codex 复核:调用方先读到 IP 存在、还没拿到门禁时 ip remove 完成(拓扑已删、标记已清),
        # 拿到门禁后必须重新读拓扑并拒绝,不能用过期拓扑拉起 agent
        from a2a.lifecycle import LifecycleError
        self.ops.add("gpio", roles=["dv"])
        errors = []
        lock_held = threading.Event()
        release = threading.Event()

        def holder():
            with ipdrain.drain_exclusive(self.dir, "gpio"):
                lock_held.set()
                release.wait(5)
        h = threading.Thread(target=holder)
        h.start()
        self.assertTrue(lock_held.wait(5))

        def spawn():
            try:
                self.life.spawn("sw", "gpio")
            except LifecycleError as exc:
                errors.append(str(exc))
        t = threading.Thread(target=spawn)
        t.start()
        time.sleep(0.3)                     # spawn 卡在门禁上
        self.life.purge("dv", "gpio")       # 模拟 ip remove 在此期间完成
        self.store.remove_ip("gpio")
        release.set()
        t.join(5)
        h.join(5)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("不在拓扑里", errors[0])
        self.assertIsNone(self.registry.find("sw", "gpio"))

    @unittest.skipIf(os.geteuid() == 0, "root 不受文件权限限制")
    def test_marker_check_failure_counts_as_draining(self):
        # Codex 复核:检查标记本身失败(权限 / 路径错误)不能当作"没有标记"
        d = self.dir / "ip_draining"
        d.mkdir()
        d.chmod(0)
        self.addCleanup(d.chmod, 0o755)
        self.assertTrue(ipdrain.is_draining(self.dir, "uart"))
        self.assertIn("unreadable", ipdrain.read_marker(self.dir, "uart"))
        from a2a.lifecycle import LifecycleError
        with self.assertRaises(LifecycleError):
            self.life.spawn("dv", "uart")

    def test_marker_waits_for_a_spawn_in_progress_and_remove_purges_it(self):
        # 启动进行中(持共享锁)时写标记的一方要等它结束;随后 remove 的 purge 包括这个新 agent
        started, release = threading.Event(), threading.Event()
        original = self.herdr.pane_run

        def slow_run(pane_id, command):
            started.set()
            release.wait(5)
            return original(pane_id, command)
        self.herdr.pane_run = slow_run
        spawner = threading.Thread(target=self.life.spawn, args=("dv", "uart"))
        spawner.start()
        self.assertTrue(started.wait(5))
        remover = threading.Thread(target=lambda: self.ops.remove("uart"))
        remover.start()
        time.sleep(0.3)
        self.assertFalse(ipdrain.is_draining(self.dir, "uart"))         # 标记还没写:在等启动结束
        release.set()
        spawner.join(5)
        remover.join(10)
        self.assertEqual([r for r in self.registry.list() if r.ip_id == "uart"], [])
        self.assertNotIn("uart", self.store.current().ips)


class RouterDrain(Base):
    def setUp(self):
        super().setUp()
        self.ops.add("gpio")
        self.ops.add("uart")

    def test_draining_ip_is_rejected_and_other_ips_are_not(self):
        with ipdrain.drain_exclusive(self.dir, "gpio"):
            ipdrain.write_marker(self.dir, "gpio", "op1", "测试")
        with self.assertRaises(SendRejected) as ctx:
            self.router().send("dv_done", self.send_env("dv", "gpio"))
        self.assertEqual(ctx.exception.code, REJECT_IP_DRAINING)
        self.assertEqual(self.spool.pending("sw_gpio"), [])
        self.router().send("dv_done", self.send_env("dv", "uart"))       # 其他 IP 照常
        self.assertEqual(len(self.spool.pending("sw_uart")), 1)

    def test_marker_waits_for_an_enqueue_that_already_checked(self):
        # 窗口 ①:Router 持共享锁、检查完标记、尚未入队时,写标记的一方必须等它入队完成
        checked, release = threading.Event(), threading.Event()
        original = self.spool.enqueue

        def slow_enqueue(message):
            checked.set()
            release.wait(5)
            return original(message)
        self.spool.enqueue = slow_enqueue
        sender = threading.Thread(target=self.router().send, args=("dv_done", self.send_env("dv", "gpio")))
        sender.start()
        self.assertTrue(checked.wait(5))
        wrote = []

        def drain():
            with ipdrain.drain_exclusive(self.dir, "gpio"):
                wrote.append(len(self.spool.pending("sw_gpio")))
                ipdrain.write_marker(self.dir, "gpio", "op1", "测试")
        drainer = threading.Thread(target=drain)
        drainer.start()
        time.sleep(0.3)
        self.assertEqual(wrote, [])                                       # 独占锁在等共享锁
        release.set()
        sender.join(5)
        drainer.join(5)
        self.assertEqual(wrote, [1])                                      # 拿到独占锁时消息已入队、可见

    def test_marker_and_dispatch_guard_exclude_each_other(self):
        # 窗口 ②:broker 持共享锁、检查完、尚未写 DISPATCHING 时,写标记的一方必须等它写完
        m = self.enqueue()
        from a2a.delivery import DeliveryEngine
        holding, release = threading.Event(), threading.Event()

        def guard(message):
            from contextlib import contextmanager

            @contextmanager
            def g():
                with ipdrain.drain_shared(self.dir, message.ip_id):
                    holding.set()
                    release.wait(5)
                    yield
            return g()
        engine = DeliveryEngine(self.spool, self.registry, self.audit, self.herdr, semantics_verified=True,
                                skip=lambda msg: ipdrain.is_draining(self.dir, msg.ip_id), dispatch_guard=guard)
        self.herdr.agent_prompt = lambda *a, **k: {"agent_status": "working"}
        worker = threading.Thread(target=engine.deliver, args=(m.msg_id,))
        worker.start()
        self.assertTrue(holding.wait(5))
        seen = []

        def drain():
            with ipdrain.drain_exclusive(self.dir, "gpio"):
                seen.append(self.spool.get(m.msg_id).state)
                ipdrain.write_marker(self.dir, "gpio", "op1", "测试")
        drainer = threading.Thread(target=drain)
        drainer.start()
        time.sleep(0.3)
        self.assertEqual(seen, [])
        release.set()
        drainer.join(5)
        worker.join(5)
        self.assertIn(seen[0], (DISPATCHING, DELIVERED))                  # 拿到独占锁时 DISPATCHING 已落盘

    def test_engine_does_not_dispatch_after_the_marker(self):
        # 窗口 ③:标记之后开始的投递不写 DISPATCHING,消息保持原状态
        m = self.enqueue()
        from a2a.delivery import DeliveryEngine
        with ipdrain.drain_exclusive(self.dir, "gpio"):
            ipdrain.write_marker(self.dir, "gpio", "op1", "测试")
        prompts = []
        self.herdr.agent_prompt = lambda *a, **k: prompts.append(a) or {}
        engine = DeliveryEngine(self.spool, self.registry, self.audit, self.herdr, semantics_verified=True,
                                skip=lambda msg: ipdrain.is_draining(self.dir, msg.ip_id),
                                dispatch_guard=lambda msg: ipdrain.drain_shared(self.dir, msg.ip_id))
        result = engine.deliver(m.msg_id)
        self.assertIn(result.state, (QUEUED, WAITING_TARGET))
        self.assertEqual(prompts, [])


class ConcurrentOps(Base):
    def test_same_ip_operations_are_serialized(self):
        order = []
        original = self.life.spawn

        def slow_spawn(role, ip, cwd=None):
            order.append(("start", role))
            time.sleep(0.2)
            result = original(role, ip, cwd=cwd)
            order.append(("end", role))
            return result
        self.life.spawn = slow_spawn
        first = threading.Thread(target=self.ops.add, args=("gpio",))
        first.start()
        time.sleep(0.05)
        second = threading.Thread(target=lambda: self.make_ops().add("gpio"))
        second.start()
        first.join(5)
        second.join(5)
        self.assertEqual(order, [("start", "dv"), ("end", "dv"), ("start", "sw"), ("end", "sw")])


if __name__ == "__main__":
    unittest.main()
