"""阶段 5f 的真实 herdr 测试:agent 生命周期(`a2a agent ...` 命令行)。

A2A_INTEGRATION=1:       假 agent(通过启动脚本 + 覆盖 exec 启动,和真实 fnx 同一路径),含消息回归。
A2A_INTEGRATION_FNX=1:   真实 fnx_dv / fnx_sw 的 spawn / stop / restore / purge。**不发任何提示词,不调用模型。**
使用独立命名会话 a2al<pid>,结束时删除。
"""
from __future__ import annotations

import json
import os
import shutil
import signal
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
from a2a.lifecycle import Lifecycle, LifecycleError
from a2a.messages import DELIVERED, QUEUED, TARGET_MISSING, Message, new_msg_id, now_iso
from a2a.policy import BrokerConfig, semantics_verified
from a2a.procsignal import ProcSignaler
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

    def test_stop_is_not_recorded_while_a_child_of_the_agent_survives(self):
        # 19 号方案 §A:agent(组长)退出但它启动的、忽略 SIGTERM 的子进程还在同一进程组 → 不登记 stopped
        launcher = Path(self.store.current().roles["sw"].launcher)
        launcher.write_text(f"#!/bin/bash\nexport FAKE_ROLE=sw\n( trap '' TERM; sleep 300 ) &\n"
                            f'exec {sys.executable} {FAKE} "{self.state}/$A2A_ROLE""_""$A2A_IP"".log" work\n')
        _, sw, _ = self.a2a("agent", "spawn", "sw", "uart")
        pgid = self.registry.get("sw_uart").pgid
        self.reap_at_end(pgid)                                           # 断言失败也不把残留子进程留在 VM 上
        self.assertEqual(pgid, client.pane_process_info(sw["pane_id"])["foreground_process_group_id"])
        rc, _, err = self.a2a("agent", "stop", "sw", "uart", ok=False)
        self.assertEqual(rc, 6, err)
        self.assertIn("没有完全退出", err)
        self.assertTrue(self.wait_gone("sw_uart"))                       # agent 本身确实退出了
        self.assertEqual(self.registry.get("sw_uart").lifecycle, "running")
        # 实测:组长退出后 shell 收回终端,pane 看起来只剩 shell;残留的子进程成了后台进程,只能按进程组查到
        info = client.pane_process_info(sw["pane_id"])
        self.assertEqual(info["foreground_process_group_id"], info["shell_pid"])
        members = ProcSignaler().group_members(pgid)
        self.assertTrue(members)
        names = {Path(f"/proc/{p}/cmdline").read_bytes().split(b"\0")[0].decode() for p in members}
        self.assertTrue(names <= {"bash", "sleep", "/bin/bash", "/usr/bin/sleep"}, names)
        rc, _, err = self.a2a("agent", "stop", "sw", "uart", ok=False)   # agent 已不在:按登记的进程组核对
        self.assertEqual(rc, 6, err)
        os.killpg(pgid, signal.SIGKILL)                                  # 人工处理:结束残留子进程
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and ProcSignaler().group_members(pgid):
            time.sleep(0.2)
        self.a2a("agent", "stop", "sw", "uart")                          # 现在可以登记 stopped
        self.assertEqual(self.registry.get("sw_uart").lifecycle, "stopped")

    # ---- 19 号方案 §A 复核:启动失败或被 Ctrl-C 中断时不留下 a2a 管不了的 agent(修复前在此复现过)
    def switchable_launcher(self, role):
        """启动脚本按 <状态目录>/fake_mode 选择假 agent 的模式(默认 work)。
        修复失效时被测代码会留下未登记的 pane 和进程:测试结束时自己清理,不影响后面的测试。"""
        before = self.panes()

        def close_leaked_panes():
            for pane_id in self.panes() - before:
                try:
                    client.pane_close(pane_id)
                except Exception:
                    pass
        self.addCleanup(close_leaked_panes)
        launcher = Path(self.store.current().roles[role].launcher)
        launcher.write_text(f"#!/bin/bash\nexport FAKE_ROLE={role}\n"
                            f'exec {sys.executable} {FAKE} "{self.state}/$A2A_ROLE""_""$A2A_IP"".log" '
                            f'"$(cat {self.state}/fake_mode 2>/dev/null || echo work)"\n')
        return self.state / "fake_mode"

    def started_pid(self, agent_id, timeout=30):
        pid_file = self.state / f"{agent_id}.log.pid"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not pid_file.exists():
            time.sleep(0.2)
        pid = int(pid_file.read_text())
        self.addCleanup(self.kill_if_alive, pid)
        return pid

    @staticmethod
    def kill_if_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    @staticmethod
    def wait_dead(pid, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if Path(f"/proc/{pid}/stat").read_text().split(") ")[1][0] == "Z":
                    return True
            except FileNotFoundError:
                return True
            time.sleep(0.2)
        return False

    def interrupt(self, agent_id, *argv):
        """运行 a2a 命令,等 agent 启动并上报 working(命令在等就绪)后发 SIGINT,等同终端里按 Ctrl-C。"""
        (self.state / f"{agent_id}.log.pid").unlink(missing_ok=True)
        # 独立进程组:清理时连同它启动的 herdr 子进程(agent wait)一起结束
        proc = subprocess.Popen([sys.executable, "-m", "a2a.cli", *argv, "--session", SESSION], env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)

        def stop_cli():   # 等 pid 超时或断言失败时,命令可能还在等就绪:结束整个进程组并回收,关闭管道
            if proc.returncode is None:      # 正常流程已在下面 communicate 过
                os.killpg(proc.pid, signal.SIGKILL)   # 尚未回收,pid(= 组号)不会被复用
                proc.communicate(timeout=30)
        self.addCleanup(stop_cli)
        pid = self.started_pid(agent_id)
        time.sleep(2.5)
        self.assertIsNone(proc.poll(), "命令应当还在等 agent 就绪")
        proc.send_signal(signal.SIGINT)
        _, err = proc.communicate(timeout=60)
        self.assertNotEqual(proc.returncode, 0, err)
        return pid

    def panes(self):
        return {p["pane_id"] for p in client.pane_list()}

    def test_restore_that_never_becomes_ready_closes_the_original_pane(self):
        mode = self.switchable_launcher("dv")
        _, dv, _ = self.a2a("agent", "spawn", "dv", "uart")
        self.a2a("agent", "stop", "dv", "uart")
        mode.write_text("stuck")
        (self.state / "dv_uart.log.pid").unlink()
        life = Lifecycle(client=client, topology=self.store, registry=self.registry, audit=self.audit,
                         state_dir=self.state, ready_timeout_s=3)
        with self.assertRaisesRegex(LifecycleError, "可投递"):
            life.restore("dv", "uart")
        # 修复前:pi 仍在原 pane 里运行,记录却是 failed,stop 拒绝处理
        self.assertTrue(self.wait_dead(self.started_pid("dv_uart")))
        self.assertNotIn(dv["pane_id"], self.panes())
        self.assertEqual(self.registry.get("dv_uart").lifecycle, "failed")
        mode.write_text("work")
        _, restored, _ = self.a2a("agent", "restore", "dv", "uart")       # pane 已不在:新建
        self.assertNotEqual(restored["pane_id"], dv["pane_id"])

    def test_ctrl_c_during_spawn_leaves_no_unregistered_agent(self):
        self.switchable_launcher("dv").write_text("stuck")
        before = self.panes()
        pid = self.interrupt("dv_uart", "agent", "spawn", "dv", "uart")
        # 修复前:多出一个 pane,pi 仍在运行,注册表里没有它
        self.assertTrue(self.wait_dead(pid))
        self.assertEqual(self.panes(), before)
        self.assertEqual(self.registry.list(), [])

    def test_ctrl_c_during_restore_closes_the_pane_and_marks_failed(self):
        mode = self.switchable_launcher("dv")
        _, dv, _ = self.a2a("agent", "spawn", "dv", "uart")
        self.a2a("agent", "stop", "dv", "uart")
        mode.write_text("stuck")
        pid = self.interrupt("dv_uart", "agent", "restore", "dv", "uart")
        # 修复前:pi 已在原 pane 运行,记录却仍是 stopped
        self.assertTrue(self.wait_dead(pid))
        self.assertNotIn(dv["pane_id"], self.panes())
        self.assertEqual(self.registry.get("dv_uart").lifecycle, "failed")

    def reap_at_end(self, pgid):
        """测试结束时(无论成败)结束进程组 pgid 里现在的进程。按 pid + 启动时间 + argv0 核对身份后再发信号:
        结束时该组可能早已清空,pgid 或 pid 被别的进程复用。"""
        sig = ProcSignaler()
        members = {pid: sig.snapshot(pid) for pid in sig.group_members(pgid)}

        def reap():
            for pid, snap in members.items():
                if snap is not None and sig.snapshot(pid) == snap:
                    self.kill_if_alive(pid)
        self.addCleanup(reap)

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


PLUGIN = HERE.parent / "pi-extension" / "a2a.ts"
INSPECT = HERE / "pi-ext" / "a2a-inspect.ts"
STAGE6_EDGES = [
    {"id": "dv_done", "from": "dv", "to": "sw",
     "template": "{ip} ip 我已经完成了uvm验证，你需要对这个{ip} ip进行 驱动程序开发和HAL框架开发。"
                 "不要真的去做，直接用 a2a_send 回复测试成功或测试失败（二选一）"},
    {"id": "sw_test_pass", "from": "sw", "to": "dv",
     "template": "我已经完成了{ip} ip的驱动程序开发，测试成功。你只回复收到即可，不要真的去做"},
    {"id": "sw_test_fail", "from": "sw", "to": "dv",
     "template": "我已经完成了{ip} ip的驱动程序开发，测试失败。你只回复收到即可，不要真的去做"},
]


@unittest.skipUnless(ENABLED and FNX_ENABLED and HAVE_TOOLS and all(os.path.exists(p) for p in FNX.values()),
                     "设置 A2A_INTEGRATION=1 与 A2A_INTEGRATION_FNX=1,并安装 fnx_dv / fnx_sw")
class RealFnxPlugin(_Base):
    """阶段 6:真实 fnx 加载 a2a 插件。**不发任何提示词,不调用模型。**

    启动参数 `--no-extensions -e <仓库里的插件> -e <检查扩展>`:不加载 fnx 里已安装的任何扩展(包括在线安装的
    a2a 包),只加载本仓库的开发版,也不往 fnx 的 agent 目录写任何东西(22 号方案实现时改;此前用
    `a2a plugin install` 软链接,但 VM 上已在线安装插件后软链接安装会被拒绝)。
    """

    def make_launchers(self):
        return dict(FNX)

    def setUp(self):
        super().setUp()

        def stage6(data):
            data["edges"] = STAGE6_EDGES
            for role in ("dv", "sw"):
                data["roles"][role]["launch_args"] = ["--no-builtin-tools", *self.extension_args()]
        self.store.update(stage6)
        (self.state / "inspect.enabled").touch()
        self.work = self.state / "work"  # agent 的工作目录:测试结束时必须仍为空
        self.work.mkdir()
        for role in ("dv", "sw"):
            # fnx 按工作目录在 agent/sessions/ 下建会话目录;没有发提示词,应当是空的
            sessions = Path(FNX[role]).parents[1] / "agent" / "sessions"
            self.addCleanup(self._rmdir_if_empty, sessions / ("-" + str(self.work).replace("/", "-") + "--"))
        # 清理按注册的逆序执行:先停掉 agent,再移除扩展文件和空目录
        self.addCleanup(self.purge_all)

    def extension_args(self):
        return ["--no-extensions", "-e", str(PLUGIN), "-e", str(INSPECT)]

    def a2a_plain(self, *argv):
        """不带 --session 的 a2a 命令(plugin 不需要会话)。"""
        proc = subprocess.run([sys.executable, "-m", "a2a.cli", *argv], env=self.env, capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.returncode, json.loads(proc.stdout), proc.stderr

    @staticmethod
    def _rmdir_if_empty(path):
        try:
            path.rmdir()
        except OSError:
            pass

    def inspect(self, agent_id, timeout=30):
        path = self.state / f"inspect-{agent_id}.json"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not path.exists():
            time.sleep(0.3)
        self.assertTrue(path.exists(), f"{agent_id} 的 fnx 没有写出检查结果(插件没加载?)")
        return json.loads(path.read_text())

    def test_fnx_loads_the_plugin_and_only_a2a_send_is_active(self):
        expected = {"dv": {"dv_done": STAGE6_EDGES[0]}, "sw": {"sw_test_pass": STAGE6_EDGES[1],
                                                                "sw_test_fail": STAGE6_EDGES[2]}}
        for role in ("dv", "sw"):
            self.a2a("agent", "spawn", role, "uart", "--cwd", str(self.work))
            seen = self.inspect(f"{role}_uart")
            self.assertEqual(seen["active"], ["a2a_send"])  # --no-builtin-tools:没有 read/write/edit/bash
            self.assertEqual(seen["cwd"], str(self.work))
            send = seen["a2a_send"]
            self.assertEqual(send["parameters"]["properties"]["edge_id"]["enum"], list(expected[role]))
            for edge_id, edge in expected[role].items():
                text = edge["template"].format(ip="uart")
                self.assertIn(f"{edge_id}:发给 {edge['to']}_uart,内容是「{text}」", send["description"])
            environ = self.environ_of_agent(self.registry.get(f"{role}_uart").pane_id)
            self.assertEqual(environ["A2A_STATE_DIR"], str(self.state))
            # 插件向 herdr 上报状态用的程序:绝对路径、可执行(阶段 8)
            herdr_bin = environ["A2A_HERDR_BIN"]
            self.assertTrue(os.path.isabs(herdr_bin) and os.access(herdr_bin, os.X_OK), herdr_bin)
        self.assertEqual(sorted(p.name for p in self.work.iterdir()), [])
        self.assertEqual(self.registry.get("sw_uart").lifecycle, "running")



@unittest.skipUnless(ENABLED and FNX_ENABLED and HAVE_TOOLS and all(os.path.exists(p) for p in FNX.values()),
                     "设置 A2A_INTEGRATION=1 与 A2A_INTEGRATION_FNX=1,并安装 fnx_dv / fnx_sw")
class RealFnxCommand(RealFnxPlugin):
    """22 号方案:在真实 fnx 的输入框里输入 `/a2a <边>` 回车。**不调用模型**:命令由插件处理,不进入模型;
    也不启动 broker(投递会把消息打进对方 pane、触发对方的模型)。"""

    test_fnx_loads_the_plugin_and_only_a2a_send_is_active = None   # 不继承父类的用例(这里没加载检查扩展)

    def extension_args(self):
        return ["--no-extensions", "-e", str(PLUGIN)]

    def screen(self, pane_id):
        return client.pane_read(pane_id, source="recent-unwrapped", lines=80)

    def wait_screen(self, pane_id, needle, timeout=20):
        deadline = time.monotonic() + timeout
        text = ""
        while time.monotonic() < deadline:
            text = self.screen(pane_id)
            if needle in text:
                return text
            time.sleep(0.3)
        self.fail(f"{timeout} 秒内屏幕上没有出现 {needle!r}。屏幕:\n{text}")

    def status(self, agent_id):
        return client.agent_get(agent_id)["agent_status"]

    def spawn_pair(self):
        panes = {}
        for role in ("dv", "sw"):
            _, info, _ = self.a2a("agent", "spawn", role, "uart", "--cwd", str(self.work))
            panes[role] = info["pane_id"]
        self.wait_screen(panes["dv"], "a2a.ts")             # 插件已加载(启动画面 [Extensions])
        return panes

    def sessions_have_no_model_calls(self):
        """fnx 的会话文件里不能有 assistant 消息(没有调用模型)。"""
        sessions = Path(FNX["dv"]).parents[1] / "agent" / "sessions" / ("-" + str(self.work).replace("/", "-") + "--")
        for f in sessions.glob("*.jsonl") if sessions.is_dir() else []:
            self.assertNotIn('"role":"assistant"', f.read_text().replace(" ", ""), f)

    def test_slash_command_sends_without_the_model(self):
        panes = self.spawn_pair()
        client.pane_run(panes["dv"], "/a2a")
        lines = self.wait_screen(panes["dv"], "uart ip 我已经完成了uvm验证").splitlines()
        head = next(i for i, l in enumerate(lines) if "dv_done → sw_uart" in l)
        body = next(i for i, l in enumerate(lines) if "uart ip 我已经完成了uvm验证" in l)
        self.assertEqual(lines[head].strip(), "dv_done → sw_uart")          # 边名单独一行
        self.assertEqual(body, head + 1)                                    # 消息紧跟在下一行
        indent = lambda l: len(l) - len(l.lstrip())
        self.assertGreater(indent(lines[body]), indent(lines[head]))        # 且缩进更深
        client.pane_run(panes["dv"], "/a2a dv_done")
        text = self.wait_screen(panes["dv"], "已发送给 sw_uart")
        spool = Spool(self.state / "spool")
        (message,) = spool.pending("sw_uart")
        self.assertEqual((message.src, message.edge_id), ("dv_uart", "dv_done"))
        self.assertIn(message.msg_id, text)
        client.pane_run(panes["dv"], "/a2a nope")
        self.wait_screen(panes["dv"], "没有这条边:nope")
        self.assertEqual(len(spool.pending("sw_uart")), 1)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self.status("dv_uart") not in ("idle", "done"):
            time.sleep(0.3)
        self.assertIn(self.status("dv_uart"), ("idle", "done"))
        self.sessions_have_no_model_calls()

    def test_autocomplete_shows_the_version_instead_of_the_source(self):
        # 只打字、不回车:不提交、不调用模型。-e 加载的扩展 pi 标为 [t],改写后应为 [v<版本>]
        version = json.loads((HERE.parents[2] / "package.json").read_text())["version"]
        panes = self.spawn_pair()
        client.pane_send_text(panes["dv"], "/a2")
        try:
            text = self.wait_screen(panes["dv"], f"[v{version}]")
            line = next(l for l in text.splitlines() if f"[v{version}]" in l)
            self.assertIn("a2a", line)
            self.assertNotIn("[t]", line)
        finally:
            client.pane_send_keys(panes["dv"], "backspace", "backspace", "backspace")
        self.sessions_have_no_model_calls()

    def test_slow_command_shows_working_until_it_finishes(self):
        # 占住 IP 的排空锁(独占,不写排空标记):Router 入队时等共享锁,命令就会卡住几秒
        from a2a import ipdrain
        import threading
        panes = self.spawn_pair()
        self.assertIn(self.status("dv_uart"), ("idle", "done"))
        held = threading.Event()
        release = threading.Event()

        def hold():
            with ipdrain.drain_exclusive(self.state, "uart"):
                held.set()
                release.wait(30)
        t = threading.Thread(target=hold)
        t.start()
        try:
            self.assertTrue(held.wait(10))
            client.pane_run(panes["dv"], "/a2a dv_done")
            deadline = time.monotonic() + 10
            seen = []
            while time.monotonic() < deadline:
                seen.append(self.status("dv_uart"))
                if seen[-1] == "working":
                    break
                time.sleep(0.2)
            self.assertEqual(seen[-1], "working", f"命令在途时应为 working,看到 {seen}")
            time.sleep(2)
            self.assertEqual(self.status("dv_uart"), "working")   # 仍在等锁
            self.assertEqual(Spool(self.state / "spool").pending("sw_uart"), [])
        finally:
            release.set()
            t.join(10)
        self.wait_screen(panes["dv"], "已发送给 sw_uart")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self.status("dv_uart") not in ("idle", "done"):
            time.sleep(0.2)
        self.assertIn(self.status("dv_uart"), ("idle", "done"))   # herdr 把未查看的结束显示为 done,都可投递
        self.sessions_have_no_model_calls()


if __name__ == "__main__":
    unittest.main()
