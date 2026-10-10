"""阶段 5f 的真实 herdr 测试:agent 生命周期(`a2a agent ...` 命令行)。

A2A_INTEGRATION=1:       假 agent(通过启动脚本 + 覆盖 exec 启动,和真实 fnx 同一路径),含消息回归。
A2A_INTEGRATION_FNX=1:   真实 fnx_dv / fnx_sw 的 spawn / stop / restore / purge。**不发任何提示词,不调用模型。**
使用独立命名会话 a2al<pid>,结束时删除。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Optional

from a2a.audit import AuditLog
from a2a.broker import Broker
from a2a.herdr_client import HerdrClient, herdr_version, session_delete, session_stop
from a2a.messages import DELIVERED, QUEUED, TARGET_MISSING, Message, new_msg_id, now_iso
from a2a.policy import BrokerConfig, semantics_verified
from a2a.registry import Registry
from a2a.spool import Spool
from a2a.topology import TopologyStore

ENABLED = os.environ.get("A2A_INTEGRATION") == "1"
FNX_ENABLED = os.environ.get("A2A_INTEGRATION_FNX") == "1"
HAVE_TOOLS = bool(shutil.which("herdr") and shutil.which("tmux"))
SESSION = "a2al%d" % os.getpid()
TMUX = SESSION + "_tty"
HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
FAKE = HERE / "fake_agent.py"
FNX = {"dv": os.path.expanduser("~/.forenyx/fnx_dv/bin/fnx_dv"),
       "sw": os.path.expanduser("~/.forenyx/fnx_sw/bin/fnx_sw")}
client: Optional[HerdrClient] = None


def setUpModule() -> None:  # noqa: N802
    global client
    if not (ENABLED and HAVE_TOOLS):
        return
    subprocess.run(["tmux", "new-session", "-d", "-x", "200", "-y", "50", "-s", TMUX,
                    "cd %s && herdr session attach %s" % (os.path.expanduser("~"), SESSION)], check=True)
    client = HerdrClient(SESSION)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if client.is_server_running():
            return
        time.sleep(0.5)
    raise RuntimeError("命名会话 %s 没有在 30 秒内启动" % SESSION)


def tearDownModule() -> None:  # noqa: N802
    if not (ENABLED and HAVE_TOOLS):
        return
    for fn in (session_stop, session_delete):
        try:
            fn(SESSION)
        except Exception as exc:
            print("清理 %s 失败: %s" % (fn.__name__, exc))
    subprocess.run(["tmux", "kill-session", "-t", TMUX], capture_output=True)


class _Base(unittest.TestCase):
    launchers: dict = {}

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.env = dict(os.environ, A2A_STATE_DIR=str(self.state), PYTHONPATH=str(SRC), PYTHONDONTWRITEBYTECODE="1")
        self.store = TopologyStore.create(self.state / "topology.yaml", {
            "version": 1, "project_id": "soc_a", "workspace_label": "lc-%s" % self.id().split(".")[-1][:20],
            "roles": {r: {"label": f"{r}智能体", "launcher": path} for r, path in self.make_launchers().items()},
            "ips": ["uart", "gpio"],
            "edges": [{"id": "dv_done", "from": "dv", "to": "sw", "template": "{ip}已完成UVM验证,请开发驱动。"}]})
        self.registry = Registry(self.state / "registry.json")
        self.audit = AuditLog(self.state / "audit.jsonl")
        self.addCleanup(self.purge_all)

    def make_launchers(self) -> dict:
        raise NotImplementedError

    def a2a(self, *argv, ok=True):
        proc = subprocess.run([sys.executable, "-m", "a2a.cli", *argv, "--session", SESSION], env=self.env,
                              capture_output=True, text=True, timeout=180)
        if ok:
            self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.returncode, (json.loads(proc.stdout) if proc.stdout.strip() else None), proc.stderr

    def purge_all(self):
        for record in self.registry.list():
            subprocess.run([sys.executable, "-m", "a2a.cli", "agent", "purge", record.role, record.ip_id,
                            "--session", SESSION], env=self.env, capture_output=True, timeout=60)

    def live(self, agent_id):
        try:
            return client.agent_get(agent_id)
        except Exception:
            return None

    def wait_gone(self, agent_id, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self.live(agent_id) is not None:
            time.sleep(0.3)
        return self.live(agent_id) is None

    def environ_of_agent(self, pane_id):
        pid = client.pane_process_info(pane_id)["foreground_processes"][0]["pid"]
        raw = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        return dict(item.decode().split("=", 1) for item in raw if b"=" in item)


@unittest.skipUnless(ENABLED and HAVE_TOOLS, "设置 A2A_INTEGRATION=1,并要求有 herdr 和 tmux")
class FakeAgentLifecycle(_Base):
    def make_launchers(self):
        launchers = {}
        for role in ("dv", "sw"):
            path = self.state / f"launch_{role}.sh"
            # 和 fnx 的启动脚本同一结构:设置环境、最后一行 exec;生命周期会覆盖 exec 让 argv[0] 变成 pi
            path.write_text(f"#!/bin/bash\nexport FAKE_ROLE={role}\n"
                            f'exec {sys.executable} {FAKE} "{self.state}/$A2A_ROLE""_""$A2A_IP"".log" work\n')
            path.chmod(0o755)
            launchers[role] = str(path)
        return launchers

    def test_spawn_layout_identity_and_naming(self):
        _, dv, _ = self.a2a("agent", "spawn", "dv", "uart")
        _, sw, _ = self.a2a("agent", "spawn", "sw", "uart")
        _, sw2, _ = self.a2a("agent", "spawn", "sw", "gpio")
        self.assertEqual(sw["tab_id"], sw2["tab_id"])        # 同一角色同一个 tab
        self.assertNotEqual(dv["tab_id"], sw["tab_id"])
        labels = {t["tab_id"]: t.get("label") for t in client.tab_list()}
        self.assertEqual((labels[dv["tab_id"]], labels[sw["tab_id"]]), ("dv智能体", "sw智能体"))
        for agent_id, info in (("dv_uart", dv), ("sw_uart", sw), ("sw_gpio", sw2)):
            agent = self.live(agent_id)                       # 按 agent_id 能在 herdr 里找到
            self.assertEqual((agent["pane_id"], agent["agent"]), (info["pane_id"], "pi"))
        environ = self.environ_of_agent(sw2["pane_id"])       # 进程里真实的身份环境变量
        self.assertEqual((environ["A2A_PROJECT_ID"], environ["A2A_ROLE"], environ["A2A_IP"], environ["FAKE_ROLE"]),
                         ("soc_a", "sw", "gpio", "sw"))
        self.assertEqual(self.a2a("agent", "spawn", "sw", "uart", ok=False)[0], 6)  # 重复 spawn 被拒

    def test_stop_close_restore_purge(self):
        _, sw, _ = self.a2a("agent", "spawn", "sw", "uart")
        self.a2a("agent", "stop", "sw", "uart")
        self.assertTrue(self.wait_gone("sw_uart"))
        self.assertIn(sw["pane_id"], {p["pane_id"] for p in client.pane_list()})   # pane 还在
        self.assertEqual(self.registry.get("sw_uart").lifecycle, "stopped")
        _, restored, _ = self.a2a("agent", "restore", "sw", "uart")
        self.assertEqual(restored["pane_id"], sw["pane_id"])                       # 原 pane 重启
        self.assertIsNotNone(self.live("sw_uart"))                                 # 重新改名
        self.a2a("agent", "close", "sw", "uart")
        self.assertNotIn(sw["pane_id"], {p["pane_id"] for p in client.pane_list()})
        _, again, _ = self.a2a("agent", "restore", "sw", "uart")
        self.assertNotEqual(again["pane_id"], sw["pane_id"])                       # 新 pane
        self.a2a("agent", "purge", "sw", "uart")
        self.assertEqual(self.a2a("agent", "stop", "sw", "uart", ok=False)[0], 5)  # 已注销
        self.assertNotIn(again["pane_id"], {p["pane_id"] for p in client.pane_list()})
        events = [e["state"] for e in self.audit.read() if str(e.get("state", "")).startswith("AGENT_")]
        self.assertEqual(events, ["AGENT_SPAWNED", "AGENT_STOPPED", "AGENT_RESTORED", "AGENT_CLOSED",
                                  "AGENT_RESTORED", "AGENT_PURGED"])

    def test_stopped_target_pauses_its_queue_until_restore_and_ruling(self):
        # 回归(08 §7.2):目标被停止后,队列头判 TARGET_MISSING 并暂停;恢复后由操作员裁定重试才继续
        self.a2a("agent", "spawn", "sw", "uart")
        spool = Spool(self.state / "spool")
        broker = Broker(spool=spool, registry=self.registry, audit=self.audit, client=client, session=SESSION,
                        state_dir=self.state, semantics_verified=semantics_verified(herdr_version()),
                        config=BrokerConfig())

        def enqueue(text):
            return spool.enqueue(Message(msg_id=new_msg_id(), created_at=now_iso(), edge_id="dv_done",
                                         src="dv_uart", dst="sw_uart", project_id="soc_a", ip_id="uart",
                                         session=SESSION, text=text, state=QUEUED, topology_revision="r"))

        first = enqueue("FIRST")
        broker.scan(threaded=False)
        self.assertEqual(spool.get(first.msg_id).state, DELIVERED)
        self.a2a("agent", "stop", "sw", "uart")
        second, third = enqueue("SECOND"), enqueue("THIRD")
        self.assertEqual(broker.scan(threaded=False), {"sw_uart": "paused"})
        self.assertEqual((spool.get(second.msg_id).state, spool.get(third.msg_id).state), (TARGET_MISSING, QUEUED))
        self.a2a("agent", "restore", "sw", "uart")
        broker.scan(threaded=False)
        self.assertEqual(spool.get(third.msg_id).state, QUEUED)   # 目标恢复不会自动放行
        code, ruling, err = self.a2a_resolve(second.msg_id)
        self.assertEqual(code, 0, err)
        broker.scan(threaded=False)
        self.assertEqual([spool.get(m).state for m in (ruling["retry_msg_id"], third.msg_id)], [DELIVERED, DELIVERED])
        log = (self.state / "sw_uart.log").read_bytes()
        self.assertEqual([log.count(t) for t in (b"FIRST", b"SECOND", b"THIRD")], [1, 1, 1])

    def a2a_resolve(self, msg_id):
        proc = subprocess.run([sys.executable, "-m", "a2a.cli", "resolve", msg_id, "retry", "--reason",
                               "目标已恢复"], env=self.env, capture_output=True, text=True, timeout=60)
        return proc.returncode, (json.loads(proc.stdout) if proc.stdout.strip() else None), proc.stderr


@unittest.skipUnless(ENABLED and FNX_ENABLED and HAVE_TOOLS and all(os.path.exists(p) for p in FNX.values()),
                     "设置 A2A_INTEGRATION=1 与 A2A_INTEGRATION_FNX=1,并安装 fnx_dv / fnx_sw")
class RealFnxLifecycle(_Base):
    """真实 fnx:只启动、识别、改名、登记、停止、恢复、注销。不发任何提示词。"""

    def make_launchers(self):
        return dict(FNX)

    def test_real_fnx_spawn_stop_restore_purge(self):
        for role in ("dv", "sw"):
            _, info, _ = self.a2a("agent", "spawn", role, "uart")
            agent = self.live(f"{role}_uart")
            self.assertEqual((agent["pane_id"], agent["agent"]), (info["pane_id"], "pi"))
            self.assertIn(agent["agent_status"], ("idle", "done"))
            environ = self.environ_of_agent(info["pane_id"])
            self.assertEqual((environ["A2A_ROLE"], environ["A2A_IP"]), (role, "uart"))
        self.a2a("agent", "stop", "dv", "uart")
        self.assertTrue(self.wait_gone("dv_uart"))
        _, restored, _ = self.a2a("agent", "restore", "dv", "uart")
        self.assertEqual(self.live("dv_uart")["pane_id"], restored["pane_id"])
        for role in ("dv", "sw"):
            self.a2a("agent", "purge", role, "uart")
            self.assertTrue(self.wait_gone(f"{role}_uart"))


if __name__ == "__main__":
    unittest.main()
