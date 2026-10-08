from __future__ import annotations

import json
import subprocess
import unittest
from typing import Any, Optional, Sequence

from a2a_codex import HerdrClient
from a2a_codex.errors import HerdrAgentNotReady
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


if __name__ == "__main__":
    unittest.main()
