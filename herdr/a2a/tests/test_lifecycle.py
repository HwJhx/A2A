"""阶段 5f:生命周期的决策逻辑(假 herdr,内存里模拟 workspace / tab / pane / agent)。真实行为见集成测试。"""
from __future__ import annotations

import signal
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List

from a2a.audit import AuditLog
from a2a.errors import HerdrNotFound, HerdrTimeout
from a2a.herdr_client import Created
from a2a.lifecycle import Lifecycle, LifecycleError
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

    def _pane(self, tab_id, env):
        pid = self._id("p")
        self.panes[pid] = {"pane_id": pid, "tab_id": tab_id, "env": env}
        return pid

    def workspace_list(self):
        return self.workspaces

    def workspace_create(self, *, label, cwd, env):
        self.calls.append(("workspace_create", label))
        self.workspaces.append({"workspace_id": "w1", "label": label})
        tab = {"tab_id": self._id("t"), "label": label, "workspace_id": "w1"}
        self.tabs.append(tab)
        pid = self._pane(tab["tab_id"], env)
        return Created({}, "w1", tab["tab_id"], pid)

    def tab_rename(self, tab_id, label):
        next(t for t in self.tabs if t["tab_id"] == tab_id)["label"] = label

    def tab_list(self, workspace_id=None):
        return self.tabs

    def tab_create(self, *, workspace_id, label, cwd, env):
        self.calls.append(("tab_create", label))
        tab = {"tab_id": self._id("t"), "label": label, "workspace_id": workspace_id}
        self.tabs.append(tab)
        return Created({}, workspace_id, tab["tab_id"], self._pane(tab["tab_id"], env))

    def pane_list(self, workspace_id=None):
        return list(self.panes.values())

    def pane_split(self, pane_id, *, direction, cwd, env):
        self.calls.append(("pane_split", pane_id))
        tab_id = self.panes[pane_id]["tab_id"]
        return Created({}, "w1", tab_id, self._pane(tab_id, env))

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

    def pane_process_info(self, pane_id):
        return self.process[pane_id]

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
        d = Path(temp.name)
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
                              state_dir=d, kill=lambda pgid, sig: self.killed.append((pgid, sig)) or
                              self.herdr.agents.pop(next(p for p, i in self.herdr.process.items()
                                                         if i["foreground_process_group_id"] == pgid), None),
                              sleep=lambda s: None)

    def events(self, state):
        return [e for e in self.audit.read() if e.get("state") == state]


class Spawn(Base):
    def test_layout_workspace_then_role_tab_then_split_per_ip(self):
        uart = self.life.spawn("dv", "uart")
        gpio = self.life.spawn("dv", "gpio")
        kinds = [c[0] for c in self.herdr.calls if c[0] in ("workspace_create", "tab_create", "pane_split")]
        self.assertEqual(kinds, ["workspace_create", "pane_split"])  # 同一角色第二个 IP 在同一 tab 里拆分
        self.assertEqual(uart.tab_id, gpio.tab_id)
        self.assertEqual(self.herdr.tabs[0]["label"], "验证智能体")
        self.assertEqual(self.herdr.panes[gpio.pane_id]["env"],
                         {"A2A_PROJECT_ID": "soc_a", "A2A_ROLE": "dv", "A2A_IP": "gpio"})
        self.assertEqual((uart.lifecycle, uart.agent_name), ("running", "dv_uart"))
        self.assertEqual(len(self.events("AGENT_SPAWNED")), 2)

    def test_rename_is_the_last_step_after_detection(self):
        self.life.spawn("dv", "uart")
        names = [c[0] for c in self.herdr.calls]
        self.assertLess(names.index("pane_run"), names.index("agent_rename"))
        run = next(c for c in self.herdr.calls if c[0] == "pane_run")
        self.assertIn("exec -a pi", run[2])  # 覆盖 exec 的启动命令

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
