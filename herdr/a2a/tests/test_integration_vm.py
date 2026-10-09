"""集成测试:在装了 herdr 的机器(本项目是 OrbStack 虚拟机)上对真实 herdr 运行。

默认不运行。运行方式(在虚拟机里):

    cd herdr/a2a
    PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 A2A_INTEGRATION=1 python3 -m unittest discover -s tests -v

  A2A_INTEGRATION=1       运行本文件(只用普通 shell 和 sleep,不调用模型)
  A2A_INTEGRATION_FNX=1   额外运行真实 fnx_dv / fnx_sw 的测试(启动 TUI,但不发给模型任何提示词)

测试会自己起一个一次性的命名会话(名字 a2at<进程号>),并在结束时停止、删除它,
同时关闭为它托管界面的 tmux 窗口。不会碰默认会话和其他命名会话。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
import unittest
from typing import Optional

from a2a import (
    HerdrAgentNameTaken,
    HerdrClient,
    HerdrInvalidAgentName,
    HerdrNotFound,
    HerdrServerNotRunning,
    HerdrTimeout,
    READY_STATUSES,
    build_launch_command,
    identity_env,
    preflight_launcher,
    session_delete,
    session_list,
    session_stop,
)

ENABLED = os.environ.get("A2A_INTEGRATION") == "1"
FNX_ENABLED = os.environ.get("A2A_INTEGRATION_FNX") == "1"
HAVE_TOOLS = bool(shutil.which("herdr") and shutil.which("tmux"))

SESSION = "a2at%d" % os.getpid()
TMUX = SESSION + "_tty"
CWD = os.path.expanduser("~")

client: Optional[HerdrClient] = None


def setUpModule() -> None:  # noqa: N802
    global client
    if not (ENABLED and HAVE_TOOLS):
        return
    # 用 tmux 托管 herdr 界面:命名会话必须通过 attach 才会启动,而 attach 需要一个终端
    subprocess.run(
        ["tmux", "new-session", "-d", "-x", "200", "-y", "50", "-s", TMUX,
         "cd %s && herdr session attach %s" % (CWD, SESSION)],
        check=True,
    )
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
        except Exception as exc:  # 清理失败只提示,不掩盖测试结果
            print("清理 %s(%s) 失败: %s" % (fn.__name__, SESSION, exc))
    subprocess.run(["tmux", "kill-session", "-t", TMUX], capture_output=True)


def wait_for_text(pane_id: str, needle: str, timeout: float = 15.0) -> str:
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        text = client.pane_read(pane_id, source="recent-unwrapped", lines=200)
        if needle in text:
            return text
        time.sleep(0.4)
    raise AssertionError("在 pane %s 里 %s 秒内没有出现 %r;当前屏幕:\n%s" % (pane_id, timeout, needle, text[-600:]))


@unittest.skipUnless(ENABLED and HAVE_TOOLS, "设置 A2A_INTEGRATION=1,并要求有 herdr 和 tmux")
class IntegrationBase(unittest.TestCase):
    def new_tab(self, label: str, **kwargs):
        created = client.tab_create(label=label, cwd=CWD, **kwargs)
        self.addCleanup(self._close_tab, created.tab_id)
        return created

    @staticmethod
    def _close_tab(tab_id: Optional[str]) -> None:
        try:
            client.tab_close(tab_id)
        except Exception:
            pass


class TestSessionAndLayout(IntegrationBase):
    def test_session_is_listed_and_running(self):
        rows = {r["name"]: r for r in session_list()}
        self.assertIn(SESSION, rows)
        self.assertEqual(rows[SESSION]["status"], "running")

    def test_missing_session_raises_server_not_running(self):
        ghost = HerdrClient("a2a-no-such-session-%d" % os.getpid())
        self.assertFalse(ghost.is_server_running())
        with self.assertRaises(HerdrServerNotRunning):
            ghost.workspace_list()

    def test_tab_create_returns_ids_and_label(self):
        created = self.new_tab("t_label")
        # 实测:ID 的编号部分不一定是数字(例如 w1:tC),不要假设格式
        self.assertRegex(created.tab_id, r"^w[0-9A-Za-z]+:t[0-9A-Za-z]+$")
        self.assertRegex(created.pane_id, r"^w[0-9A-Za-z]+:p[0-9A-Za-z]+$")
        labels = {t["tab_id"]: t.get("label") for t in client.tab_list()}
        self.assertEqual(labels[created.tab_id], "t_label")
        client.tab_rename(created.tab_id, "t_renamed")
        self.assertEqual({t["tab_id"]: t.get("label") for t in client.tab_list()}[created.tab_id], "t_renamed")

    def test_env_injection_reaches_the_shell(self):
        created = self.new_tab("t_env", env=identity_env("soc_a", "dv", "uart"))
        client.pane_run(created.pane_id, 'echo "ENVCHK $A2A_PROJECT_ID $A2A_ROLE $A2A_IP $HERDR_PANE_ID $HERDR_ENV"')
        text = wait_for_text(created.pane_id, "ENVCHK soc_a dv uart %s 1" % created.pane_id)
        self.assertIn("ENVCHK soc_a dv uart", text)

    def test_split_rename_process_info_close(self):
        created = self.new_tab("t_split")
        split = client.pane_split(created.pane_id, direction="down", cwd=CWD, env={"A2A_IP": "gpio"})
        self.assertNotEqual(split.pane_id, created.pane_id)
        self.assertEqual(split.tab_id, created.tab_id)
        ids = {p["pane_id"] for p in client.pane_list()}
        self.assertTrue({created.pane_id, split.pane_id} <= ids)

        client.pane_run(split.pane_id, 'echo "SPLITENV $A2A_IP"')
        wait_for_text(split.pane_id, "SPLITENV gpio")

        client.pane_rename(split.pane_id, "gpio-pane")
        client.pane_rename(split.pane_id, None)

        info = client.pane_process_info(split.pane_id)
        names = [p.get("name") for p in info.get("foreground_processes", [])]
        self.assertTrue(names, info)

        client.pane_close(split.pane_id)
        self.assertNotIn(split.pane_id, {p["pane_id"] for p in client.pane_list()})

    def test_unknown_agent_and_pane_raise_not_found(self):
        with self.assertRaises(HerdrNotFound):
            client.agent_get("no_such_agent")
        with self.assertRaises(HerdrNotFound):
            client.pane_read("w99:p99")

    def test_send_keys_interrupts_a_running_command(self):
        created = self.new_tab("t_keys")
        client.pane_run(created.pane_id, "sleep 300; echo AFTERSLEEP")
        time.sleep(1.0)
        client.pane_send_keys(created.pane_id, "ctrl+c")
        client.pane_run(created.pane_id, "echo KEYSOK")
        wait_for_text(created.pane_id, "KEYSOK")

    def test_wait_output_matches_new_text(self):
        created = self.new_tab("t_wait")
        client.pane_run(created.pane_id, "sleep 1; echo WAITMARK_42")
        client.pane_wait_output(created.pane_id, match="WAITMARK_42", timeout_ms=15000)


class TestAgentDetectionWithSleep(IntegrationBase):
    """用 `exec -a pi sleep` 当作假的 pi agent:验证识别、改名、等待,不调用任何模型。"""

    def start_fake_agent(self, label: str) -> str:
        created = self.new_tab(label)
        client.pane_run(created.pane_id, 'bash -c "exec -a pi sleep 600"')
        client.wait_for_agent_detected(created.pane_id, timeout_s=20)
        return created.pane_id

    def test_detected_by_argv0_and_idle(self):
        pane = self.start_fake_agent("t_det")
        info = client.find_agent(pane)
        self.assertEqual(info["agent"], "pi")
        # 实测:刚被识别的那一刻状态可能还是 unknown,"识别到了"不等于"可以投递",
        # 必须再等一次 READY(idle / done)
        ready = client.agent_wait(pane, until=READY_STATUSES, timeout_ms=15000)
        self.assertIn(ready["agent_status"], READY_STATUSES)

    def test_rename_and_address_by_name(self):
        pane = self.start_fake_agent("t_name")
        client.agent_rename(pane, "it_agent_a")
        self.assertEqual(client.agent_get("it_agent_a")["pane_id"], pane)
        self.assertEqual(client.agent_get(pane)["name"], "it_agent_a")
        # 按名字等待可投递状态:应立即返回
        self.assertIn(client.agent_wait("it_agent_a", until=READY_STATUSES, timeout_ms=5000)["agent_status"],
                      READY_STATUSES)
        client.agent_rename("it_agent_a", None)
        with self.assertRaises(HerdrNotFound):
            client.agent_get("it_agent_a")

    def test_name_taken_and_invalid_name(self):
        pane_a = self.start_fake_agent("t_dup_a")
        pane_b = self.start_fake_agent("t_dup_b")
        client.agent_rename(pane_a, "it_dup")
        with self.assertRaises(HerdrAgentNameTaken):
            client.agent_rename(pane_b, "it_dup")
        for bad in ("Upper", "has.dot", "1leading", "x" * 33, ""):
            with self.assertRaises((HerdrInvalidAgentName, ValueError), msg=bad):
                client.agent_rename(pane_b, bad)

    def test_wait_for_unreached_state_times_out(self):
        pane = self.start_fake_agent("t_timeout")
        with self.assertRaises(HerdrTimeout):
            client.agent_wait(pane, until=["working"], timeout_ms=1500)

    def test_name_is_lost_after_agent_exits(self):
        pane = self.start_fake_agent("t_exit")
        client.agent_rename(pane, "it_gone")
        client.pane_send_keys(pane, "ctrl+c")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                client.agent_get("it_gone")
            except HerdrNotFound:
                return
            time.sleep(0.4)
        self.fail("agent 退出后名字仍然存在")

    def test_prompt_is_accepted_by_a_recognised_agent(self):
        pane = self.start_fake_agent("t_prompt")
        info = client.agent_prompt(pane, "这是一句测试,sleep 不会处理它")
        self.assertEqual(info["pane_id"], pane)


@unittest.skipUnless(ENABLED and HAVE_TOOLS and FNX_ENABLED, "设置 A2A_INTEGRATION_FNX=1 才运行真实 fnx 测试")
class TestRealFnxLaunch(IntegrationBase):
    """启动真实的 fnx_dv / fnx_sw(会出现 TUI,但不会向模型发送任何提示词)。"""

    def check_agent(self, name: str, role: str) -> None:
        launcher = os.path.expanduser("~/.forenyx/%s/bin/%s" % (name, name))
        if not os.path.isfile(launcher):
            self.skipTest("没有安装 %s" % name)
        self.assertEqual(preflight_launcher(launcher), [])

        created = self.new_tab("t_" + role, env=identity_env("soc_a", role, "uart"))
        client.pane_run(created.pane_id, build_launch_command(launcher))
        agent = client.wait_for_agent_detected(created.pane_id, timeout_s=60)
        self.assertEqual(agent["agent"], "pi")

        argv0 = [p.get("argv") for p in client.pane_process_info(created.pane_id)["foreground_processes"]]
        self.assertTrue(any(a and a[0] == "pi" for a in argv0), argv0)

        banner = wait_for_text(created.pane_id, name, timeout=30)
        self.assertIn(name, banner)

        # pi 的 `!` 前缀直接执行 shell,不经过模型
        client.pane_send_text(created.pane_id, "! echo FNXENV $A2A_PROJECT_ID $A2A_ROLE $A2A_IP $HERDR_PANE_ID")
        time.sleep(0.6)  # 实测:TUI 需要一点间隔才会处理紧跟着的回车,否则文字留在输入框里不执行
        client.pane_send_keys(created.pane_id, "enter")
        wait_for_text(created.pane_id, "FNXENV soc_a %s uart %s" % (role, created.pane_id), timeout=20)

        client.agent_rename(created.pane_id, "%s_uart" % role)
        self.assertEqual(client.agent_get("%s_uart" % role)["pane_id"], created.pane_id)

        client.pane_send_keys(created.pane_id, "ctrl+d")

    def test_fnx_dv(self):
        self.check_agent("fnx_dv", "dv")

    def test_fnx_sw(self):
        self.check_agent("fnx_sw", "sw")


if __name__ == "__main__":
    unittest.main()
