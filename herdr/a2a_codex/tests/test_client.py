from __future__ import annotations

import json
import io
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, Optional, Sequence
from unittest import mock

from a2a_codex import HerdrClient
from a2a_codex import cli as cli_module
from a2a_codex.errors import (
    HerdrAgentNameTaken,
    HerdrAgentNotReady,
    HerdrInvalidAgentName,
    HerdrPromptOutcomeUnknown,
    from_code,
)
from a2a_codex.launcher import build_launch_command


def response(result: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, json.dumps({"id": "x", "result": result}), "")


class Runner:
    def __init__(self, *items: subprocess.CompletedProcess[str]) -> None:
        self.items = list(items)
        self.calls: list[list[str]] = []
        self.timeouts: list[Optional[float]] = []

    def __call__(self, argv: Sequence[str], timeout: Optional[float]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        self.timeouts.append(timeout)
        return self.items.pop(0) if self.items else response({})


class TestHerdrClient(unittest.TestCase):
    def test_agent_name_errors_are_mapped(self) -> None:
        self.assertIsInstance(from_code("agent_name_taken", "taken"), HerdrAgentNameTaken)
        self.assertIsInstance(from_code("invalid_agent_name", "invalid"), HerdrInvalidAgentName)

    def test_create_tab_maps_to_cli_and_model(self) -> None:
        r = Runner(response({"tab": {"tab_id": "w1:t2", "workspace_id": "w1"},
                             "root_pane": {"pane_id": "w1:p2"}}))
        value = HerdrClient("test1", runner=r).create_tab(label="dv", cwd="/tmp", env={"A2A_IP": "uart"})
        self.assertEqual(value.tab_id, "w1:t2")
        self.assertEqual(r.calls[0], ["herdr", "--session", "test1", "tab", "create", "--label", "dv",
                                      "--cwd", "/tmp", "--env", "A2A_IP=uart", "--no-focus"])

    def test_start_agent_uses_pane_run(self) -> None:
        r = Runner(response({}))
        HerdrClient("test1", runner=r).start_agent("w1:p2", "bash -c 'exec -a pi fnx_dv'")
        self.assertEqual(r.calls[0][3:], ["pane", "run", "w1:p2", "bash -c 'exec -a pi fnx_dv'"])

    def test_read_is_plain_text(self) -> None:
        r = Runner(subprocess.CompletedProcess([], 0, "hello\n", ""))
        self.assertEqual(HerdrClient("test1", runner=r).read_agent("w1:p2", lines=20), "hello\n")

    def test_error_is_mapped(self) -> None:
        body = json.dumps({"error": {"code": "agent_not_ready", "message": "not ready"}})
        r = Runner(subprocess.CompletedProcess([], 1, body, ""))
        with self.assertRaises(HerdrAgentNotReady):
            HerdrClient("test1", runner=r).send_prompt("w1:p2", "hi")

    def test_delete_and_rename(self) -> None:
        r = Runner(response({}), response({}), response({}))
        c = HerdrClient("test1", runner=r)
        c.delete_workspace("w1")
        c.rename_tab("w1:t1", "verification")
        c.delete_pane("w1:p1")
        self.assertEqual(r.calls[0][3:], ["workspace", "close", "w1"])
        self.assertEqual(r.calls[1][3:], ["tab", "rename", "w1:t1", "verification"])
        self.assertEqual(r.calls[2][3:], ["pane", "close", "w1:p1"])

    def test_agent_rename_and_launch_command(self) -> None:
        r = Runner(response({"agents": [{"agent": "pi", "pane_id": "w1:p1"}]}),
                   response({"agent": {"agent": "pi", "pane_id": "w1:p1", "name": "dv_uart"}}))
        c = HerdrClient("test1", runner=r)
        self.assertEqual(c.find_agent("w1:p1").agent, "pi")
        self.assertEqual(c.rename_agent("w1:p1", "dv_uart").raw["name"], "dv_uart")
        self.assertIn("builtin exec -a pi", build_launch_command("/opt/fnx_dv"))

    def test_agent_rename_can_clear_name(self) -> None:
        r = Runner(response({"agent": {"pane_id": "w1:p1"}}))
        HerdrClient("test1", runner=r).rename_agent("w1:p1", None)
        self.assertEqual(r.calls[0][3:], ["agent", "rename", "w1:p1", "--clear"])

    def test_pane_process_info_maps_to_cli(self) -> None:
        r = Runner(response({"process_info": {"pane_id": "w1:p1", "foreground_processes": []}}))
        info = HerdrClient("test1", runner=r).pane_process_info("w1:p1")
        self.assertEqual(info["pane_id"], "w1:p1")
        self.assertEqual(r.calls[0][3:], ["pane", "process-info", "--pane", "w1:p1"])

    def test_pane_process_info_maps_to_cli_and_result(self) -> None:
        r = Runner(response({"process_info": {"pane_id": "w1:p1", "foreground_processes": []}}))
        value = HerdrClient("test1", runner=r).pane_process_info("w1:p1")
        self.assertEqual(value["pane_id"], "w1:p1")
        self.assertEqual(r.calls[0][3:], ["pane", "process-info", "--pane", "w1:p1"])

    def test_send_prompt_wait_requires_timeout(self) -> None:
        with self.assertRaises(ValueError):
            HerdrClient("test1", runner=Runner()).send_prompt("w1:p1", "hello", wait=True)

    def test_stalled_and_timed_out_prompt_are_outcome_unknown(self) -> None:
        stalled = json.dumps({"error": {"code": "agent_prompt_stalled", "message": "stalled"}})
        timed_out = json.dumps({"error": {"code": "timeout", "message": "timed out"}})
        for body in (stalled, timed_out):
            runner = Runner(subprocess.CompletedProcess([], 1, body, ""))
            with self.subTest(body=body), self.assertRaises(HerdrPromptOutcomeUnknown):
                HerdrClient("test1", runner=runner).send_prompt(
                    "w1:p1", "hello", wait=True, timeout_ms=120000
                )

    def test_client_process_timeout_during_prompt_is_outcome_unknown(self) -> None:
        def timeout_runner(argv, timeout):
            raise subprocess.TimeoutExpired(argv, timeout)

        with self.assertRaises(HerdrPromptOutcomeUnknown):
            HerdrClient("test1", runner=timeout_runner).send_prompt("w1:p1", "hello")


class TestCli(unittest.TestCase):
    def test_wait_until_explicit_value_replaces_default(self) -> None:
        captured = {}

        class StubClient:
            def __init__(self, session):
                pass

            def wait_agent(self, target, *, until, timeout_ms):
                captured["target"] = target
                captured["until"] = list(until)
                return type("Result", (), {"raw": {"ok": True}})()

        with mock.patch.object(cli_module, "HerdrClient", StubClient), redirect_stdout(io.StringIO()):
            self.assertEqual(cli_module.main(["wait-agent", "w1:p1", "--until", "idle"]), 0)
        self.assertEqual(captured["until"], ["idle"])

    def test_free_text_send_prompt_is_not_an_agent_cli_command(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            cli_module.main(["send-prompt", "w1:p1", "arbitrary text"])
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
