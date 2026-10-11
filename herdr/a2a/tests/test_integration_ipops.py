"""阶段 8:按 IP 增删(真实 herdr;假 agent,另有一项真实 fnx 起停,都不调用模型)。17 号方案 §4。

只有设置 A2A_INTEGRATION=1 时才运行;真实 fnx 一项还需要 A2A_INTEGRATION_FNX=1。使用独立命名会话 a2ai<pid>,结束时删除。
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

from a2a import ipdrain
from a2a.audit import AuditLog
from a2a.herdr_client import HerdrClient, session_delete, session_stop
from a2a.messages import DELIVERED, QUEUED
from a2a.registry import Registry
from a2a.spool import Spool
from a2a.topology import TopologyStore

ENABLED = os.environ.get("A2A_INTEGRATION") == "1"
FNX_ENABLED = os.environ.get("A2A_INTEGRATION_FNX") == "1"
HAVE_TOOLS = bool(shutil.which("herdr") and shutil.which("tmux"))
SESSION = "a2ai%d" % os.getpid()
TMUX = SESSION + "_tty"
HERE = Path(__file__).resolve().parent
FAKE = HERE / "fake_agent.py"
SRC = HERE.parent / "src"
FNX = {"dv": os.path.expanduser("~/.forenyx/fnx_dv/bin/fnx_dv"),
       "sw": os.path.expanduser("~/.forenyx/fnx_sw/bin/fnx_sw")}
TEMPLATE = "{ip} 已完成验证"
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
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.env = dict(os.environ, A2A_STATE_DIR=str(self.state), PYTHONPATH=str(SRC), PYTHONDONTWRITEBYTECODE="1")
        self.store = TopologyStore.create(self.state / "topology.yaml", {
            "version": 1, "project_id": "soc_a", "workspace_label": "ip-%s" % self.id().split(".")[-1][5:25],
            "roles": self.roles(), "ips": ["uart"],
            "edges": [{"id": "dv_done", "from": "dv", "to": "sw", "template": TEMPLATE}]})
        self.registry = Registry(self.state / "registry.json")
        self.spool = Spool(self.state / "spool")
        self.audit = AuditLog(self.state / "audit.jsonl")
        self.addCleanup(self.purge_all)

    def roles(self):
        raise NotImplementedError

    def a2a(self, *argv, ok=True, timeout=300):
        proc = subprocess.run([sys.executable, "-m", "a2a.cli", *argv], env=self.env, capture_output=True,
                              text=True, timeout=timeout)
        if ok:
            self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def purge_all(self):
        for record in self.registry.list():
            subprocess.run([sys.executable, "-m", "a2a.cli", "agent", "purge", record.role, record.ip_id,
                            "--session", SESSION], env=self.env, capture_output=True, timeout=120)

    def panes(self):
        return {p["pane_id"] for p in client.pane_list()}


@unittest.skipUnless(ENABLED and HAVE_TOOLS, "设置 A2A_INTEGRATION=1,并要求有 herdr 和 tmux")
class FakeAgentIpOps(_Base):
    def roles(self):
        launcher = self.state / "launch_fake.sh"
        launcher.write_text(
            "#!/bin/bash\n"
            f'exec {sys.executable} {FAKE} "{self.state}/${{A2A_ROLE}}_${{A2A_IP}}.log" '
            f'"$( [ "$A2A_ROLE" = dv ] && echo script:dv_done,dv_done,dv_done || echo work )"\n')
        launcher.chmod(0o755)
        return {"spec": {"label": "Spec"}, "dv": {"label": "dv", "launcher": str(launcher)},
                "sw": {"label": "sw", "launcher": str(launcher)}}

    def broker(self):
        proc = subprocess.Popen([sys.executable, "-m", "a2a.cli", "broker", "run", "--session", SESSION],
                                env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def stop():
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=90)
        self.addCleanup(stop)
        return stop

    def send_from_dv(self, ip, n):
        """触发假 dv 执行第 n 条 script(a2a send dv_done),返回回执。"""
        client.agent_prompt(f"dv_{ip}", "开始")
        events = Path(f"{self.state}/dv_{ip}.log.events")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            rows = [json.loads(l) for l in events.read_text().splitlines()] if events.exists() else []
            done = [r for r in rows if r.get("n") == n and "rc" in r]
            if done:
                client.agent_wait(f"dv_{ip}", until=("idle", "done"), timeout_ms=30000)
                return done[0]
            time.sleep(0.2)
        self.fail(f"dv_{ip} 第 {n} 次发送没有完成")

    def wait_state(self, msg_id, state, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self.spool.get(msg_id).state != state:
            time.sleep(0.2)
        self.assertEqual(self.spool.get(msg_id).state, state)

    def test_add_remove_readd_with_isolation(self):
        self.a2a("agent", "spawn", "dv", "uart", "--session", SESSION)
        self.a2a("agent", "spawn", "sw", "uart", "--session", SESSION)

        # add:加入拓扑、按角色启动;没有启动脚本的 spec 被跳过
        out = json.loads(self.a2a("ip", "add", "gpio", "--session", SESSION).stdout)
        self.assertEqual(out["roles"], {"spec": "no_launcher", "dv": "spawned", "sw": "spawned"})
        self.assertIn("gpio", self.store.current().ips)

        # broker 未运行时 gpio 发一条 -> 排队;此时 remove 被拒、撤销排空、什么都不改
        first = self.send_from_dv("gpio", 1)
        self.assertEqual(first["rc"], 0, first["stderr"])
        msg = json.loads(first["stdout"])
        refused = self.a2a("ip", "remove", "gpio", "--session", SESSION, ok=False)
        self.assertEqual(refused.returncode, 6, refused.stderr)
        self.assertIn("sw_gpio", json.loads(refused.stderr)["problems"])
        self.assertFalse(ipdrain.is_draining(self.state, "gpio"))
        self.assertIsNotNone(self.registry.find("sw", "gpio"))

        # broker 投递后,remove 成功:pane 关闭、注册表清空、拓扑删除
        self.broker()
        self.wait_state(msg["msg_id"], DELIVERED)
        uart_msg = json.loads(self.send_from_dv("uart", 1)["stdout"])     # 其他 IP 照常
        self.wait_state(uart_msg["msg_id"], DELIVERED)
        time.sleep(2)   # 等 broker 放行 gpio 的槽位、假 agent 回到 idle
        panes = {r.pane_id for r in self.registry.list() if r.ip_id == "gpio"}
        removed = json.loads(self.a2a("ip", "remove", "gpio", "--session", SESSION).stdout)
        self.assertTrue(removed["removed"])
        self.assertEqual(panes & self.panes(), set())
        self.assertEqual([r for r in self.registry.list() if r.ip_id == "gpio"], [])
        self.assertNotIn("gpio", self.store.current().ips)
        self.assertFalse(ipdrain.is_draining(self.state, "gpio"))
        self.assertEqual(len([e for e in self.audit.read() if e.get("state") == "IP_REMOVED"]), 1)
        self.assertIsNotNone(client.agent_get("dv_uart"))                 # uart 不受影响

        # 同名 IP 再加回来能正常工作,队列序号接着排(新的假 dv 从第 1 次输入重新计数,先清掉旧日志)
        for name in ("dv_gpio.log", "dv_gpio.log.events", "sw_gpio.log"):
            (self.state / name).unlink(missing_ok=True)
        self.a2a("ip", "add", "gpio", "--session", SESSION)
        again = json.loads(self.send_from_dv("gpio", 1)["stdout"])
        self.assertEqual(again["queue_seq"], msg["queue_seq"] + 1)
        self.wait_state(again["msg_id"], DELIVERED)

    def test_draining_rejects_sends_and_undrain_restores(self):
        self.a2a("ip", "add", "gpio", "--session", SESSION)
        with ipdrain.drain_exclusive(self.state, "gpio"):
            ipdrain.write_marker(self.state, "gpio", "op-test", "测试")
        rejected = self.send_from_dv("gpio", 1)
        self.assertEqual((rejected["rc"], json.loads(rejected["stderr"])["rejected"]), (4, "ip_draining"))
        self.assertEqual(self.spool.pending("sw_gpio"), [])
        self.a2a("ip", "undrain", "gpio", "--session", SESSION)
        ok = self.send_from_dv("gpio", 2)
        self.assertEqual(ok["rc"], 0, ok["stderr"])
        self.assertEqual(self.spool.get(json.loads(ok["stdout"])["msg_id"]).state, QUEUED)


@unittest.skipUnless(ENABLED and FNX_ENABLED and HAVE_TOOLS and all(os.path.exists(p) for p in FNX.values()),
                     "设置 A2A_INTEGRATION=1 与 A2A_INTEGRATION_FNX=1,并安装 fnx_dv / fnx_sw")
class RealFnxIpOps(_Base):
    """真实 fnx:ip add 启动、ip remove 清理。不发任何提示词。"""

    def roles(self):
        return {r: {"label": r, "launcher": FNX[r]} for r in ("dv", "sw")}

    def test_real_fnx_add_and_remove(self):
        work = self.state / "work"
        out = json.loads(self.a2a("ip", "add", "gpio", "--cwd", str(work / "{ip}" / "{role}"),
                                  "--session", SESSION).stdout)
        self.assertEqual(out["roles"], {"dv": "spawned", "sw": "spawned"})
        pids = {}
        for role in ("dv", "sw"):
            info = client.pane_process_info(self.registry.find(role, "gpio").pane_id)
            pids[role] = info["foreground_processes"][0]["pid"]
        self.a2a("ip", "remove", "gpio", "--session", SESSION)
        time.sleep(1)
        self.assertEqual([r for r in pids.values() if Path(f"/proc/{r}").exists()], [])   # fnx 进程已退出
        self.assertNotIn("gpio", self.store.current().ips)
        for role in ("dv", "sw"):   # fnx 为临时工作目录建的会话目录:没发提示词,应为空,删掉
            d = Path(FNX[role]).parents[1] / "agent" / "sessions" / ("-" + str(work / "gpio" / role).replace("/", "-") + "--")
            if d.exists():
                self.assertEqual(list(d.iterdir()), [])
                d.rmdir()


if __name__ == "__main__":
    unittest.main()
