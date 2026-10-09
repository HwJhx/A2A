"""注册表单元测试:移植自 a2a_codex,并补上跨进程并发与写入中途崩溃。"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from a2a import (
    AgentAlreadyRegisteredError,
    AgentIdentity,
    AgentNotRegisteredError,
    AmbiguousPaneError,
    Registry,
    RegistryError,
    RuntimeAddressConflictError,
)

DV = AgentIdentity("soc_a", "uart", "dv", "dv_uart")
SW = AgentIdentity("soc_a", "uart", "sw", "sw_uart")


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "registry.json"
        self.registry = Registry(self.path)

    def register(self, identity=DV, pane="w1:p2", session="test", **kw):
        return self.registry.register(identity, session=session, workspace_id="w1", tab_id="w1:t1",
                                      pane_id=pane, status="idle", **kw)

    def test_register_persists_and_defaults_agent_name(self):
        record = self.register()
        self.assertEqual(record.agent_name, "dv_uart")
        self.assertEqual(record.lifecycle, "running")
        reloaded = Registry(self.path)
        self.assertEqual(reloaded.get("dv_uart"), record)
        self.assertEqual(reloaded.get_by_pane("w1:p2", session="test").agent_id, "dv_uart")

    def test_record_exposes_identity(self):
        self.assertEqual(self.register().identity, DV)

    def test_duplicate_business_id_and_runtime_location_rejected(self):
        self.register()
        with self.assertRaises(AgentAlreadyRegisteredError):
            self.register()
        with self.assertRaises(RuntimeAddressConflictError):
            self.register(SW, pane="w1:p2")  # 同会话同 pane

    def test_duplicate_agent_name_rejected_within_session(self):
        self.register()
        other = AgentIdentity("soc_a", "gpio", "sw", "sw_gpio")
        with self.assertRaises(RuntimeAddressConflictError):
            self.registry.register(other, session="test", workspace_id="w1", tab_id="w1:t1",
                                   pane_id="w1:p3", agent_name="dv_uart")

    def test_same_pane_in_different_sessions_is_allowed_but_ambiguous_without_session(self):
        self.register(session="s1")
        self.register(SW, session="s2")  # 同一个 w1:p2,另一个会话
        with self.assertRaises(AmbiguousPaneError):
            self.registry.get_by_pane("w1:p2")
        self.assertEqual(self.registry.get_by_pane("w1:p2", session="s2").agent_id, "sw_uart")
        self.assertEqual(self.registry.get_by_pane("w1:p2", session="s1").agent_id, "dv_uart")

    def test_session_must_be_non_empty_text(self):
        for bad in (None, "", "  "):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.registry.register(DV, session=bad, workspace_id="w1", tab_id="w1:t1", pane_id="w1:p2")

    def test_rebuild_updates_runtime_without_changing_business_identity(self):
        self.register()
        updated = self.registry.update_runtime("dv_uart", tab_id="w1:t9", pane_id="w1:p9")
        self.assertEqual(updated.agent_id, "dv_uart")
        self.assertEqual(updated.pane_id, "w1:p9")
        with self.assertRaises(AgentNotRegisteredError):
            self.registry.get_by_pane("w1:p2", session="test")
        self.assertEqual(Registry(self.path).get("dv_uart").tab_id, "w1:t9")

    def test_update_cannot_collide_with_another_agent(self):
        self.register()
        self.register(SW, pane="w1:p3")
        with self.assertRaises(RuntimeAddressConflictError):
            self.registry.update_runtime("sw_uart", pane_id="w1:p2")
        with self.assertRaises(RuntimeAddressConflictError):
            self.registry.update_runtime("sw_uart", agent_name="dv_uart")

    def test_update_validation(self):
        self.register()
        with self.assertRaises(ValueError):
            self.registry.update_runtime("dv_uart", status="sleeping")
        with self.assertRaises(ValueError):
            self.registry.update_runtime("dv_uart", lifecycle="vanished")
        with self.assertRaises(ValueError):
            self.registry.update_runtime("dv_uart", agent_name="x" * 33)
        with self.assertRaises(AgentNotRegisteredError):
            self.registry.update_runtime("nobody", status="idle")

    def test_status_lifecycle_and_unregister(self):
        self.register()
        self.assertEqual(self.registry.update_status("dv_uart", "working").status, "working")
        self.assertEqual(self.registry.update_runtime("dv_uart", lifecycle="stopped").lifecycle, "stopped")
        self.assertEqual(self.registry.unregister("dv_uart").agent_id, "dv_uart")
        self.assertEqual(self.registry.list(), [])
        with self.assertRaises(AgentNotRegisteredError):
            self.registry.unregister("dv_uart")

    def test_find_and_list_filters(self):
        self.register()
        self.register(SW, pane="w1:p3")
        gpio = AgentIdentity("soc_a", "gpio", "dv", "dv_gpio")
        self.registry.register(gpio, session="test", workspace_id="w1", tab_id="w1:t1", pane_id="w1:p4")
        self.assertEqual(self.registry.find("dv", "uart").agent_id, "dv_uart")
        self.assertIsNone(self.registry.find("sw", "gpio"))
        self.assertEqual([r.agent_id for r in self.registry.list(role="dv")], ["dv_gpio", "dv_uart"])
        self.assertEqual([r.agent_id for r in self.registry.list(ip_id="uart")], ["dv_uart", "sw_uart"])
        self.assertEqual(len(self.registry.list(project_id="other")), 0)

    def test_corrupt_files_raise_registry_error(self):
        for content in ("null", "[]", '"text"', "{not json", '{"format_version": 99, "agents": {}}',
                        '{"format_version": 1, "agents": []}',
                        '{"format_version": 1, "agents": {"dv_uart": 5}}',
                        '{"format_version": 1, "agents": {"dv_uart": {"bogus": 1}}}'):
            self.path.write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaises(RegistryError):
                Registry(self.path).list()

    def test_relative_path_rejected(self):
        with self.assertRaises(RegistryError):
            Registry("registry.json")

    def test_default_path_follows_state_dir(self):
        old = os.environ.get("A2A_STATE_DIR")
        os.environ["A2A_STATE_DIR"] = self.temp.name
        try:
            self.assertEqual(Registry().path, Path(self.temp.name) / "registry.json")
        finally:
            if old is None:
                del os.environ["A2A_STATE_DIR"]
            else:
                os.environ["A2A_STATE_DIR"] = old

    # ---- 跨进程并发与崩溃 ------------------------------------------
    def test_concurrent_registration_from_many_processes_loses_nothing(self):
        script = textwrap.dedent("""
            import sys
            from a2a import AgentIdentity, Registry
            path, worker = sys.argv[1], int(sys.argv[2])
            registry = Registry(path)
            for n in range(5):
                ip = "p%d-%d" % (worker, n)
                identity = AgentIdentity("soc_a", ip, "dv", "dv_" + ip)
                registry.register(identity, session="s", workspace_id="w1", tab_id="w1:t1",
                                  pane_id="w1:p%d%d" % (worker, n))
        """)
        workers = 8
        procs = [subprocess.Popen([sys.executable, "-c", script, str(self.path), str(w)],
                                  env=os.environ.copy(), stderr=subprocess.PIPE) for w in range(workers)]
        for proc in procs:
            _, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err.decode())
        records = Registry(self.path).list()
        self.assertEqual(len(records), workers * 5)
        self.assertEqual(len({r.agent_id for r in records}), workers * 5)

    def test_concurrent_updates_to_the_same_record_are_serialised(self):
        self.register()
        script = textwrap.dedent("""
            import sys
            from a2a import Registry
            registry = Registry(sys.argv[1])
            for _ in range(20):
                registry.update_status("dv_uart", sys.argv[2])
        """)
        procs = [subprocess.Popen([sys.executable, "-c", script, str(self.path), status],
                                  env=os.environ.copy(), stderr=subprocess.PIPE)
                 for status in ("idle", "working", "done", "blocked")]
        for proc in procs:
            _, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err.decode())
        self.assertIn(self.registry.get("dv_uart").status, {"idle", "working", "done", "blocked"})

    def test_crash_in_the_middle_of_a_write_leaves_the_registry_readable(self):
        record = self.register()
        before = self.path.read_bytes()
        script = textwrap.dedent("""
            import os, sys
            from a2a import AgentIdentity, Registry
            real = os.replace
            def dying_replace(src, dst):
                os._exit(137)          # 临时文件已写好,但还没替换到目标位置时进程被杀
            os.replace = dying_replace
            Registry(sys.argv[1]).register(AgentIdentity("soc_a", "uart", "sw", "sw_uart"), session="test",
                                           workspace_id="w1", tab_id="w1:t1", pane_id="w1:p3")
        """)
        proc = subprocess.run([sys.executable, "-c", script, str(self.path)], env=os.environ.copy())
        self.assertEqual(proc.returncode, 137)
        self.assertEqual(self.path.read_bytes(), before)           # 原文件完好
        self.assertEqual(Registry(self.path).list(), [record])      # 半截写入不可见
        self.register(SW, pane="w1:p3")                             # 之后可继续正常写入
        self.assertEqual(len(Registry(self.path).list()), 2)


if __name__ == "__main__":
    unittest.main()
