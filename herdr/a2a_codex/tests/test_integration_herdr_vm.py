"""Opt-in integration test against a real Herdr server, without model calls.

Run inside the Ubuntu VM after selecting an isolated, already-running Herdr
session (for example ``HERDR_TEST_SESSION=test1``):

    A2A_HERDR_INTEGRATION=1 HERDR_TEST_SESSION=test1 \\
      PYTHONPATH=herdr PYTHONDONTWRITEBYTECODE=1 \\
      python3 -m unittest discover -s herdr/a2a_codex/tests -p test_integration_herdr_vm.py -v

The test creates and closes one temporary tab in that named session. It does
not create/stop a Herdr session, touch the default session, or invoke a model.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from a2a_codex import HerdrClient


ENABLED = os.environ.get("A2A_HERDR_INTEGRATION") == "1"
SESSION = os.environ.get("HERDR_TEST_SESSION")


@unittest.skipUnless(ENABLED and SESSION,
                     "设置 A2A_HERDR_INTEGRATION=1 和 HERDR_TEST_SESSION 指定隔离的运行中 session")
class TestHerdrIntegrationVM(unittest.TestCase):
    def setUp(self) -> None:
        self.client = HerdrClient(SESSION)
        self.cwd = tempfile.TemporaryDirectory(prefix="a2a-herdr-it-")
        self.addCleanup(self.cwd.cleanup)
        self.tab = self.client.create_tab(
            label="a2a-it-%d" % os.getpid(), cwd=str(Path(self.cwd.name).resolve()), focus=False
        )
        self.addCleanup(self._close_tab)

    def _close_tab(self) -> None:
        if self.tab.tab_id:
            try:
                self.client.delete_tab(self.tab.tab_id)
            except Exception as exc:  # cleanup errors must remain visible in VM output
                print("清理临时 Herdr tab 失败: %s" % exc)

    def test_detect_rename_wait_prompt_and_read_without_model(self) -> None:
        pane_id = self.tab.pane_id
        self.assertIsNotNone(pane_id)
        self.client.start_agent(pane_id, "bash -c 'exec -a pi sleep 120'")

        detected = self.client.wait_for_agent_detected(pane_id, timeout_s=30)
        self.assertEqual(detected.agent, "pi")
        info = self.client.pane_process_info(pane_id)
        argv = [process.get("argv") for process in info.get("foreground_processes", [])]
        self.assertTrue(any(command and command[0] == "pi" for command in argv), argv)

        name = "a2a_it_%d" % os.getpid()
        self.client.rename_agent(pane_id, name)
        self.assertEqual(self.client.get_agent(name).pane_id, pane_id)
        ready = self.client.wait_agent(name, until=("idle", "done"), timeout_ms=15000)
        self.assertIn(ready.status, {"idle", "done"})

        self.assertIsInstance(self.client.read_agent(pane_id, lines=20), str)

        # This targets the fake pi process above, not a model-backed agent.
        accepted = self.client.send_prompt(pane_id, "A2A_HERDR_INTEGRATION_NO_MODEL")
        self.assertEqual(accepted.pane_id, pane_id)


if __name__ == "__main__":
    unittest.main()
