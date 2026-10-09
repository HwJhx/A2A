"""状态目录解析测试。"""
from __future__ import annotations

import os
import unittest
from pathlib import Path

from a2a import PathConfigError, default_registry_path, default_topology_path, state_dir


class StateDirTests(unittest.TestCase):
    HOME = os.path.expanduser("~")

    def test_priority_explicit_then_xdg_then_home(self):
        env = {"A2A_STATE_DIR": "/data/a2a", "XDG_STATE_HOME": "/xdg"}
        self.assertEqual(state_dir(env), Path("/data/a2a"))
        self.assertEqual(state_dir({"XDG_STATE_HOME": "/xdg"}), Path("/xdg/a2a"))
        self.assertEqual(state_dir({}), Path(self.HOME) / ".local" / "state" / "a2a")

    def test_relative_xdg_is_ignored_per_the_xdg_spec(self):
        self.assertEqual(state_dir({"XDG_STATE_HOME": "relative/dir"}),
                         Path(self.HOME) / ".local" / "state" / "a2a")

    def test_relative_explicit_dir_is_rejected(self):
        # 不同 agent 的工作目录不同,相对路径会悄悄产生多份互不相通的状态
        with self.assertRaises(PathConfigError):
            state_dir({"A2A_STATE_DIR": "relative/dir"})

    def test_tilde_is_expanded(self):
        self.assertEqual(state_dir({"A2A_STATE_DIR": "~/a2a_state"}), Path(self.HOME) / "a2a_state")

    def test_empty_values_are_ignored(self):
        self.assertEqual(state_dir({"A2A_STATE_DIR": "", "XDG_STATE_HOME": ""}),
                         Path(self.HOME) / ".local" / "state" / "a2a")

    def test_default_files(self):
        env = {"A2A_STATE_DIR": "/data/a2a"}
        self.assertEqual(default_registry_path(env), Path("/data/a2a/registry.json"))
        self.assertEqual(default_topology_path(env), Path("/data/a2a/topology.yaml"))

    def test_topology_override(self):
        env = {"A2A_STATE_DIR": "/data/a2a", "A2A_TOPOLOGY": "/etc/a2a/topo.yaml"}
        self.assertEqual(default_topology_path(env), Path("/etc/a2a/topo.yaml"))
        with self.assertRaises(PathConfigError):
            default_topology_path({"A2A_TOPOLOGY": "topo.yaml"})


if __name__ == "__main__":
    unittest.main()
