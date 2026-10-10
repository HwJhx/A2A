"""阶段 5b 的真实 herdr 测试:投递引擎 + 假 agent(tests/fake_agent.py),不调用任何模型。

只有设置 A2A_INTEGRATION=1 时才运行(需要 herdr 和 tmux)。使用独立命名会话 a2ad<pid>,
结束时停止并删除它,不碰其他会话。判定依据是假 agent 的日志文件:文字有没有真的写进目标。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Optional

from a2a.audit import AuditLog
from a2a.delivery import DeliveryEngine
from a2a.herdr_client import HerdrClient, herdr_version, session_delete, session_stop
from a2a.identity import AgentIdentity
from a2a.messages import (DELIVERED, DELIVERY_UNCERTAIN, DISPATCHING, QUEUED, TARGET_BLOCKED, TARGET_MISSING, WAITING_TARGET,
                          Message, new_msg_id, now_iso)
from a2a.policy import BrokerConfig, semantics_verified
from a2a.registry import Registry
from a2a.spool import Spool

ENABLED = os.environ.get("A2A_INTEGRATION") == "1"
HAVE_TOOLS = bool(shutil.which("herdr") and shutil.which("tmux"))
SESSION = "a2ad%d" % os.getpid()
TMUX = SESSION + "_tty"
FAKE = Path(__file__).resolve().parent / "fake_agent.py"
SRC = Path(__file__).resolve().parents[1] / "src"
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


class _RealBase(unittest.TestCase):
    """真实 herdr 测试的公共部分(本身没有测试用例)。"""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.spool = Spool(self.dir / "spool")
        self.registry = Registry(self.dir / "registry.json")
        self.audit = AuditLog(self.dir / "audit.jsonl")
        self.log = self.dir / "agent.log"
        self.verified = semantics_verified(herdr_version())

    def start_agent(self, mode: str, name: str = "sw_uart", ip: str = "uart", log: Optional[Path] = None) -> str:
        created = client.tab_create(label="t_" + mode, cwd=os.path.expanduser("~"))
        self.addCleanup(self._close, created.tab_id)
        client.pane_run(created.pane_id, "bash -c 'exec -a pi %s %s %s %s'"
                        % (sys.executable, FAKE, log or self.log, mode))
        client.wait_for_agent_detected(created.pane_id, timeout_s=20)
        client.agent_wait(created.pane_id, until=("idle", "done"), timeout_ms=15000)
        client.agent_rename(created.pane_id, name)
        self.registry.register(AgentIdentity("soc_a", ip, "sw", "sw_" + ip), session=SESSION,
                               workspace_id="w1", tab_id=created.tab_id, pane_id=created.pane_id, agent_name=name)
        return created.pane_id

    @staticmethod
    def _close(tab_id):
        try:
            client.tab_close(tab_id)
        except Exception:
            pass

    def enqueue(self, text: str, ip: str = "uart") -> Message:
        return self.spool.enqueue(Message(
            msg_id=new_msg_id(), created_at=now_iso(), edge_id="dv_done", src="dv_" + ip, dst="sw_" + ip,
            project_id="soc_a", ip_id=ip, session=SESSION, text=text, state=QUEUED, topology_revision="r"))

    def deliver(self, message: Message, **cfg) -> Message:
        engine = DeliveryEngine(self.spool, self.registry, self.audit, client,
                                config=BrokerConfig(**cfg), semantics_verified=self.verified)
        return engine.deliver(message.msg_id)

    def written(self, text: str) -> bool:
        time.sleep(0.5)
        return self.log.exists() and text.encode() in self.log.read_bytes()

    def report(self, pane: str, state: str) -> None:
        client._call(["pane", "report-agent", pane, "--source", "a2a-fake", "--agent", "pi", "--state", state])

    def states(self, message):
        return [ev["state"] for ev in self.audit.read() if ev.get("msg_id") == message.msg_id]



def _stop(proc: subprocess.Popen) -> None:
    """结束 broker 子进程并回收(避免遗留进程与未关闭的管道)。"""
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=30)
    if proc.stderr:
        proc.stderr.close()


@unittest.skipUnless(ENABLED and HAVE_TOOLS, "设置 A2A_INTEGRATION=1,并要求有 herdr 和 tmux")
class RealDelivery(_RealBase):
    def test_idle_target_is_delivered_and_text_written(self):
        self.start_agent("work")
        m = self.enqueue("uart 验证完成 %s" % new_msg_id())
        result = self.deliver(m)
        self.assertEqual(result.state, DELIVERED, result.detail)
        self.assertTrue(self.written(m.text))
        self.assertEqual(self.audit.read()[-1].get("evidence"), "accepted_and_observed")

    def test_busy_target_is_waited_for(self):
        pane = self.start_agent("work")
        self.report(pane, "working")
        threading.Timer(3.0, self.report, args=(pane, "idle")).start()  # 3 秒后目标"忙完"
        m = self.enqueue("等我忙完 %s" % new_msg_id())
        started = time.monotonic()
        result = self.deliver(m)
        self.assertEqual(result.state, DELIVERED, result.detail)
        self.assertIn(WAITING_TARGET, self.states(m))
        self.assertGreaterEqual(time.monotonic() - started, 2.5)  # 确实等到了目标空闲才发
        self.assertTrue(self.written(m.text))

    def test_blocked_target_fails_without_writing(self):
        pane = self.start_agent("work")
        self.report(pane, "blocked")
        m = self.enqueue("不该写入 %s" % new_msg_id())
        result = self.deliver(m)
        self.assertEqual(result.state, TARGET_BLOCKED, result.detail)
        self.assertFalse(self.written(m.text))

    def test_accepted_but_never_started_is_uncertain_and_was_in_fact_written(self):
        # 这正是"不确定"的意义:herdr 接受了、目标没有表现出开始处理,但文字其实已经写进去了
        self.start_agent("silent")
        m = self.enqueue("写了但没反应 %s" % new_msg_id())
        result = self.deliver(m, observe_window_s=8)
        self.assertEqual(result.state, DELIVERY_UNCERTAIN, result.detail)
        self.assertTrue(self.written(m.text))

    def test_missing_agent_name_is_target_missing(self):
        self.start_agent("work", name="sw_other")  # 登记的名字存在,但随后被清除
        client.agent_rename("sw_other", None)
        m = self.enqueue("没有目标 %s" % new_msg_id())
        result = self.deliver(m)
        self.assertEqual(result.state, TARGET_MISSING, result.detail)
        self.assertFalse(self.written(m.text))


@unittest.skipUnless(ENABLED and HAVE_TOOLS, "设置 A2A_INTEGRATION=1,并要求有 herdr 和 tmux")
class RealBrokerProcess(_RealBase):
    """以子进程运行 `a2a broker run`,对接真实 herdr 会话与假 agent。"""

    def setUp(self):
        super().setUp()
        # broker 进程通过 A2A_STATE_DIR 找到同一份 spool / registry / audit
        self.state = self.dir
        self.env = dict(os.environ, A2A_STATE_DIR=str(self.state), PYTHONPATH=str(SRC),
                        PYTHONDONTWRITEBYTECODE="1")

    def broker(self, *extra: str) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, "-m", "a2a.cli", "broker", "run", "--session", SESSION, *extra],
                                env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        self.addCleanup(_stop, proc)
        return proc

    def wait_for(self, predicate, timeout=60.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.2)
        return False

    def test_broker_delivers_in_order_and_pauses_only_the_blocked_target(self):
        uart_log, gpio_log = self.dir / "uart.log", self.dir / "gpio.log"
        self.start_agent("work", "sw_uart", "uart", uart_log)
        gpio = self.start_agent("work", "sw_gpio", "gpio", gpio_log)
        self.report(gpio, "blocked")
        uart = [self.enqueue("UART-%d-%s" % (i, new_msg_id())) for i in range(3)]
        g1, g2 = self.enqueue("GPIO-1", "gpio"), self.enqueue("GPIO-2", "gpio")
        proc = self.broker()
        try:
            self.assertTrue(self.wait_for(lambda: all(self.spool.get(m.msg_id).state == DELIVERED for m in uart)),
                            [self.spool.get(m.msg_id).state for m in uart])
            self.assertTrue(self.wait_for(lambda: self.spool.get(g1.msg_id).state == TARGET_BLOCKED))
        finally:
            proc.terminate()
            proc.wait(timeout=30)
        text = uart_log.read_bytes().decode("utf-8", "replace")
        positions = [text.index(m.text) for m in uart]
        self.assertEqual(positions, sorted(positions))          # 按队列顺序写入
        self.assertEqual(self.spool.get(g2.msg_id).state, QUEUED)  # blocked 的目标暂停,后续不发
        self.assertFalse(gpio_log.exists() and b"GPIO" in gpio_log.read_bytes())
        self.assertEqual(proc.returncode, 0, proc.stderr.read() if proc.stderr else "")

    def test_kill_minus_9_mid_prompt_is_not_resent(self):
        self.start_agent("silent")  # 不报告 working:herdr 的 --wait 会等约 5 秒,足够在中途杀掉
        m = self.enqueue("只能出现一次 %s" % new_msg_id())
        proc = self.broker()
        self.assertTrue(self.wait_for(lambda: self.spool.get(m.msg_id).state == DISPATCHING, 30))
        time.sleep(1.0)
        proc.kill()
        proc.wait(timeout=10)
        again = subprocess.run([sys.executable, "-m", "a2a.cli", "broker", "run", "--session", SESSION, "--once"],
                               env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(self.spool.get(m.msg_id).state, DELIVERY_UNCERTAIN)
        time.sleep(0.5)
        self.assertEqual(self.log.read_bytes().count(m.text.encode()), 1)  # 已写入一次,没有重发

    def test_sigterm_mid_prompt_finishes_the_delivery_before_exit(self):
        # 日常停机(systemctl stop / restart)发 SIGTERM:正在进行的 prompt 要等它返回再退出,
        # 不能把消息留在 DISPATCHING(重启后会变成 DELIVERY_UNCERTAIN,需要人工裁定)
        self.start_agent("slow")  # 收到回车 3 秒后才报告 working:prompt --wait 在这段时间里保持进行中
        m = self.enqueue("停机时正在投递 %s" % new_msg_id())
        proc = self.broker()
        self.assertTrue(self.wait_for(lambda: self.spool.get(m.msg_id).state == DISPATCHING, 30))
        time.sleep(0.5)
        proc.terminate()
        proc.wait(timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr.read() if proc.stderr else "")
        self.assertEqual(self.spool.get(m.msg_id).state, DELIVERED)
        time.sleep(0.5)
        self.assertEqual(self.log.read_bytes().count(m.text.encode()), 1)


if __name__ == "__main__":
    unittest.main()
