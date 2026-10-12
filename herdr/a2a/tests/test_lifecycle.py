"""阶段 5f:生命周期的决策逻辑(假 herdr,内存里模拟 workspace / tab / pane / agent)。真实行为见集成测试。"""
from __future__ import annotations

import json
import os
import signal
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List

from a2a.audit import AuditLog
from a2a.errors import HerdrError, HerdrNotFound, HerdrTimeout
from a2a.herdr_client import Created
from a2a.lifecycle import Lifecycle, LifecycleError
from a2a.procsignal import ProcInspectError, ProcSnapshot
from a2a.registry import AgentNotRegisteredError, Registry
from a2a.topology import TopologyError, TopologyStore


class FakeHerdr:
    def __init__(self):
        self.session = "s1"
        self.workspaces: List[dict] = []
        self.tabs: List[dict] = []
        self.panes: Dict[str, dict] = {}
        self.agents: Dict[str, dict] = {}   # pane_id -> agent
        self.calls: List[tuple] = []
        self.detect = True
        self.n = 0
        self.process = {}

    def _id(self, prefix):
        self.n += 1
        return f"w1:{prefix}{self.n}"

    def _pane(self, tab_id, env, cwd):
        pid = self._id("p")
        self.panes[pid] = {"pane_id": pid, "tab_id": tab_id, "env": env, "cwd": cwd}
        return pid

    def workspace_list(self):
        return self.workspaces

    def workspace_create(self, *, label, cwd, env):
        self.calls.append(("workspace_create", label))
        self.workspaces.append({"workspace_id": "w1", "label": label})
        tab = {"tab_id": self._id("t"), "label": label, "workspace_id": "w1"}
        self.tabs.append(tab)
        pid = self._pane(tab["tab_id"], env, cwd)
        return Created({}, "w1", tab["tab_id"], pid)

    def tab_rename(self, tab_id, label):
        next(t for t in self.tabs if t["tab_id"] == tab_id)["label"] = label

    def tab_list(self, workspace_id=None):
        return self.tabs

    def tab_create(self, *, workspace_id, label, cwd, env):
        self.calls.append(("tab_create", label))
        tab = {"tab_id": self._id("t"), "label": label, "workspace_id": workspace_id}
        self.tabs.append(tab)
        return Created({}, workspace_id, tab["tab_id"], self._pane(tab["tab_id"], env, cwd))

    def pane_list(self, workspace_id=None):
        return list(self.panes.values())

    def pane_split(self, pane_id, *, direction, cwd, env):
        self.calls.append(("pane_split", pane_id))
        tab_id = self.panes[pane_id]["tab_id"]
        return Created({}, "w1", tab_id, self._pane(tab_id, env, cwd))

    def pane_get(self, pane_id):
        if pane_id not in self.panes:
            raise HerdrNotFound("x", code="pane_not_found")
        return self.panes[pane_id]

    def pane_rename(self, pane_id, label):
        self.panes[pane_id]["label"] = label

    def pane_run(self, pane_id, command):
        self.calls.append(("pane_run", pane_id, command))
        if self.detect:
            self.agents[pane_id] = {"pane_id": pane_id, "agent": "pi", "agent_status": "idle"}
            self.process[pane_id] = {"foreground_process_group_id": 4242, "shell_pid": 4000,
                                     "foreground_processes": [{"argv": ["pi"], "pid": 4242}]}

    def pane_close(self, pane_id):
        self.calls.append(("pane_close", pane_id))
        if pane_id not in self.panes:
            raise HerdrNotFound("x", code="pane_not_found")
        del self.panes[pane_id]
        self.agents.pop(pane_id, None)
        self.process.pop(pane_id, None)

    def pane_process_info(self, pane_id):
        if pane_id not in self.panes:
            raise HerdrNotFound("x", code="pane_not_found")
        # 没有前台进程(从未启动或已退出)的 pane:只剩 shell(与 herdr 实测一致)
        return self.process.get(pane_id) or {"foreground_process_group_id": 4000, "shell_pid": 4000,
                                             "foreground_processes": [{"argv": ["bash"], "pid": 4000}]}

    def wait_for_agent_detected(self, pane_id, *, timeout_s):
        if pane_id not in self.agents:
            raise HerdrTimeout("没识别", code="agent_not_detected")
        return self.agents[pane_id]

    def agent_wait(self, target, *, until=None, timeout_ms=None):
        return self.agents[target]

    def agent_rename(self, target, name):
        self.calls.append(("agent_rename", target, name))
        self.agents[target]["name"] = name

    def agent_get(self, target):
        if target in self.agents:
            return self.agents[target]
        for agent in self.agents.values():  # 也可以按名字寻址
            if agent.get("name") == target:
                return agent
        raise HerdrNotFound("x", code="agent_not_found")


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        d = self.dir = Path(temp.name)
        self.launcher = d / "fnx_fake"
        self.launcher.write_text("#!/bin/bash\nexport X=1\nexec /bin/sleep 100 \"$@\"\n")
        self.store = TopologyStore.create(d / "topology.yaml", {
            "version": 1, "project_id": "soc_a", "workspace_label": "SoC-A",
            "roles": {"dv": {"label": "验证智能体", "launcher": str(self.launcher)},
                      "sw": {"label": "软件智能体"}},
            "ips": ["uart", "gpio"], "edges": []})
        self.registry = Registry(d / "registry.json")
        self.audit = AuditLog(d / "audit.jsonl")
        self.herdr = FakeHerdr()
        self.killed = []
        self.life = Lifecycle(client=self.herdr, topology=self.store, registry=self.registry, audit=self.audit,
                              state_dir=d, kill=self.fake_kill, sleep=lambda s: None)

    def fake_kill(self, pgid, sig):
        """信号结束 agent:agent 从 herdr 消失,pane 里只剩 shell。"""
        self.killed.append((pgid, sig))
        pane = next(p for p, i in self.herdr.process.items() if i and i["foreground_process_group_id"] == pgid)
        self.herdr.agents.pop(pane, None)
        self.herdr.process[pane] = None

    def events(self, state):
        return [e for e in self.audit.read() if e.get("state") == state]


class WorkingDirectory(Base):
    """18 号方案 Codex 审核:pane 丢失后新建 pane 要用登记的工作目录,不在家目录里猜着启动。"""

    def test_spawn_records_the_absolute_cwd(self):
        rec = self.life.spawn("dv", "uart", cwd=str(self.dir / "work"))
        self.assertEqual(rec.cwd, str(self.dir / "work"))
        self.assertEqual(self.herdr.panes[rec.pane_id]["cwd"], rec.cwd)
        self.assertEqual(self.life.spawn("dv", "gpio").cwd, os.path.expanduser("~"))   # 默认家目录,也登记

    def test_restore_into_a_new_pane_uses_the_recorded_cwd(self):
        rec = self.life.spawn("dv", "uart", cwd=str(self.dir / "work"))
        self.life.close("dv", "uart")
        restored = self.life.restore("dv", "uart")
        self.assertNotEqual(restored.pane_id, rec.pane_id)
        self.assertEqual(self.herdr.panes[restored.pane_id]["cwd"], str(self.dir / "work"))
        self.assertEqual(restored.cwd, str(self.dir / "work"))

    def test_explicit_cwd_overrides_and_is_recorded(self):
        self.life.spawn("dv", "uart", cwd=str(self.dir / "a"))
        self.life.close("dv", "uart")
        restored = self.life.restore("dv", "uart", cwd=str(self.dir / "b"))
        self.assertEqual(self.herdr.panes[restored.pane_id]["cwd"], str(self.dir / "b"))
        self.assertEqual(self.registry.get("dv_uart").cwd, str(self.dir / "b"))

    def test_legacy_record_without_cwd_refuses_a_new_pane(self):
        rec = self.life.spawn("dv", "uart")
        self.life.close("dv", "uart")
        # 模拟旧注册表:记录里没有 cwd
        data = json.loads(self.registry.path.read_text())
        del data["agents"]["dv_uart"]["cwd"]
        self.registry.path.write_text(json.dumps(data))
        self.assertIsNone(self.registry.get("dv_uart").cwd)
        panes = set(self.herdr.panes)
        with self.assertRaises(LifecycleError):
            self.life.restore("dv", "uart")
        self.assertEqual(set(self.herdr.panes), panes)                       # 没有新建 pane
        self.assertEqual(self.registry.get("dv_uart").lifecycle, "closed")   # 也没有改成 failed
        restored = self.life.restore("dv", "uart", cwd=str(self.dir))       # 显式 --cwd 可以
        self.assertNotEqual(restored.pane_id, rec.pane_id)

    def test_restart_in_the_original_pane_keeps_the_recorded_cwd(self):
        rec = self.life.spawn("dv", "uart", cwd=str(self.dir / "work"))
        self.life.stop("dv", "uart")
        self.assertEqual(self.life.restore("dv", "uart").cwd, rec.cwd)


class RestoreGuard(Base):
    def test_restore_refuses_an_original_pane_occupied_by_another_process(self):
        # 18 号方案 §4.1:原 pane 前台不是只剩 shell 时,不往里写启动命令
        rec = self.life.spawn("dv", "uart")
        self.life.stop("dv", "uart")
        for info in ({"foreground_process_group_id": 5555, "shell_pid": 4000,
                      "foreground_processes": [{"argv": ["vim"], "pid": 5555}]},
                     {"foreground_process_group_id": 4000, "shell_pid": 4000}):          # 不完整
            with self.subTest(info=info):
                self.herdr.process[rec.pane_id] = info
                runs = len([c for c in self.herdr.calls if c[0] == "pane_run"])
                with self.assertRaises(LifecycleError):
                    self.life.restore("dv", "uart")
                self.assertEqual(len([c for c in self.herdr.calls if c[0] == "pane_run"]), runs)
        self.herdr.process[rec.pane_id] = None
        self.assertEqual(self.life.restore("dv", "uart").pane_id, rec.pane_id)   # 只剩 shell 时原 pane 重启


class Spawn(Base):
    def test_layout_workspace_then_role_tab_then_split_per_ip(self):
        uart = self.life.spawn("dv", "uart")
        gpio = self.life.spawn("dv", "gpio")
        kinds = [c[0] for c in self.herdr.calls if c[0] in ("workspace_create", "tab_create", "pane_split")]
        self.assertEqual(kinds, ["workspace_create", "pane_split"])  # 同一角色第二个 IP 在同一 tab 里拆分
        self.assertEqual(uart.tab_id, gpio.tab_id)
        self.assertEqual(self.herdr.tabs[0]["label"], "验证智能体")
        env = self.herdr.panes[gpio.pane_id]["env"]
        self.assertEqual({k: env[k] for k in ("A2A_PROJECT_ID", "A2A_ROLE", "A2A_IP")},
                         {"A2A_PROJECT_ID": "soc_a", "A2A_ROLE": "dv", "A2A_IP": "gpio"})
        # pi 插件调用 `a2a send` 需要的位置:与 broker 同一个状态目录与拓扑,以及运行 a2a 的 Python
        self.assertEqual(env["A2A_STATE_DIR"], str(self.dir))
        self.assertEqual(env["A2A_TOPOLOGY"], str(self.dir / "topology.yaml"))
        self.assertTrue(Path(env["A2A_PYTHON"]).exists())
        self.assertTrue((Path(env["A2A_SRC"]) / "a2a" / "cli.py").exists())
        self.assertTrue(env["A2A_HERDR_BIN"])  # 插件向 herdr 上报状态用的程序
        self.assertEqual((uart.lifecycle, uart.agent_name), ("running", "dv_uart"))
        self.assertEqual(len(self.events("AGENT_SPAWNED")), 2)

    def test_rename_is_the_last_step_after_detection(self):
        self.life.spawn("dv", "uart")
        names = [c[0] for c in self.herdr.calls]
        self.assertLess(names.index("pane_run"), names.index("agent_rename"))
        run = next(c for c in self.herdr.calls if c[0] == "pane_run")
        self.assertIn("exec -a pi", run[2])  # 覆盖 exec 的启动命令

    def test_launch_command_carries_the_identity_env_itself(self):
        # 18 号方案实测:herdr 会话重启后 pane 的新 shell 不带创建时注入的变量;启动命令必须自带身份
        import shlex
        self.life.spawn("dv", "uart")
        run = next(c for c in self.herdr.calls if c[0] == "pane_run")
        words = shlex.split(run[2])
        env = dict(w.split("=", 1) for w in words[:words.index("bash")])
        self.assertEqual((env["A2A_PROJECT_ID"], env["A2A_ROLE"], env["A2A_IP"]), ("soc_a", "dv", "uart"))
        self.assertEqual(env["A2A_STATE_DIR"], str(self.dir))
        self.assertIn("A2A_HERDR_BIN", env)

    def test_launch_args_from_topology_are_passed_to_the_launcher(self):
        self.store.update(lambda data: data["roles"]["dv"].update(launch_args=["--no-builtin-tools"]))
        self.life.spawn("dv", "uart")
        run = next(c for c in self.herdr.calls if c[0] == "pane_run")
        self.assertTrue(run[2].endswith(" _ --no-builtin-tools"), run[2])

    def test_refuses_before_touching_herdr(self):
        for role, ip, error in (("dv", "spi", LifecycleError), ("sw", "uart", LifecycleError)):
            with self.assertRaises(error, msg=(role, ip)):
                self.life.spawn(role, ip)
        self.launcher.write_text("#!/bin/bash\necho $0\nexec a\nexec b\n")  # 预检不通过
        with self.assertRaises(LifecycleError):
            self.life.spawn("dv", "uart")
        self.assertEqual(self.herdr.calls, [])

    def test_failed_start_closes_the_pane_and_registers_nothing(self):
        self.herdr.detect = False
        with self.assertRaises(HerdrTimeout):
            self.life.spawn("dv", "uart")
        self.assertEqual(self.herdr.panes, {})
        with self.assertRaises(AgentNotRegisteredError):
            self.registry.get("dv_uart")

    def test_already_registered(self):
        self.life.spawn("dv", "uart")
        with self.assertRaisesRegex(LifecycleError, "已登记"):
            self.life.spawn("dv", "uart")


class FakeSignaler:
    """假的 pidfd 信号器:snapshots 是依次返回的快照(None 表示进程不在),可让 open / send 抛 ESRCH。"""
    supported = True

    def __init__(self, test, snapshots, *, open_gone=False, send_gone=False, on_gone=None):
        self.test, self.snapshots = test, list(snapshots)
        self.open_gone, self.send_gone, self.on_gone = open_gone, send_gone, on_gone
        self.calls = []
        self.members = {}   # pgid -> 仍活着的进程

    def group_members(self, pgid):
        if self.members.get(pgid) == "error":
            raise ProcInspectError("权限不足")
        return list(self.members.get(pgid, []))

    def snapshot(self, pid):
        self.calls.append(("snapshot", pid))
        snap = self.snapshots.pop(0)
        if snap is None and self.on_gone:
            self.on_gone()
        return snap

    def open(self, pid):
        self.calls.append(("open", pid))
        if self.open_gone:
            self.on_gone and self.on_gone()
            raise ProcessLookupError(pid)
        return 99

    def send(self, fd, sig):
        self.calls.append(("send", fd, sig))
        if self.send_gone:
            self.on_gone and self.on_gone()
            raise ProcessLookupError(fd)
        self.test.killed.append(("pidfd", sig))

    def close(self, fd):
        self.calls.append(("close", fd))


class StopVerified(Base):
    """19 号方案 §A:pidfd 锁定组长后发信号;只有 herdr 看不到 agent 且 pane 只剩 shell 才算停止。"""
    PI = ProcSnapshot(start_time=777, pgid=4242, argv0="pi")

    def setUp(self):
        super().setUp()
        self.life.stop_timeout_s = 0.05

    def use(self, *snapshots, **kw):
        self.sig = FakeSignaler(self, snapshots, **kw)
        self.life._signaler = self.sig
        return self.life.spawn("dv", "uart")

    def leader_exits_child_stays(self, record):
        """组长退出,但它启动的子进程(同一进程组)还在前台。"""
        self.herdr.agents.pop(record.pane_id, None)
        self.herdr.process[record.pane_id] = {"foreground_process_group_id": 4242, "shell_pid": 4000,
                                              "foreground_processes": [{"argv": ["sleep"], "pid": 4300}]}

    def test_signals_the_leader_through_pidfd_then_the_group(self):
        self.use(self.PI, self.PI)
        self.assertEqual(self.life.stop("dv", "uart").lifecycle, "stopped")
        self.assertEqual(self.killed, [("pidfd", signal.SIGTERM), (4242, signal.SIGTERM)])
        self.assertEqual([c[0] for c in self.sig.calls], ["snapshot", "open", "snapshot", "send", "close"])

    def test_refuses_when_the_leader_was_replaced_while_checking(self):
        self.use(self.PI, ProcSnapshot(start_time=888, pgid=4242, argv0="pi"))
        with self.assertRaisesRegex(LifecycleError, "被替换"):
            self.life.stop("dv", "uart")
        self.assertEqual(self.killed, [])
        self.assertIn(("close", 99), self.sig.calls)
        self.assertEqual(self.registry.get("dv_uart").lifecycle, "running")

    def test_refuses_when_the_snapshot_is_not_a_pi_group_leader(self):
        for snap in (ProcSnapshot(777, 4242, "vim"), ProcSnapshot(777, 5000, "pi")):
            with self.subTest(snap=snap):
                self.setUp()
                self.use(snap)
                with self.assertRaises(LifecycleError):
                    self.life.stop("dv", "uart")
                self.assertEqual(self.killed, [])

    def test_refuses_when_the_leader_is_not_in_the_foreground_list(self):
        record = self.use()
        self.herdr.process[record.pane_id]["foreground_processes"] = [{"argv": ["pi"], "pid": 4300}]
        with self.assertRaisesRegex(LifecycleError, "组长"):
            self.life.stop("dv", "uart")
        self.assertEqual(self.killed, [])

    def test_leader_gone_before_pidfd_send_never_calls_killpg(self):
        for kw, snaps in (({"open_gone": True}, (self.PI,)), ({"send_gone": True}, (self.PI, self.PI)), ({}, (None,))):
            with self.subTest(kw=kw, snaps=snaps):
                self.setUp()
                pane = {}
                # 组长在 stop 过程中(核对之后)退出,它启动的子进程还在
                record = self.use(*snaps, on_gone=lambda: self.leader_exits_child_stays(pane["r"]), **kw)
                pane["r"] = record
                with self.assertRaisesRegex(LifecycleError, "没有完全退出"):
                    self.life.stop("dv", "uart")
                self.assertNotIn((4242, signal.SIGTERM), self.killed)   # 没有按旧 pgid 发组信号
                self.assertEqual(self.registry.get("dv_uart").lifecycle, "running")
                # agent 已不在、子进程仍在时再 stop:仍拒绝
                with self.assertRaisesRegex(LifecycleError, "不是只剩 shell"):
                    self.life.stop("dv", "uart")
                self.herdr.process[record.pane_id] = None              # 子进程也退出了 → 可以登记 stopped
                self.assertEqual(self.life.stop("dv", "uart").lifecycle, "stopped")

    def test_leader_gone_and_pane_back_to_shell_counts_as_stopped(self):
        holder = {}

        def exit_cleanly():
            self.herdr.agents.pop(holder["r"].pane_id, None)
            self.herdr.process[holder["r"].pane_id] = None
        holder["r"] = self.use(self.PI, open_gone=True, on_gone=exit_cleanly)
        self.assertEqual(self.life.stop("dv", "uart").lifecycle, "stopped")
        self.assertNotIn((4242, signal.SIGTERM), self.killed)

    def test_orphaned_child_in_the_group_is_not_stopped_even_if_the_pane_looks_clean(self):
        # 实测:组长退出后 shell 收回终端,残留子进程成了后台进程,pane 看起来只剩 shell
        self.use(self.PI, self.PI)
        self.sig.members[4242] = [4300]
        with self.assertRaisesRegex(LifecycleError, r"进程组 4242 仍有进程 \[4300\]"):
            self.life.stop("dv", "uart")
        self.assertEqual(self.registry.get("dv_uart").lifecycle, "running")
        # agent 已不在时再 stop:按注册表里登记的进程组核对,仍拒绝
        self.assertEqual(self.registry.get("dv_uart").pgid, 4242)
        with self.assertRaisesRegex(LifecycleError, "不是只剩 shell"):
            self.life.stop("dv", "uart")
        self.sig.members[4242] = []                                   # 人工结束残留进程后
        self.assertEqual(self.life.stop("dv", "uart").lifecycle, "stopped")

    def test_legacy_record_without_pgid_is_not_stopped_when_the_agent_is_already_gone(self):
        # Codex 审核阻断:旧记录没有进程组,agent 已不在时无法确认子进程都退出了 → 不登记 stopped
        record = self.use()
        data = json.loads(self.registry.path.read_text())
        del data["agents"]["dv_uart"]["pgid"]
        self.registry.path.write_text(json.dumps(data))
        self.herdr.agents.pop(record.pane_id, None)
        self.herdr.process[record.pane_id] = None                    # pane 看起来只剩 shell
        with self.assertRaisesRegex(LifecycleError, "旧记录"):
            self.life.stop("dv", "uart")
        self.assertEqual(self.registry.get("dv_uart").lifecycle, "running")
        self.assertEqual(self.life.restore("dv", "uart").pgid, 4242)   # restore 后补上进程组

    def test_cannot_inspect_the_process_group_fails_closed(self):
        self.use(self.PI, self.PI)
        self.sig.members[4242] = "error"
        with self.assertRaisesRegex(LifecycleError, "无法核对进程组"):
            self.life.stop("dv", "uart")
        self.assertEqual(self.registry.get("dv_uart").lifecycle, "running")

    def test_cannot_inspect_the_leader_refuses_to_signal(self):
        self.use()
        self.sig.snapshot = lambda pid: (_ for _ in ()).throw(ProcInspectError("权限不足"))
        with self.assertRaisesRegex(LifecycleError, "无法核对组长"):
            self.life.stop("dv", "uart")
        self.assertEqual(self.killed, [])

    def test_spawn_or_restore_without_a_process_group_does_not_succeed(self):
        record = self.life.spawn("dv", "uart")
        self.life.stop("dv", "uart")
        original = self.herdr.pane_run

        def run_without_group(pane_id, command):
            original(pane_id, command)
            self.herdr.process[pane_id] = {"foreground_process_group_id": 4000, "shell_pid": 4000,
                                           "foreground_processes": [{"argv": ["pi"], "pid": 4000}]}
        self.herdr.pane_run = run_without_group
        with self.assertRaisesRegex(LifecycleError, "取不到前台进程组"):
            self.life.restore("dv", "uart")
        self.assertEqual(self.registry.get("dv_uart").lifecycle, "failed")   # 不保留旧的进程组作数
        # Codex 复核:agent 已在原 pane 启动并改名,失败时不能留下一个记录不是 running 的运行进程
        self.assertNotIn(record.pane_id, self.herdr.panes)
        self.assertNotIn(record.pane_id, self.herdr.agents)
        with self.assertRaisesRegex(LifecycleError, "取不到前台进程组"):
            self.life.spawn("dv", "gpio")
        with self.assertRaises(AgentNotRegisteredError):
            self.registry.get("dv_gpio")
        self.assertEqual(record.pgid, 4242)

    def test_failed_restore_in_the_original_pane_leaves_no_unmanaged_agent(self):
        record = self.life.spawn("dv", "uart")
        self.life.stop("dv", "uart")

        def never_ready(target, *, until=None, timeout_ms=None):
            raise HerdrTimeout("没就绪", code="timeout")
        self.herdr.agent_wait = never_ready                       # pi 已启动,但没进入可投递状态
        with self.assertRaisesRegex(LifecycleError, "可投递"):
            self.life.restore("dv", "uart")
        self.assertNotIn(record.pane_id, self.herdr.panes)        # 连同已启动的 agent 一起关掉
        self.assertNotIn(record.pane_id, self.herdr.agents)
        self.assertEqual(self.registry.get("dv_uart").lifecycle, "failed")
        del self.herdr.agent_wait
        restored = self.life.restore("dv", "uart")                # 之后 restore 新建 pane(用登记的工作目录)
        self.assertNotEqual(restored.pane_id, record.pane_id)
        self.assertEqual(self.herdr.panes[restored.pane_id]["cwd"], record.cwd)

    def test_failed_restore_whose_pane_cannot_be_closed_says_the_agent_may_still_run(self):
        self.life.spawn("dv", "uart")
        self.life.stop("dv", "uart")
        self.herdr.agent_rename = lambda target, name: (_ for _ in ()).throw(HerdrError("改名失败"))
        self.herdr.pane_close = lambda pane_id: (_ for _ in ()).throw(HerdrError("关不掉"))
        with self.assertRaisesRegex(LifecycleError, "可能仍在运行.*注册表已标为 failed.*a2a agent close"):
            self.life.restore("dv", "uart")
        self.assertEqual(self.registry.get("dv_uart").lifecycle, "failed")

    def test_failed_restore_still_closes_the_pane_when_the_registry_write_fails(self):
        # Codex 复核:注册表写失败不能跳过关 pane
        record = self.life.spawn("dv", "uart")
        self.life.stop("dv", "uart")
        self.herdr.agent_rename = lambda target, name: (_ for _ in ()).throw(HerdrError("改名失败"))
        original = self.registry.update_runtime

        def update_runtime(agent_id, **fields):
            if fields.get("lifecycle") == "failed":
                raise OSError("磁盘已满")
            return original(agent_id, **fields)
        self.registry.update_runtime = update_runtime
        with self.assertRaisesRegex(LifecycleError, "已关闭.*未能标为 failed.*磁盘已满.*仍是 stopped"):
            self.life.restore("dv", "uart")
        self.assertNotIn(record.pane_id, self.herdr.panes)
        self.assertNotIn(record.pane_id, self.herdr.agents)
        self.assertEqual(self.registry.get("dv_uart").lifecycle, "stopped")
        self.registry.update_runtime = original
        del self.herdr.agent_rename
        self.assertNotEqual(self.life.restore("dv", "uart").pane_id, record.pane_id)   # pane 已不在:新建

    def test_ctrl_c_during_start_leaves_no_unmanaged_agent(self):
        # Codex 复核:KeyboardInterrupt 不是 Exception,清理分支不能被它绕过
        def interrupted(target, *, until=None, timeout_ms=None):
            raise KeyboardInterrupt
        self.herdr.agent_wait = interrupted                       # pi 已启动,等就绪时按了 Ctrl-C
        with self.assertRaises(KeyboardInterrupt):
            self.life.spawn("dv", "uart")
        self.assertEqual(self.herdr.panes, {})
        self.assertEqual(self.herdr.agents, {})
        with self.assertRaises(AgentNotRegisteredError):
            self.registry.get("dv_uart")
        del self.herdr.agent_wait
        record = self.life.spawn("dv", "uart")
        self.life.stop("dv", "uart")
        self.herdr.agent_wait = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.life.restore("dv", "uart")
        self.assertNotIn(record.pane_id, self.herdr.panes)
        self.assertNotIn(record.pane_id, self.herdr.agents)
        self.assertEqual(self.registry.get("dv_uart").lifecycle, "failed")

    def test_spawn_and_restore_record_the_agent_process_group(self):
        self.assertEqual(self.life.spawn("dv", "uart").pgid, 4242)
        self.life.stop("dv", "uart")
        self.assertEqual(self.life.restore("dv", "uart").pgid, 4242)

    def test_agent_gone_but_child_still_in_foreground_is_not_stopped(self):
        # 正常路径(没有 pidfd)也一样:herdr 看不到 agent 不等于进程组都退出了
        record = self.life.spawn("dv", "uart")
        self.life._kill = lambda pgid, sig: (self.killed.append((pgid, sig)), self.leader_exits_child_stays(record))
        with self.assertRaisesRegex(LifecycleError, "没有完全退出"):
            self.life.stop("dv", "uart")
        self.assertEqual(self.registry.get("dv_uart").lifecycle, "running")
        self.herdr.process[record.pane_id] = {"foreground_process_group_id": 4000, "shell_pid": 4000}  # 不完整
        with self.assertRaises(LifecycleError):
            self.life._terminate(self.registry.get("dv_uart"))


class StopRestoreClosePurge(Base):
    def test_stop_signals_only_the_agent_process_group(self):
        record = self.life.spawn("dv", "uart")
        stopped = self.life.stop("dv", "uart")
        self.assertEqual(self.killed, [(4242, signal.SIGTERM)])
        self.assertEqual(stopped.lifecycle, "stopped")
        self.assertIn(record.pane_id, self.herdr.panes)  # pane 保留

    def test_stop_refuses_to_signal_the_shell_or_a_non_agent(self):
        record = self.life.spawn("dv", "uart")
        info = self.herdr.process[record.pane_id]
        info["foreground_process_group_id"] = info["shell_pid"]
        with self.assertRaisesRegex(LifecycleError, "不明确"):
            self.life.stop("dv", "uart")
        info["foreground_process_group_id"] = 4242
        info["foreground_processes"] = [{"argv": ["vim"]}]
        with self.assertRaisesRegex(LifecycleError, "不是 agent"):
            self.life.stop("dv", "uart")
        self.assertEqual(self.killed, [])

    def test_stop_refuses_when_the_pane_holds_a_different_agent(self):
        # 登记的 agent 退出后,同一 pane 里有人手动启动了另一个 pi(没有改名)
        record = self.life.spawn("dv", "uart")
        self.herdr.agents[record.pane_id] = {"pane_id": record.pane_id, "agent": "pi", "agent_status": "idle"}
        with self.assertRaisesRegex(LifecycleError, "不是登记的"):
            self.life.stop("dv", "uart")
        self.assertEqual(self.killed, [])
        self.assertEqual(self.registry.get("dv_uart").lifecycle, "running")

    def test_restore_after_stop_reuses_the_pane_and_renames_again(self):
        record = self.life.spawn("dv", "uart")
        self.life.stop("dv", "uart")
        restored = self.life.restore("dv", "uart")
        self.assertEqual((restored.pane_id, restored.lifecycle), (record.pane_id, "running"))
        self.assertEqual(len([c for c in self.herdr.calls if c[0] == "agent_rename"]), 2)

    def test_restore_after_close_creates_a_new_pane(self):
        record = self.life.spawn("dv", "uart")
        closed = self.life.close("dv", "uart")
        self.assertEqual(closed.lifecycle, "closed")
        self.assertNotIn(record.pane_id, self.herdr.panes)
        restored = self.life.restore("dv", "uart")
        self.assertNotEqual(restored.pane_id, record.pane_id)
        self.assertEqual(restored.lifecycle, "running")

    def test_restore_refuses_a_running_agent(self):
        self.life.spawn("dv", "uart")
        with self.assertRaisesRegex(LifecycleError, "正在运行"):
            self.life.restore("dv", "uart")

    def test_purge_unregisters_and_tolerates_a_missing_pane(self):
        record = self.life.spawn("dv", "uart")
        del self.herdr.panes[record.pane_id]  # pane 已被人手动关掉
        self.life.purge("dv", "uart")
        with self.assertRaises(AgentNotRegisteredError):
            self.registry.get("dv_uart")
        self.assertEqual(len(self.events("AGENT_PURGED")), 1)


if __name__ == "__main__":
    unittest.main()
