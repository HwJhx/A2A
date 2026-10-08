"""HerdrClient 单元测试。使用假 runner,不需要运行 herdr。"""
from __future__ import annotations

import json
import subprocess
import unittest
from typing import Any, List, Optional, Sequence

from a2a import (
    HerdrAgentBlocked,
    HerdrAgentNameTaken,
    HerdrAgentNotReady,
    HerdrBinaryNotFound,
    HerdrClient,
    HerdrError,
    HerdrInvalidAgentName,
    HerdrNotFound,
    HerdrPromptStalled,
    HerdrServerNotRunning,
    HerdrTimeout,
    HerdrUsageError,
    READY_STATUSES,
    session_delete,
    session_list,
    session_stop,
)


def proc(stdout: str = "", stderr: str = "", rc: int = 0) -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def ok(result: Any = None) -> "subprocess.CompletedProcess[str]":
    return proc(json.dumps({"id": "cli:x", "result": result if result is not None else {}}))


def err(code: str, message: str = "boom", rc: int = 1, *, on_stderr: bool = False) -> "subprocess.CompletedProcess[str]":
    body = json.dumps({"id": "cli:x", "error": {"code": code, "message": message}})
    return proc(stderr=body, rc=rc) if on_stderr else proc(stdout=body, rc=rc)


class FakeRunner:
    """按顺序返回预设的结果,并记录每次调用的参数。"""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: List[List[str]] = []
        self.timeouts: List[Optional[float]] = []

    def __call__(self, argv: Sequence[str], timeout: Optional[float]):
        self.calls.append(list(argv))
        self.timeouts.append(timeout)
        if not self.responses:
            return ok({})
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def last(self) -> List[str]:
        return self.calls[-1]


def client(*responses: Any, session: Optional[str] = "t") -> "tuple[HerdrClient, FakeRunner]":
    r = FakeRunner(*responses)
    return HerdrClient(session, runner=r), r


class TestArgv(unittest.TestCase):
    def test_session_flag_comes_first(self):
        c, r = client(ok({"workspaces": []}))
        c.workspace_list()
        self.assertEqual(r.last, ["herdr", "--session", "t", "workspace", "list"])

    def test_no_session_means_no_flag(self):
        c, r = client(ok({"workspaces": []}), session=None)
        c.workspace_list()
        self.assertEqual(r.last, ["herdr", "workspace", "list"])

    def test_custom_binary(self):
        r = FakeRunner(ok({"workspaces": []}))
        HerdrClient("t", herdr_bin="/opt/herdr", runner=r).workspace_list()
        self.assertEqual(r.last[0], "/opt/herdr")


class TestCreate(unittest.TestCase):
    TAB_RESULT = {
        "root_pane": {"pane_id": "w1:p2", "tab_id": "w1:t2", "workspace_id": "w1"},
        "tab": {"tab_id": "w1:t2", "workspace_id": "w1", "label": "dv"},
        "type": "tab_created",
    }

    def test_tab_create_argv_and_ids(self):
        c, r = client(ok(self.TAB_RESULT))
        created = c.tab_create(label="dv", cwd="/x", env={"A2A_ROLE": "dv", "A2A_IP": "uart"})
        self.assertEqual(
            r.last,
            ["herdr", "--session", "t", "tab", "create", "--cwd", "/x", "--label", "dv",
             "--env", "A2A_ROLE=dv", "--env", "A2A_IP=uart", "--no-focus"],
        )
        self.assertEqual((created.workspace_id, created.tab_id, created.pane_id), ("w1", "w1:t2", "w1:p2"))

    def test_focus_default_is_no_focus_and_can_be_enabled(self):
        c, r = client(ok(self.TAB_RESULT), ok(self.TAB_RESULT))
        c.tab_create()
        self.assertIn("--no-focus", r.last)
        c.tab_create(focus=True)
        self.assertIn("--focus", r.last)

    def test_tab_create_with_workspace(self):
        c, r = client(ok(self.TAB_RESULT))
        c.tab_create(workspace_id="w1")
        self.assertEqual(r.last[3:7], ["tab", "create", "--workspace", "w1"])

    def test_workspace_create_ids(self):
        result = {
            "workspace": {"workspace_id": "w2"},
            "tab": {"tab_id": "w2:t1", "workspace_id": "w2"},
            "root_pane": {"pane_id": "w2:p1", "tab_id": "w2:t1", "workspace_id": "w2"},
        }
        c, _ = client(ok(result))
        created = c.workspace_create(label="soc")
        self.assertEqual((created.workspace_id, created.tab_id, created.pane_id), ("w2", "w2:t1", "w2:p1"))

    def test_pane_split_with_id(self):
        c, r = client(ok({"pane": {"pane_id": "w1:p4", "tab_id": "w1:t2", "workspace_id": "w1"}}))
        created = c.pane_split("w1:p2", direction="down", cwd="/x", ratio=0.5, env={"A2A_IP": "gpio"})
        self.assertEqual(
            r.last[3:],
            ["pane", "split", "w1:p2", "--direction", "down", "--ratio", "0.5", "--cwd", "/x",
             "--env", "A2A_IP=gpio", "--no-focus"],
        )
        self.assertEqual(created.pane_id, "w1:p4")

    def test_pane_split_current_when_no_id(self):
        c, r = client(ok({"pane": {"pane_id": "w1:p5"}}))
        c.pane_split()
        self.assertEqual(r.last[3:7], ["pane", "split", "--current", "--direction"])

    def test_bad_direction(self):
        c, _ = client()
        with self.assertRaises(ValueError):
            c.pane_split("w1:p1", direction="left")

    def test_bad_env_key(self):
        c, _ = client()
        for bad in ("A-B", "1X", "", "a b", "A=B"):
            with self.assertRaises(ValueError, msg=bad):
                c.tab_create(env={bad: "1"})

    def test_env_value_may_contain_equals(self):
        c, r = client(ok(self.TAB_RESULT))
        c.tab_create(env={"K": "a=b"})
        self.assertIn("K=a=b", r.last)


class TestTargetsValidation(unittest.TestCase):
    def test_empty_and_dash_targets_rejected(self):
        c, r = client()
        for call in (
            lambda: c.pane_get(""),
            lambda: c.pane_close("--current"),
            lambda: c.agent_get("-x"),
            lambda: c.agent_prompt("-x", "hi"),
            lambda: c.tab_close(""),
        ):
            with self.assertRaises(ValueError):
                call()
        self.assertEqual(r.calls, [])  # 校验失败时不应该执行任何命令


class TestPane(unittest.TestCase):
    def test_pane_run_and_send(self):
        c, r = client(ok(), ok(), ok())
        c.pane_run("w1:p2", "echo hi")
        self.assertEqual(r.last[3:], ["pane", "run", "w1:p2", "echo hi"])
        c.pane_send_text("w1:p2", "abc")
        self.assertEqual(r.last[3:], ["pane", "send-text", "w1:p2", "abc"])
        c.pane_send_keys("w1:p2", "ctrl+c", "enter")
        self.assertEqual(r.last[3:], ["pane", "send-keys", "w1:p2", "ctrl+c", "enter"])

    def test_send_keys_requires_keys(self):
        c, _ = client()
        with self.assertRaises(ValueError):
            c.pane_send_keys("w1:p2")
        with self.assertRaises(ValueError):
            c.agent_send_keys("dv_uart")

    def test_pane_read_returns_raw_text_even_if_it_looks_like_json(self):
        screen = '{"id":"cli:fake","error":{"code":"x"}}\n$ '
        c, r = client(proc(stdout=screen))
        text = c.pane_read("w1:p2", lines=20)
        self.assertEqual(text, screen)
        self.assertEqual(r.last[3:], ["pane", "read", "w1:p2", "--source", "recent-unwrapped",
                                      "--format", "text", "--lines", "20"])

    def test_pane_read_validates(self):
        c, _ = client()
        with self.assertRaises(ValueError):
            c.pane_read("w1:p2", source="detection")  # pane read 不支持 detection
        with self.assertRaises(ValueError):
            c.pane_read("w1:p2", fmt="html")

    def test_pane_read_failure_raises(self):
        c, _ = client(err("pane_not_found", "no such pane"))
        with self.assertRaises(HerdrNotFound):
            c.pane_read("w9:p9")

    def test_process_info_uses_dash_dash_pane(self):
        info = {"process_info": {"pane_id": "w1:p2", "foreground_processes": [{"name": "pi"}]}}
        c, r = client(ok(info))
        out = c.pane_process_info("w1:p2")
        self.assertEqual(r.last[3:], ["pane", "process-info", "--pane", "w1:p2"])
        self.assertEqual(out["foreground_processes"][0]["name"], "pi")

    def test_pane_rename_and_clear(self):
        c, r = client(ok(), ok())
        c.pane_rename("w1:p2", "dv-uart")
        self.assertEqual(r.last[3:], ["pane", "rename", "w1:p2", "dv-uart"])
        c.pane_rename("w1:p2", None)
        self.assertEqual(r.last[3:], ["pane", "rename", "w1:p2", "--clear"])

    def test_wait_output_requires_exactly_one_matcher(self):
        c, r = client(ok(), ok())
        with self.assertRaises(ValueError):
            c.pane_wait_output("w1:p2")
        with self.assertRaises(ValueError):
            c.pane_wait_output("w1:p2", match="a", regex="b")
        c.pane_wait_output("w1:p2", regex="ready", timeout_ms=5000, source="recent", lines=50)
        self.assertEqual(r.last[3:], ["pane", "wait-output", "w1:p2", "--regex", "ready",
                                      "--source", "recent", "--lines", "50", "--timeout", "5000"])

    def test_list_methods(self):
        c, r = client(ok({"panes": [{"pane_id": "w1:p1"}]}), ok({"tabs": [{"tab_id": "w1:t1"}]}))
        self.assertEqual(c.pane_list("w1")[0]["pane_id"], "w1:p1")
        self.assertEqual(r.calls[0][3:], ["pane", "list", "--workspace", "w1"])
        self.assertEqual(c.tab_list()[0]["tab_id"], "w1:t1")


class TestAgent(unittest.TestCase):
    AGENT = {"agent": {"agent": "pi", "agent_status": "idle", "pane_id": "w1:p2", "name": "dv_uart"}}

    def test_agent_get_unwraps(self):
        c, r = client(ok(self.AGENT))
        info = c.agent_get("dv_uart")
        self.assertEqual(r.last[3:], ["agent", "get", "dv_uart"])
        self.assertEqual(info["agent_status"], "idle")

    def test_agent_rename_and_clear(self):
        c, r = client(ok(self.AGENT), ok(self.AGENT))
        c.agent_rename("w1:p2", "dv_uart")
        self.assertEqual(r.last[3:], ["agent", "rename", "w1:p2", "dv_uart"])
        c.agent_rename("w1:p2", None)
        self.assertEqual(r.last[3:], ["agent", "rename", "w1:p2", "--clear"])

    def test_prompt_plain(self):
        c, r = client(ok(self.AGENT))
        c.agent_prompt("dv_uart", "你好")
        self.assertEqual(r.last[3:], ["agent", "prompt", "dv_uart", "你好"])

    def test_prompt_wait_requires_timeout(self):
        c, r = client()
        with self.assertRaises(ValueError):
            c.agent_prompt("dv_uart", "hi", wait=True)
        self.assertEqual(r.calls, [])

    def test_prompt_wait_with_timeout_extends_client_timeout(self):
        c, r = client(ok(self.AGENT))
        c.agent_prompt("dv_uart", "hi", wait=True, timeout_ms=120000)
        self.assertEqual(r.last[3:], ["agent", "prompt", "dv_uart", "hi", "--wait", "--timeout", "120000"])
        self.assertGreater(r.timeouts[-1], 120.0)  # 本进程的等待必须比 herdr 的超时更长

    def test_prompt_empty_text_rejected(self):
        c, _ = client()
        with self.assertRaises(ValueError):
            c.agent_prompt("dv_uart", "")

    def test_wait_repeats_until_flag(self):
        c, r = client(ok(self.AGENT))
        c.agent_wait("dv_uart", until=READY_STATUSES, timeout_ms=8000)
        self.assertEqual(r.last[3:], ["agent", "wait", "dv_uart", "--until", "idle", "--until", "done",
                                      "--timeout", "8000"])

    def test_wait_rejects_unknown_status(self):
        c, _ = client()
        with self.assertRaises(ValueError):
            c.agent_wait("dv_uart", until=["finished"])

    def test_wait_timeout_maps_to_HerdrTimeout(self):
        c, _ = client(err("timeout", "timed out waiting for agent status"))
        with self.assertRaises(HerdrTimeout):
            c.agent_wait("dv_uart", until=["idle"], timeout_ms=1000)

    def test_ready_statuses_include_done(self):
        # 实测:只等 idle 时,done 状态会一直等到超时
        self.assertIn("done", READY_STATUSES)
        self.assertIn("idle", READY_STATUSES)

    def test_agent_read(self):
        c, r = client(proc(stdout="屏幕内容"))
        self.assertEqual(c.agent_read("dv_uart", source="detection", lines=5), "屏幕内容")
        self.assertEqual(r.last[3:], ["agent", "read", "dv_uart", "--source", "detection",
                                      "--format", "text", "--lines", "5"])

    def test_agent_list(self):
        c, _ = client(ok({"agents": [{"pane_id": "w1:p2"}, {"pane_id": "w1:p3"}]}))
        self.assertEqual([a["pane_id"] for a in c.agent_list()], ["w1:p2", "w1:p3"])

    def test_find_agent(self):
        a = {"agents": [{"pane_id": "w1:p2", "agent": "pi"}]}
        c, _ = client(ok(a), ok(a))
        self.assertEqual(c.find_agent("w1:p2")["agent"], "pi")
        self.assertIsNone(c.find_agent("w1:p9"))

    def test_focus_and_explain(self):
        c, r = client(ok(self.AGENT), proc(stdout="agent: pi\nstate: idle\n"))
        c.agent_focus("dv_uart")
        self.assertEqual(r.last[3:], ["agent", "focus", "dv_uart"])
        self.assertIn("state: idle", c.agent_explain("dv_uart", verbose=True))
        self.assertEqual(r.last[3:], ["agent", "explain", "dv_uart", "--verbose"])


class TestWaitForDetected(unittest.TestCase):
    def test_polls_until_found(self):
        empty = ok({"agents": []})
        found = ok({"agents": [{"pane_id": "w1:p2", "agent": "pi"}]})
        r = FakeRunner(empty, empty, found)
        sleeps: List[float] = []
        c = HerdrClient("t", runner=r, sleep=sleeps.append, monotonic=lambda: 0.0)
        self.assertEqual(c.wait_for_agent_detected("w1:p2", timeout_s=5, interval_s=0.5)["agent"], "pi")
        self.assertEqual(sleeps, [0.5, 0.5])

    def test_times_out(self):
        class Clock:
            t = 0.0

            def __call__(self):
                return self.t

            def sleep(self, s):
                self.t += s

        clock = Clock()
        r = FakeRunner(*[ok({"agents": []}) for _ in range(50)])
        c = HerdrClient("t", runner=r, sleep=clock.sleep, monotonic=clock)
        with self.assertRaises(HerdrTimeout) as ctx:
            c.wait_for_agent_detected("w1:p2", timeout_s=2, interval_s=0.5)
        self.assertEqual(ctx.exception.code, "agent_not_detected")


class TestErrors(unittest.TestCase):
    def assert_maps(self, code: str, cls):
        c, _ = client(err(code, "msg"))
        with self.assertRaises(cls) as ctx:
            c.agent_get("dv_uart")
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.message, "msg")

    def test_code_mapping(self):
        self.assert_maps("server_not_running", HerdrServerNotRunning)
        self.assert_maps("agent_not_ready", HerdrAgentNotReady)
        self.assert_maps("agent_blocked", HerdrAgentBlocked)
        self.assert_maps("agent_name_taken", HerdrAgentNameTaken)
        self.assert_maps("invalid_agent_name", HerdrInvalidAgentName)
        self.assert_maps("agent_prompt_stalled", HerdrPromptStalled)
        self.assert_maps("timeout", HerdrTimeout)
        self.assert_maps("agent_not_found", HerdrNotFound)
        self.assert_maps("pane_not_found", HerdrNotFound)
        self.assert_maps("tab_not_found", HerdrNotFound)

    def test_unknown_code_is_plain_HerdrError(self):
        c, _ = client(err("something_new", "x"))
        with self.assertRaises(HerdrError) as ctx:
            c.agent_get("a")
        self.assertIs(type(ctx.exception), HerdrError)
        self.assertEqual(ctx.exception.code, "something_new")

    def test_all_specific_errors_are_HerdrError_subclasses(self):
        for cls in (HerdrServerNotRunning, HerdrNotFound, HerdrAgentNotReady, HerdrAgentBlocked,
                    HerdrAgentNameTaken, HerdrInvalidAgentName, HerdrPromptStalled, HerdrTimeout,
                    HerdrUsageError, HerdrBinaryNotFound):
            self.assertTrue(issubclass(cls, HerdrError))

    def test_error_json_on_stderr(self):
        c, _ = client(err("agent_not_found", "gone", on_stderr=True))
        with self.assertRaises(HerdrNotFound):
            c.agent_get("a")

    def test_error_with_exit_code_zero_still_raises(self):
        c, _ = client(err("agent_not_found", "gone", rc=0))
        with self.assertRaises(HerdrNotFound):
            c.agent_get("a")

    def test_exit_code_2_is_usage_error(self):
        c, _ = client(proc(stderr="usage: herdr agent list", rc=2))
        with self.assertRaises(HerdrUsageError) as ctx:
            c.agent_list()
        self.assertIn("usage", str(ctx.exception))

    def test_non_json_failure(self):
        c, _ = client(proc(stderr="something broke", rc=1))
        with self.assertRaises(HerdrError) as ctx:
            c.agent_list()
        self.assertEqual(ctx.exception.returncode, 1)
        self.assertIn("something broke", str(ctx.exception))

    def test_garbage_stdout_with_exit_zero_raises(self):
        c, _ = client(proc(stdout="not json at all"))
        with self.assertRaises(HerdrError):
            c.agent_list()

    def test_empty_stdout_success_returns_empty(self):
        # report-agent / pane run 这类命令成功时没有输出
        c, _ = client(proc(stdout=""))
        self.assertEqual(c.pane_run("w1:p2", "echo 1"), {})

    def test_subprocess_timeout(self):
        c, _ = client(subprocess.TimeoutExpired(cmd="herdr", timeout=30))
        with self.assertRaises(HerdrTimeout) as ctx:
            c.agent_list()
        self.assertEqual(ctx.exception.code, "client_timeout")

    def test_binary_missing(self):
        c, _ = client(FileNotFoundError("herdr"))
        with self.assertRaises(HerdrBinaryNotFound):
            c.agent_list()

    def test_exception_carries_argv(self):
        c, _ = client(err("agent_not_found"))
        with self.assertRaises(HerdrNotFound) as ctx:
            c.agent_get("dv_uart")
        self.assertEqual(ctx.exception.argv, ["herdr", "--session", "t", "agent", "get", "dv_uart"])

    def test_is_server_running(self):
        c, _ = client(ok({"workspaces": []}))
        self.assertTrue(c.is_server_running())
        c, _ = client(err("server_not_running"))
        self.assertFalse(c.is_server_running())
        # 其他错误不应被吞掉
        c, _ = client(err("something_new"))
        with self.assertRaises(HerdrError):
            c.is_server_running()


class TestSessionHelpers(unittest.TestCase):
    TABLE = (
        "name                 status   directory                                        socket\n"
        "default              stopped  /home/jhx/.config/herdr                          /home/jhx/.config/herdr/herdr.sock\n"
        "walk1                running  /home/jhx/.config/herdr/sessions/walk1           /home/jhx/.config/herdr/sessions/walk1/herdr.sock\n"
    )

    def test_session_list_parses_table(self):
        r = FakeRunner(proc(stdout=self.TABLE))
        rows = session_list(runner=r)
        self.assertEqual(r.last, ["herdr", "session", "list"])
        self.assertEqual([(x["name"], x["status"]) for x in rows], [("default", "stopped"), ("walk1", "running")])
        self.assertTrue(rows[1]["socket"].endswith("herdr.sock"))

    def test_stop_and_delete(self):
        r = FakeRunner(proc(stdout="stopped session walk1\n"), proc(stdout="deleted session walk1\n"))
        session_stop("walk1", runner=r)
        self.assertEqual(r.last, ["herdr", "session", "stop", "walk1"])
        session_delete("walk1", runner=r)
        self.assertEqual(r.last, ["herdr", "session", "delete", "walk1"])

    def test_delete_default_is_refused_locally(self):
        r = FakeRunner()
        with self.assertRaises(ValueError):
            session_delete("default", runner=r)
        self.assertEqual(r.calls, [])

    def test_session_error_json(self):
        r = FakeRunner(err("session_delete_failed", "deleting the default session is not supported"))
        with self.assertRaises(HerdrError) as ctx:
            session_stop("x", runner=r)
        self.assertEqual(ctx.exception.code, "session_delete_failed")


if __name__ == "__main__":
    unittest.main()
