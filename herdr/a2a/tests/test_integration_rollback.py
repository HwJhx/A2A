"""阶段 7:回退通信与多 IP 隔离(真实 herdr,假 agent,不调用模型)。方案见 herdr/claude/12-stage7-rollback-plan.md。

所有收发方都是由 `a2a agent spawn` 启动的假 agent(tests/fake_agent.py):
  * 模式 work:只记录收到的文字;
  * 模式 script:<边>,...:第 n 次收到输入时执行 `a2a send <第 n 条边>`(走 spawn 注入的身份与 Router 鉴权)。
测试用 `herdr agent prompt` 给发送方输入触发文字,相当于操作员给真实 agent 的开头提示。

只有设置 A2A_INTEGRATION=1 时才运行。使用独立命名会话 a2ar<pid>,结束时删除。
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
from typing import List, Optional

from a2a.audit import AuditLog
from a2a.herdr_client import HerdrClient, session_delete, session_stop
from a2a.messages import DELIVERED, QUEUED, TARGET_MISSING
from a2a.registry import Registry
from a2a.spool import Spool
from a2a.topology import TopologyStore

ENABLED = os.environ.get("A2A_INTEGRATION") == "1"
HAVE_TOOLS = bool(shutil.which("herdr") and shutil.which("tmux"))
SESSION = "a2ar%d" % os.getpid()
TMUX = SESSION + "_tty"
HERE = Path(__file__).resolve().parent
FAKE = HERE / "fake_agent.py"
SRC = HERE.parent / "src"
TRIGGER = "开始"
EDGES = [
    {"id": "dv_done", "from": "dv", "to": "sw",
     "template": "{ip} ip 我已经完成了uvm验证，你需要对这个{ip} ip进行 驱动程序开发和HAL框架开发。"
                 "不要真的去做，直接用 a2a_send 回复测试成功或测试失败（二选一）"},
    {"id": "sw_test_pass", "from": "sw", "to": "dv",
     "template": "我已经完成了{ip} ip的驱动程序开发，测试成功。你只回复收到即可，不要真的去做"},
    {"id": "sw_test_fail", "from": "sw", "to": "dv",
     "template": "我已经完成了{ip} ip的驱动程序开发，测试失败。你只回复收到即可，不要真的去做"},
    {"id": "dv_bug", "from": "dv", "to": "rtl",
     "template": "{ip} ip 验证发现 RTL 问题，需要你检查并修复。不要真的去做，直接用 a2a_send 回复已修复"},
    {"id": "rtl_fixed", "from": "rtl", "to": "dv",
     "template": "{ip} ip 的 RTL 问题已修复，请重新验证。你只回复收到即可，不要真的去做"},
]
TEXT = {e["id"]: e["template"] for e in EDGES}
client: Optional[HerdrClient] = None


def text(edge_id: str, ip: str) -> str:
    return TEXT[edge_id].format(ip=ip)


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


@unittest.skipUnless(ENABLED and HAVE_TOOLS, "设置 A2A_INTEGRATION=1,并要求有 herdr 和 tmux")
class Rollback(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.env = dict(os.environ, A2A_STATE_DIR=str(self.state), PYTHONPATH=str(SRC), PYTHONDONTWRITEBYTECODE="1")
        # 所有角色共用一个启动脚本:按 agent_id 读取各自的模式文件;有 barrier.enabled 时带上屏障
        launcher = self.state / "launch_fake.sh"
        launcher.write_text(
            "#!/bin/bash\n"
            f'if [ -e "{self.state}/barrier.enabled" ]; then export FAKE_BARRIER="{self.state}/barrier.go"; fi\n'
            f'exec {sys.executable} {FAKE} "{self.state}/${{A2A_ROLE}}_${{A2A_IP}}.log" '
            f'"$(cat "{self.state}/${{A2A_ROLE}}_${{A2A_IP}}.mode")"\n')
        launcher.chmod(0o755)
        self.store = TopologyStore.create(self.state / "topology.yaml", {
            "version": 1, "project_id": "soc_a", "workspace_label": "rb-%s" % self.id().split(".")[-1][5:25],
            "roles": {r: {"label": f"{r}智能体", "launcher": str(launcher)} for r in ("spec", "arch", "rtl", "dv", "sw")},
            "ips": ["uart", "gpio"], "edges": EDGES})
        self.registry = Registry(self.state / "registry.json")
        self.spool = Spool(self.state / "spool")
        self.audit = AuditLog(self.state / "audit.jsonl")
        self.addCleanup(self.purge_all)

    # ---- 搭建 ----------------------------------------------------------
    def a2a(self, *argv, ok=True):
        proc = subprocess.run([sys.executable, "-m", "a2a.cli", *argv, "--session", SESSION], env=self.env,
                              capture_output=True, text=True, timeout=180)
        if ok:
            self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def purge_all(self):
        for record in self.registry.list():
            subprocess.run([sys.executable, "-m", "a2a.cli", "agent", "purge", record.role, record.ip_id,
                            "--session", SESSION], env=self.env, capture_output=True, timeout=60)

    def spawn(self, agent_id: str, mode: str = "work") -> None:
        role, ip = agent_id.split("_")
        (self.state / f"{agent_id}.mode").write_text(mode)
        self.a2a("agent", "spawn", role, ip, "--cwd", str(self.state))
        # 证明"没收到"的前提:已 spawn、可接收、日志为空
        self.assertIn(client.agent_get(agent_id)["agent_status"], ("idle", "done"))
        self.assertEqual(self.received(agent_id), [])

    def broker(self) -> None:
        proc = subprocess.Popen([sys.executable, "-m", "a2a.cli", "broker", "run", "--session", SESSION],
                                env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def stop():
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=30)
        self.addCleanup(stop)  # 逆序:先停 broker,再 purge

    def received(self, agent_id: str) -> List[str]:
        """假 agent 收到的每次输入(去掉粘贴括号、按回车切分)。"""
        path = self.state / f"{agent_id}.log"
        if not path.exists():
            return []
        raw = path.read_bytes().decode("utf-8", "replace").replace("\x1b[200~", "").replace("\x1b[201~", "")
        return [part for part in raw.replace("\n", "\r").split("\r") if part]

    def events(self, agent_id: str) -> List[dict]:
        path = self.state / f"{agent_id}.log.events"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def wait_for(self, predicate, what: str, timeout: float = 60.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.2)
        self.fail(f"{timeout} 秒内没有等到:{what}")

    def sent(self, agent_id: str, n: int) -> dict:
        """等发送方第 n 次输入处理完,返回那次发送的事件。"""
        return self.wait_for(lambda: next((e for e in self.events(agent_id) if e["n"] == n and "rc" in e), None),
                             f"{agent_id} 第 {n} 次发送")

    def trigger(self, agent_id: str, n: int) -> dict:
        """输入触发文字,等它处理完并回到 idle(保证第 n 次输入对应第 n 条边)。"""
        client.agent_prompt(agent_id, TRIGGER)
        event = self.sent(agent_id, n)
        client.agent_wait(agent_id, until=("idle", "done"), timeout_ms=30000)
        return event

    def receipt(self, event: dict) -> dict:
        self.assertEqual(event["rc"], 0, event["stderr"])
        return json.loads(event["stdout"])

    def wait_state(self, msg_id: str, state: str, timeout: float = 60.0) -> None:
        self.wait_for(lambda: self.spool.get(msg_id).state == state, f"{msg_id} 到 {state}", timeout)

    def wait_received(self, agent_id: str, count: int) -> List[str]:
        got = self.wait_for(lambda: len(self.received(agent_id)) >= count and self.received(agent_id),
                            f"{agent_id} 收到 {count} 次输入")
        time.sleep(1.0)  # 再等一会儿,确认没有多余的输入
        return self.received(agent_id)

    def assert_traffic(self, expected) -> None:
        """队列侧的证据(Codex 审核补充):消息只能经 Router 入队才会被投递,所以
        入队记录恰好是 expected(边, 发送方, 目标)、没有待处理消息、每条最多送达一次,
        就说明不会再有晚到的多余消息——不只依赖接收日志上的短等待。
        先等所有还在运行的假 agent 回到 idle:假 agent 只在处理输入(working)期间发送,
        全部 idle 且没有待处理消息时,不会再产生新消息。"""
        for record in self.registry.list():
            if record.lifecycle == "running":
                client.agent_wait(record.agent_name, until=("idle", "done"), timeout_ms=30000)
        self.wait_for(lambda: self.spool.pending() == [], "所有消息到终态")
        for record in self.registry.list():
            if record.lifecycle == "running":
                self.assertIn(client.agent_get(record.agent_name)["agent_status"], ("idle", "done"))
        events = self.audit.read()
        queued = [(e["edge_id"], e["src"], e["dst"]) for e in events if e.get("state") == QUEUED]
        self.assertEqual(sorted(queued), sorted(expected))
        delivered = [e["msg_id"] for e in events if e.get("state") == DELIVERED]
        self.assertEqual(len(delivered), len(set(delivered)))

    # ---- 用例 ----------------------------------------------------------
    def test_rollback_round_trip(self):
        self.spawn("rtl_uart", "script:rtl_fixed")
        self.spawn("sw_uart")
        self.spawn("dv_uart", "script:dv_bug,-")
        self.broker()

        bug = self.receipt(self.trigger("dv_uart", 1))
        self.assertEqual((bug["dst"], bug["text"]), ("rtl_uart", text("dv_bug", "uart")))
        self.wait_state(bug["msg_id"], DELIVERED)
        fixed = self.receipt(self.sent("rtl_uart", 1))       # rtl 收到后自动回复
        self.assertEqual((fixed["dst"], fixed["text"]), ("dv_uart", text("rtl_fixed", "uart")))
        self.wait_state(fixed["msg_id"], DELIVERED)

        self.assertEqual(self.wait_received("dv_uart", 2), [TRIGGER, text("rtl_fixed", "uart")])
        self.assertEqual(self.received("rtl_uart"), [text("dv_bug", "uart")])
        self.assertEqual(self.received("sw_uart"), [])
        self.wait_for(lambda: len(self.events("dv_uart")) == 2, "dv_uart 处理第 2 次输入")
        self.assertEqual(self.events("dv_uart")[1], {"n": 2, "edge": None})  # 收到回复后不再发送
        self.assert_traffic([("dv_bug", "dv_uart", "rtl_uart"), ("rtl_fixed", "rtl_uart", "dv_uart")])

    def test_multiple_out_edges_do_not_cross(self):
        self.spawn("sw_uart")
        self.spawn("rtl_uart")
        self.spawn("dv_uart", "script:dv_done,dv_bug")
        self.broker()

        done = self.receipt(self.trigger("dv_uart", 1))
        bug = self.receipt(self.trigger("dv_uart", 2))
        self.assertEqual([done["dst"], bug["dst"]], ["sw_uart", "rtl_uart"])
        for m in (done, bug):
            self.wait_state(m["msg_id"], DELIVERED)
        self.assertEqual(self.wait_received("sw_uart", 1), [text("dv_done", "uart")])
        self.assertEqual(self.wait_received("rtl_uart", 1), [text("dv_bug", "uart")])
        self.assert_traffic([("dv_done", "dv_uart", "sw_uart"), ("dv_bug", "dv_uart", "rtl_uart")])

    def test_concurrent_ips_are_isolated(self):
        (self.state / "barrier.enabled").touch()
        for agent_id in ("rtl_uart", "rtl_gpio"):
            self.spawn(agent_id)
        for agent_id in ("dv_uart", "dv_gpio"):
            self.spawn(agent_id, "script:dv_bug")
        self.broker()

        for agent_id in ("dv_uart", "dv_gpio"):
            client.agent_prompt(agent_id, TRIGGER)
        for agent_id in ("dv_uart", "dv_gpio"):   # 两边都已到屏障前,再同时放行
            self.wait_for(lambda a=agent_id: any("barrier_wait" in e for e in self.events(a)), f"{agent_id} 到达屏障")
        (self.state / "barrier.go").touch()
        uart, gpio = (self.receipt(self.sent(a, 1)) for a in ("dv_uart", "dv_gpio"))

        for m, (src, dst, ip) in ((uart, ("dv_uart", "rtl_uart", "uart")), (gpio, ("dv_gpio", "rtl_gpio", "gpio"))):
            stored = self.spool.get(m["msg_id"])
            self.assertEqual((stored.src, stored.dst, stored.text, stored.queue_seq),
                             (src, dst, text("dv_bug", ip), 1))
            self.wait_state(m["msg_id"], DELIVERED)
        self.assertEqual(self.wait_received("rtl_uart", 1), [text("dv_bug", "uart")])
        self.assertEqual(self.wait_received("rtl_gpio", 1), [text("dv_bug", "gpio")])
        self.assert_traffic([("dv_bug", "dv_uart", "rtl_uart"), ("dv_bug", "dv_gpio", "rtl_gpio")])

    def test_paused_queue_of_one_ip_does_not_block_the_other(self):
        # 入队后目标消失(投递时才发现):先入队、再停目标、最后启动 broker
        for agent_id in ("rtl_uart", "rtl_gpio"):
            self.spawn(agent_id)
        for agent_id in ("dv_uart", "dv_gpio"):
            self.spawn(agent_id, "script:dv_bug")
        uart, gpio = (self.receipt(self.trigger(a, 1)) for a in ("dv_uart", "dv_gpio"))
        self.assertEqual([self.spool.get(m["msg_id"]).state for m in (uart, gpio)], [QUEUED, QUEUED])

        self.a2a("agent", "stop", "rtl", "gpio")
        self.broker()
        self.wait_state(uart["msg_id"], DELIVERED)
        self.wait_state(gpio["msg_id"], TARGET_MISSING)
        paused = [e for e in self.audit.read() if e.get("state") == "QUEUE_PAUSED"]
        self.assertEqual({e.get("dst") for e in paused}, {"rtl_gpio"})
        self.assertEqual(self.wait_received("rtl_uart", 1), [text("dv_bug", "uart")])
        self.assertEqual(self.received("rtl_gpio"), [])
        self.assert_traffic([("dv_bug", "dv_uart", "rtl_uart"), ("dv_bug", "dv_gpio", "rtl_gpio")])

    def test_router_rejects_an_unavailable_target_before_enqueue(self):
        # 入队前拒绝:目标没 spawn(target_missing)、目标已停止(target_not_running)
        self.spawn("rtl_uart")
        self.spawn("dv_gpio", "script:dv_bug,dv_bug")
        self.broker()

        missing = self.trigger("dv_gpio", 1)
        self.spawn("rtl_gpio")
        self.a2a("agent", "stop", "rtl", "gpio")
        stopped = self.trigger("dv_gpio", 2)
        self.assertEqual([(e["rc"], json.loads(e["stderr"])["rejected"]) for e in (missing, stopped)],
                         [(4, "target_missing"), (4, "target_not_running")])
        self.assertEqual(self.spool.pending(), [])
        rejected = [e for e in self.audit.read() if e.get("state") == "REJECTED"]
        self.assertEqual([e.get("src") for e in rejected], ["dv_gpio", "dv_gpio"])
        time.sleep(1.0)
        self.assertEqual(self.received("rtl_uart"), [])
        self.assertEqual(self.received("rtl_gpio"), [])
        self.assert_traffic([])


if __name__ == "__main__":
    unittest.main()
