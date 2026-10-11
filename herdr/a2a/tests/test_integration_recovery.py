"""阶段 8:注册表自动恢复(真实 herdr;假 agent,另有一项真实 fnx,都不调用模型)。18 号方案 §5。

真的停止并重新 attach herdr 会话,再执行 `a2a agent recover`。只有设置 A2A_INTEGRATION=1 时才运行;
真实 fnx 一项还需要 A2A_INTEGRATION_FNX=1。每个用例使用独立命名会话 a2arc<pid>_<n>,结束时删除。
"""
from __future__ import annotations

import fcntl
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from a2a.herdr_client import HerdrClient, session_delete, session_list, session_stop
from a2a.messages import DELIVERED
from a2a.registry import Registry
from a2a.spool import Spool
from a2a.topology import TopologyStore

ENABLED = os.environ.get("A2A_INTEGRATION") == "1"
FNX_ENABLED = os.environ.get("A2A_INTEGRATION_FNX") == "1"
HAVE_TOOLS = bool(shutil.which("herdr") and shutil.which("tmux"))
HERE = Path(__file__).resolve().parent
FAKE = HERE / "fake_agent.py"
SRC = HERE.parent / "src"
FNX = {"dv": os.path.expanduser("~/.forenyx/fnx_dv/bin/fnx_dv"),
       "sw": os.path.expanduser("~/.forenyx/fnx_sw/bin/fnx_sw")}
_counter = itertools.count()


def broker_lock_held(state: Path) -> bool:
    """非阻塞试锁 broker 单实例锁:拿不到说明有 broker 持有它(试到了就立即释放)。"""
    with (state / "broker.lock").open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False


class _Base(unittest.TestCase):
    def setUp(self):
        self.session = "a2arc%d_%d" % (os.getpid(), next(_counter))
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.env = dict(os.environ, A2A_STATE_DIR=str(self.state), PYTHONPATH=str(SRC), PYTHONDONTWRITEBYTECODE="1")
        self.store = TopologyStore.create(self.state / "topology.yaml", {
            "version": 1, "project_id": "soc_a", "workspace_label": "rc", "roles": self.roles(), "ips": ["uart"],
            "edges": [{"id": "dv_done", "from": "dv", "to": "sw", "template": "{ip} 已完成验证"}]})
        self.registry = Registry(self.state / "registry.json")
        self.spool = Spool(self.state / "spool")
        self.attach()
        self.addCleanup(self.cleanup_session)

    def roles(self):
        raise NotImplementedError

    def attach(self):
        subprocess.run(["tmux", "new-session", "-d", "-x", "200", "-y", "50", "-s", self.session + "_tty",
                        "cd %s && herdr session attach %s" % (os.path.expanduser("~"), self.session)], check=True)
        self.client = HerdrClient(self.session)
        deadline = time.monotonic() + 30
        while not self.client.is_server_running():
            if time.monotonic() > deadline:
                raise RuntimeError("会话没有启动")
            time.sleep(0.3)

    def restart_session(self):
        """停止会话(进程全部退出)再重新 attach(pane 与 pane ID 保留,只剩 shell;18 号方案 §3.1)。"""
        session_stop(self.session)
        subprocess.run(["tmux", "kill-session", "-t", self.session + "_tty"], capture_output=True)
        time.sleep(1)
        self.attach()
        time.sleep(2)

    def cleanup_session(self):
        for record in self.registry.list():
            subprocess.run([sys.executable, "-m", "a2a.cli", "agent", "purge", record.role, record.ip_id,
                            "--session", self.session], env=self.env, capture_output=True, timeout=120)
        if any(r["name"] == self.session for r in session_list()):
            for fn in (session_stop, session_delete):
                try:
                    fn(self.session)
                except Exception as exc:
                    print("清理 %s 失败: %s" % (fn.__name__, exc))
        subprocess.run(["tmux", "kill-session", "-t", self.session + "_tty"], capture_output=True)

    def a2a(self, *argv, ok=True):
        proc = subprocess.run([sys.executable, "-m", "a2a.cli", *argv], env=self.env, capture_output=True,
                              text=True, timeout=300)
        if ok:
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        return proc


@unittest.skipUnless(ENABLED and HAVE_TOOLS, "设置 A2A_INTEGRATION=1,并要求有 herdr 和 tmux")
class FakeAgentRecovery(_Base):
    def roles(self):
        launcher = self.state / "launch_fake.sh"
        launcher.write_text(
            "#!/bin/bash\n"
            f'exec {sys.executable} {FAKE} "{self.state}/${{A2A_ROLE}}_${{A2A_IP}}.log" '
            f'"$( [ "$A2A_ROLE" = dv ] && echo script:dv_done || echo work )"\n')
        launcher.chmod(0o755)
        return {"dv": {"label": "dv", "launcher": str(launcher)}, "sw": {"label": "sw", "launcher": str(launcher)},
                "rtl": {"label": "rtl", "launcher": str(launcher)}}

    def test_recover_after_a_session_restart_then_messages_flow(self):
        work = self.state / "work"
        work.mkdir()
        for role in ("dv", "sw", "rtl"):
            self.a2a("agent", "spawn", role, "uart", "--cwd", str(work), "--session", self.session)
        self.a2a("agent", "stop", "rtl", "uart", "--session", self.session)        # 有意停掉的不恢复
        panes = {r.agent_id: r.pane_id for r in self.registry.list()}
        self.restart_session()
        self.assertEqual(self.client.agent_list(), [])                             # 会话重启后 agent 都不在了
        self.client.pane_close(panes["sw_uart"])                                   # sw 的 pane 也丢了:应新建 pane

        dry = json.loads(self.a2a("agent", "recover", "--session", self.session, "--dry-run").stdout)
        self.assertEqual(dry["agents"], {"dv_uart": "restore", "sw_uart": "restore", "rtl_uart": "kept_stopped"})
        out = json.loads(self.a2a("agent", "recover", "--session", self.session).stdout)
        self.assertEqual(out["agents"]["rtl_uart"], "kept_stopped")
        self.assertEqual(out["agents"]["dv_uart"], f"restored({panes['dv_uart']})")    # 原 pane 重启
        new_sw = self.registry.get("sw_uart").pane_id
        self.assertNotEqual(new_sw, panes["sw_uart"])
        self.assertEqual(out["agents"]["sw_uart"], f"restored({new_sw})")              # 新建 pane
        for agent_id, pane in (("dv_uart", panes["dv_uart"]), ("sw_uart", new_sw)):
            self.assertEqual(self.client.agent_get(agent_id)["pane_id"], pane)       # 名字恢复
            # 两种恢复方式,agent 进程都在登记的工作目录里
            pid = self.client.pane_process_info(pane)["foreground_processes"][0]["pid"]
            self.assertEqual(os.readlink(f"/proc/{pid}/cwd"), str(work))
        self.assertEqual(self.registry.get("rtl_uart").lifecycle, "stopped")

        # 恢复后 broker 正常投递
        broker = subprocess.Popen([sys.executable, "-m", "a2a.cli", "broker", "run", "--session", self.session],
                                  env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (broker.terminate(), broker.wait(timeout=90)))
        # 等 broker 真的持有单实例锁(锁文件在前面的 recover 时就已创建,不能用"文件存在"判断)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not broker_lock_held(self.state):
            self.assertIsNone(broker.poll(), "broker 提前退出")
            time.sleep(0.2)
        self.assertTrue(broker_lock_held(self.state), "broker 30 秒内没有拿到锁")
        # broker 在运行时 recover 被拒(退出码 3)
        self.assertEqual(self.a2a("agent", "recover", "--session", self.session, ok=False).returncode, 3)
        self.client.agent_prompt("dv_uart", "开始")
        events = self.state / "dv_uart.log.events"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not (events.exists() and "rc" in events.read_text()):
            time.sleep(0.2)
        sent = json.loads(events.read_text().splitlines()[-1])
        self.assertEqual(sent["rc"], 0, sent["stderr"])
        msg_id = json.loads(sent["stdout"])["msg_id"]
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and self.spool.get(msg_id).state != DELIVERED:
            time.sleep(0.2)
        self.assertEqual(self.spool.get(msg_id).state, DELIVERED)


@unittest.skipUnless(ENABLED and FNX_ENABLED and HAVE_TOOLS and all(os.path.exists(p) for p in FNX.values()),
                     "设置 A2A_INTEGRATION=1 与 A2A_INTEGRATION_FNX=1,并安装 fnx_dv / fnx_sw")
class RealFnxRecovery(_Base):
    """真实 fnx:spawn → 会话重启 → recover。不发任何提示词。"""

    def roles(self):
        return {r: {"label": r, "launcher": FNX[r]} for r in ("dv", "sw")}

    def test_real_fnx_recover_after_session_restart(self):
        work = self.state / "work"
        work.mkdir()
        for role in ("dv", "sw"):
            self.a2a("agent", "spawn", role, "uart", "--cwd", str(work), "--session", self.session)
        panes = {r.agent_id: r.pane_id for r in self.registry.list()}
        self.restart_session()
        out = json.loads(self.a2a("agent", "recover", "--session", self.session).stdout)
        for agent_id, pane in panes.items():
            self.assertEqual(out["agents"][agent_id], f"restored({pane})")
            self.assertEqual(self.client.agent_get(agent_id)["pane_id"], pane)
            # 恢复后的 fnx 进程带着身份:会话重启后 pane 的新 shell 没有这些变量,靠启动命令自带
            pid = self.client.pane_process_info(pane)["foreground_processes"][0]["pid"]
            raw = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
            environ = dict(x.decode().split("=", 1) for x in raw if b"=" in x)
            role = agent_id.split("_")[0]
            self.assertEqual((environ.get("A2A_ROLE"), environ.get("A2A_IP"), environ.get("A2A_STATE_DIR")),
                             (role, "uart", str(self.state)))
        for role in ("dv", "sw"):   # fnx 为该工作目录建的会话目录:没发提示词,应为空,删掉
            d = Path(FNX[role]).parents[1] / "agent" / "sessions" / ("--" + str(work).strip("/").replace("/", "-") + "--")
            self.addCleanup(lambda d=d: d.exists() and not any(d.iterdir()) and d.rmdir())


if __name__ == "__main__":
    unittest.main()
