"""阶段 5e:端到端(真实 herdr,不调用模型)。

  真实 shell pane(带 dv_uart 身份)里执行 `a2a send dv_done`
    -> Router 鉴权、渲染固定句式、入队
    -> 常驻 broker 子进程(`a2a broker run`)投递
    -> 假 agent sw_uart(tests/fake_agent.py)收到文字
  并验证:broker 停止期间发出的消息在重启后补投、顺序不乱;pane 里 `a2a status` 能查到结果;
  冒充别人身份的发送被拒绝。

只有设置 A2A_INTEGRATION=1 时才运行。使用独立命名会话 a2ae<pid>,结束时删除。
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
from a2a.herdr_client import HerdrClient, session_delete, session_stop
from a2a.identity import AgentIdentity, identity_env
from a2a.messages import DELIVERED, QUEUED
from a2a.registry import Registry
from a2a.spool import Spool
from a2a.topology import TopologyStore

ENABLED = os.environ.get("A2A_INTEGRATION") == "1"
HAVE_TOOLS = bool(shutil.which("herdr") and shutil.which("tmux"))
SESSION = "a2ae%d" % os.getpid()
TMUX = SESSION + "_tty"
HERE = Path(__file__).resolve().parent
FAKE = HERE / "fake_agent.py"
SRC = HERE.parent / "src"
TEMPLATE = "{ip}已完成UVM验证,请开发驱动。"
TOPOLOGY = {"version": 1, "project_id": "soc_a", "roles": {"dv": {}, "sw": {}}, "ips": ["uart"],
            "edges": [{"id": "dv_done", "from": "dv", "to": "sw", "template": TEMPLATE}]}
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


def _stop(proc: subprocess.Popen) -> None:
    """结束 broker 子进程并回收(避免遗留进程与未关闭的管道)。"""
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=30)
    if proc.stderr:
        proc.stderr.close()


@unittest.skipUnless(ENABLED and HAVE_TOOLS, "设置 A2A_INTEGRATION=1,并要求有 herdr 和 tmux")
class EndToEnd(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.base_env = {"A2A_STATE_DIR": str(self.state), "PYTHONPATH": str(SRC), "PYTHONDONTWRITEBYTECODE": "1"}
        self.store = TopologyStore.create(self.state / "topology.yaml", TOPOLOGY)
        self.registry = Registry(self.state / "registry.json")
        self.spool = Spool(self.state / "spool")
        self.audit = AuditLog(self.state / "audit.jsonl")
        self.recv_log = self.state / "sw_uart.log"

    # ---- 搭建 ----------------------------------------------------------
    def tab(self, label, env=None):
        created = client.tab_create(label=label, cwd=str(self.state), env=env)
        self.addCleanup(self._close, created.tab_id)
        return created

    @staticmethod
    def _close(tab_id):
        try:
            client.tab_close(tab_id)
        except Exception:
            pass

    def start_receiver(self):
        created = self.tab("sw_uart")
        client.pane_run(created.pane_id, "bash -c 'exec -a pi %s %s %s work'" % (sys.executable, FAKE, self.recv_log))
        client.wait_for_agent_detected(created.pane_id, timeout_s=20)
        client.agent_wait(created.pane_id, until=("idle", "done"), timeout_ms=15000)
        client.agent_rename(created.pane_id, "sw_uart")
        self.registry.register(AgentIdentity.create(self.store.current(), "sw", "uart"), session=SESSION,
                               workspace_id="w1", tab_id=created.tab_id, pane_id=created.pane_id)
        return created.pane_id

    def start_sender(self, role="dv", ip="uart", register=True):
        env = dict(self.base_env, **identity_env("soc_a", role, ip))
        created = self.tab(f"{role}_{ip}_shell", env=env)
        if register:
            self.registry.register(AgentIdentity.create(self.store.current(), role, ip), session=SESSION,
                                   workspace_id="w1", tab_id=created.tab_id, pane_id=created.pane_id,
                                   agent_name=f"{role}_{ip}")
        return created.pane_id

    def run_in_pane(self, pane, command, out_file, timeout=30.0):
        """在真实 pane 的 shell 里执行命令,把 stdout / 退出码写进文件后读回。"""
        out_file.unlink(missing_ok=True)
        client.pane_run(pane, f"{command} > {out_file} 2>{out_file}.err; echo $? > {out_file}.rc")
        rc_file = Path(str(out_file) + ".rc")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if rc_file.exists() and rc_file.read_text().strip():
                return int(rc_file.read_text()), out_file.read_text(), Path(str(out_file) + ".err").read_text()
            time.sleep(0.2)
        raise AssertionError(f"pane 里的命令没有在 {timeout} 秒内结束:{command}")

    def a2a_send_from(self, pane, n):
        rc, out, err = self.run_in_pane(pane, f"{sys.executable} -m a2a.cli send dv_done", self.state / f"send{n}.json")
        self.assertEqual(rc, 0, err)
        return json.loads(out)

    def broker(self):
        proc = subprocess.Popen([sys.executable, "-m", "a2a.cli", "broker", "run", "--session", SESSION],
                                env=dict(os.environ, **self.base_env), stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True)
        self.addCleanup(_stop, proc)
        return proc

    def wait_state(self, msg_id, state, timeout=60.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.spool.get(msg_id).state == state:
                return
            time.sleep(0.2)
        self.fail(f"{msg_id} 在 {timeout} 秒内没有到 {state},现在是 {self.spool.get(msg_id).state}")

    # ---- 用例 ----------------------------------------------------------
    def test_send_from_a_real_pane_through_the_broker_with_a_restart(self):
        self.start_receiver()
        sender = self.start_sender()
        proc = self.broker()

        first = self.a2a_send_from(sender, 1)
        self.assertEqual((first["dst"], first["text"], first["queue_seq"]), ("sw_uart", "uart已完成UVM验证,请开发驱动。", 1))
        self.wait_state(first["msg_id"], DELIVERED)

        proc.terminate()               # broker 停止期间继续发送
        proc.wait(timeout=30)
        self.assertEqual(proc.returncode, 0)
        queued = [self.a2a_send_from(sender, n) for n in (2, 3)]
        time.sleep(2)
        self.assertEqual([self.spool.get(m["msg_id"]).state for m in queued], [QUEUED, QUEUED])

        self.broker()                  # 重启后补投,按队列顺序
        for m in queued:
            self.wait_state(m["msg_id"], DELIVERED)

        # 接收方确实收到了三次固定句式
        time.sleep(0.5)
        received = self.recv_log.read_bytes().decode("utf-8", "replace")
        self.assertEqual(received.count(first["text"]), 3)
        delivered_order = [e["msg_id"] for e in self.audit.read() if e.get("state") == DELIVERED]
        self.assertEqual(delivered_order, [first["msg_id"]] + [m["msg_id"] for m in queued])

        # pane 里查状态
        rc, out, err = self.run_in_pane(sender, f"{sys.executable} -m a2a.cli status {queued[-1]['msg_id']}",
                                        self.state / "status.json")
        self.assertEqual((rc, json.loads(out)["state"]), (0, DELIVERED), err)

        # 会话名取自 herdr 注入 pane 的 HERDR_SESSION(之前只在命名会话实测过,这里再确认一次)
        self.assertEqual(self.spool.get(first["msg_id"]).session, SESSION)

    def test_impersonation_from_a_real_pane_is_rejected(self):
        self.start_receiver()
        # 一个 shell pane 声称自己是 sw_uart(该身份已登记在接收方的 pane 上)
        liar = self.start_sender(role="sw", ip="uart", register=False)
        rc, out, err = self.run_in_pane(liar, f"{sys.executable} -m a2a.cli send dv_done", self.state / "liar.json")
        self.assertEqual(rc, 4)
        self.assertEqual(json.loads(err)["rejected"], "identity")
        self.assertEqual(self.spool.pending(), [])


if __name__ == "__main__":
    unittest.main()
