from __future__ import annotations

import tempfile
import unittest
import multiprocessing
import os
from pathlib import Path
from unittest import mock

import yaml

from a2a_codex.identity import AgentIdentity, IdentityError, IdentityMismatchError
from a2a_codex.registry import (
    AgentAlreadyRegisteredError,
    AgentNotRegisteredError,
    AmbiguousPaneError,
    Registry,
    RuntimeAddressConflictError,
)
from a2a_codex.topology import Topology, TopologyError, TopologyStore
from a2a_codex.spool import Spool, SpoolError
from a2a_codex.audit import AuditLog
import a2a_codex.topology as topology_module
import a2a_codex.registry as registry_module


CONFIG = {
    "version": 1,
    "project_id": "soc_a",
    "workspace_label": "SoC A",
    "roles": {
        "dv": {"label": "Verification", "kind": "pi", "launcher": "/opt/fnx_dv"},
        "sw": {"label": "Software", "kind": "pi"},
        "rtl": {"label": "RTL", "kind": "pi"},
    },
    "ips": ["uart", "gpio"],
    "edges": [
        {"id": "dv_done", "from": "dv", "to": "sw", "template": "{ip} verification done"},
        {"id": "dv_rtl", "from": "dv", "to": "rtl", "template": "Fix {ip}"},
    ],
}


def _add_ip_in_child(path: str, ip_id: str) -> None:
    TopologyStore(path).add_ip(ip_id)


def _registry_register_in_child(path: str, index: int) -> None:
    ip_id = f"ip{index}"
    identity = AgentIdentity("soc_a", ip_id, "dv", f"dv_{ip_id}")
    Registry(path).register(identity, session="test", workspace_id="w1", tab_id="w1:t1",
                            pane_id=f"w1:p{index + 1}")


def _crash_registry_before_replace(path: str) -> None:
    def exit_before_replace(source, destination):
        os._exit(73)

    registry_module.os.replace = exit_before_replace
    ip_id = "crash_ip"
    identity = AgentIdentity("soc_a", ip_id, "dv", f"dv_{ip_id}")
    Registry(path).register(identity, session="test", workspace_id="w1", tab_id="w1:t1",
                            pane_id="w1:p99")


def yaml_dump(data) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


class TopologyTests(unittest.TestCase):
    def test_topology_store_rejects_relative_path(self) -> None:
        with self.assertRaises(ValueError):
            TopologyStore("relative/topology.yaml")

    def test_valid_topology_and_stable_identity(self) -> None:
        topology = Topology.from_dict(CONFIG)
        identity = AgentIdentity.create(topology, "dv", "uart")
        self.assertEqual(identity.agent_id, "dv_uart")
        self.assertEqual(topology.get_edge("dv_done").target_role, "sw")
        self.assertFalse(topology.has_node("sw", "i2c"))

    def test_load_yaml_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "topology.yaml"
            path.write_text("version: 1\nproject_id: p\nroles: {dv: {}}\nips: [uart]\nedges: []\n", encoding="utf-8")
            self.assertEqual(Topology.load(path).project_id, "p")

    def test_rejects_invalid_node_and_edge(self) -> None:
        topology = Topology.from_dict(CONFIG)
        with self.assertRaises(IdentityError):
            AgentIdentity.create(topology, "fake", "uart")
        with self.assertRaises(IdentityError):
            AgentIdentity.create(topology, "dv", "unknown")
        bad = {**CONFIG, "edges": [{"id": "bad", "from": "dv", "to": "missing", "template": "x"}]}
        with self.assertRaises(TopologyError):
            Topology.from_dict(bad)

    def test_rejects_duplicate_ip_and_unsafe_template(self) -> None:
        with self.assertRaises(TopologyError):
            Topology.from_dict({**CONFIG, "ips": ["uart", "uart"]})
        bad = {**CONFIG, "edges": [{"id": "bad", "from": "dv", "to": "sw", "template": "{target}"}]}
        with self.assertRaises(TopologyError):
            Topology.from_dict(bad)

    def test_rejects_malformed_role_edge_values(self) -> None:
        bad = {**CONFIG, "edges": [{"id": "bad", "from": ["dv"], "to": "sw", "template": "x"}]}
        with self.assertRaises(TopologyError):
            Topology.from_dict(bad)
        with self.assertRaises(TopologyError):
            Topology.from_dict({**CONFIG, "version": True})

    def test_topology_store_explicit_reload_and_invalid_reload_keeps_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "topology.yaml"
            path.write_text("version: 1\nproject_id: p\nroles: {dv: {}, sw: {}}\nips: [uart]\nedges: []\n", encoding="utf-8")
            store = TopologyStore(path)
            previous = store.current
            path.write_text("version: 1\nproject_id: p\nroles: {dv: {}, sw: {}}\nips: [uart]\nedges:\n- {id: dv_done, from: dv, to: sw, template: '{ip} done'}\n", encoding="utf-8")
            self.assertEqual(len(store.reload().edges), 1)
            self.assertEqual(len(previous.edges), 0)
            path.write_text("version: invalid\n", encoding="utf-8")
            with self.assertRaises(TopologyError):
                store.reload()
            self.assertEqual(len(store.current.edges), 1)

    def test_topology_store_dynamic_mutations_validate_persist_and_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "topology.yaml"
            path.write_text(yaml_dump(CONFIG), encoding="utf-8")
            store = TopologyStore(path)
            original = path.read_text(encoding="utf-8")

            store.add_ip("i2c")
            store.add_role("qa", label="QA", kind="pi", launcher="/opt/qa")
            store.add_edge("qa_to_sw", "qa", "sw", "Review {ip}")
            store.set_template("qa_to_sw", "Approve {ip}")
            before_launcher_update = path.read_text(encoding="utf-8")
            store.set_role_launcher("qa", "/opt/qa-v2")

            updated = TopologyStore(path).current
            self.assertIn("i2c", updated.ips)
            self.assertEqual(updated.roles["qa"].launcher, "/opt/qa-v2")
            self.assertEqual(updated.get_edge("qa_to_sw").template, "Approve {ip}")
            self.assertEqual(store.backup_path.read_text(encoding="utf-8"), before_launcher_update)

            before_invalid = path.read_text(encoding="utf-8")
            with self.assertRaises(TopologyError):
                store.add_edge("invalid", "qa", "missing", "bad")
            self.assertEqual(path.read_text(encoding="utf-8"), before_invalid)

            with self.assertRaises(TopologyError):
                store.remove_role("qa")
            store.remove_role("qa", cascade_edges=True)
            store.remove_edge("dv_rtl")
            store.remove_ip("i2c")
            self.assertNotIn("qa", store.current.roles)
            self.assertNotIn("i2c", store.current.ips)
            self.assertEqual(len(store.current.edges), 1)
            self.assertNotEqual(path.read_text(encoding="utf-8"), original)

    def test_topology_store_hot_reloads_external_changes_and_keeps_last_good(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "topology.yaml"
            path.write_text(yaml_dump(CONFIG), encoding="utf-8")
            first = TopologyStore(path)
            second = TopologyStore(path)
            second.add_ip("i2c")
            self.assertIn("i2c", first.current.ips)

            manual_edit = dict(CONFIG)
            manual_edit["ips"] = list(CONFIG["ips"]) + ["i2c", "manual"]
            path.write_text(yaml_dump(manual_edit), encoding="utf-8")
            self.assertIn("manual", first.current.ips)

            last_good = first.current
            path.write_text("version: nope\n", encoding="utf-8")
            self.assertEqual(first.current, last_good)
            self.assertIsNotNone(first.last_error)

    def test_topology_store_concurrent_process_updates_are_not_lost(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "topology.yaml"
            path.write_text(yaml_dump(CONFIG), encoding="utf-8")
            ctx = multiprocessing.get_context("fork")
            processes = [ctx.Process(target=_add_ip_in_child, args=(str(path), f"ip{index}"))
                         for index in range(6)]
            for process in processes:
                process.start()
            for process in processes:
                process.join(15)
            for process in processes:
                self.assertFalse(process.is_alive(), "拓扑并发写入进程超时")
                self.assertEqual(process.exitcode, 0)
            topology = TopologyStore(path).current
            self.assertTrue({f"ip{index}" for index in range(6)}.issubset(topology.ips))

    def test_content_revision_does_not_change_on_touch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "topology.yaml"
            path.write_text(yaml_dump(CONFIG), encoding="utf-8")
            store = TopologyStore(path)
            revision = store.revision
            os.utime(path, None)
            self.assertEqual(store.revision, revision)
            store.add_ip("i2c")
            self.assertNotEqual(store.revision, revision)

    def test_invalid_external_file_is_parsed_once_per_signature(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "topology.yaml"
            path.write_text(yaml_dump(CONFIG), encoding="utf-8")
            store = TopologyStore(path)
            path.write_text("version: [invalid\n", encoding="utf-8")
            with mock.patch.object(topology_module.yaml, "safe_load", wraps=yaml.safe_load) as parser:
                for _ in range(5):
                    store.current
                self.assertEqual(parser.call_count, 1)

    def test_environment_identity_is_bound_to_project_and_topology(self) -> None:
        topology = Topology.from_dict(CONFIG)
        env = {"A2A_PROJECT_ID": "soc_a", "A2A_ROLE": "dv", "A2A_IP": "uart"}
        self.assertEqual(AgentIdentity.from_environment(env, topology).agent_id, "dv_uart")
        with self.assertRaises(IdentityMismatchError):
            AgentIdentity.from_environment({**env, "A2A_PROJECT_ID": "other"}, topology)
        with self.assertRaises(IdentityError):
            AgentIdentity.from_environment({"A2A_ROLE": "dv"}, topology)


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "registry.json"
        self.registry = Registry(self.path)
        self.identity = AgentIdentity("soc_a", "uart", "dv", "dv_uart")

    def register(self, agent_id="dv_uart", pane="w1:p2", session="test"):
        identity = self.identity if agent_id == "dv_uart" else AgentIdentity("soc_a", "uart", "sw", agent_id)
        return self.registry.register(identity, session=session, workspace_id="w1", tab_id="w1:t1",
                                      pane_id=pane, status="idle")

    def test_register_persists_and_resolves_sender(self) -> None:
        record = self.register()
        self.assertEqual(record.agent_name, "dv_uart")
        reloaded = Registry(self.path)
        env = {"A2A_PROJECT_ID": "soc_a", "A2A_ROLE": "dv", "A2A_IP": "uart", "HERDR_PANE_ID": "w1:p2"}
        self.assertEqual(reloaded.resolve_sender(env, session="test").agent_id, "dv_uart")

    def test_identity_mismatch_is_rejected(self) -> None:
        self.register()
        env = {"A2A_PROJECT_ID": "soc_a", "A2A_ROLE": "sw", "A2A_IP": "uart", "HERDR_PANE_ID": "w1:p2"}
        with self.assertRaises(IdentityMismatchError):
            self.registry.resolve_sender(env, session="test")

    def test_sender_missing_or_unregistered_pane_is_rejected(self) -> None:
        env = {"A2A_PROJECT_ID": "soc_a", "A2A_ROLE": "dv", "A2A_IP": "uart"}
        with self.assertRaises(IdentityError):
            self.registry.resolve_sender(env, session="test")
        env["HERDR_PANE_ID"] = "w1:p404"
        with self.assertRaises(AgentNotRegisteredError):
            self.registry.resolve_sender(env, session="test")
        self.register()
        self.registry.unregister("dv_uart")
        with self.assertRaises(AgentNotRegisteredError):
            self.registry.resolve_sender({**env, "HERDR_PANE_ID": "w1:p2"}, session="test")
        self.register()
        env["A2A_ROLE"] = "dv"
        env["A2A_IP"] = "gpio"
        env["HERDR_PANE_ID"] = "w1:p2"
        with self.assertRaises(IdentityMismatchError):
            self.registry.resolve_sender(env, session="test")

    def test_duplicate_business_id_and_runtime_location_rejected(self) -> None:
        self.register()
        with self.assertRaises(AgentAlreadyRegisteredError):
            self.register()
        with self.assertRaises(RuntimeAddressConflictError):
            self.register(agent_id="sw_uart", pane="w1:p2")

    def test_duplicate_agent_name_rejected_within_session(self) -> None:
        self.register()
        other = AgentIdentity("soc_a", "gpio", "sw", "sw_gpio")
        with self.assertRaises(RuntimeAddressConflictError):
            self.registry.register(other, session="test", workspace_id="w1", tab_id="w1:t1",
                                   pane_id="w1:p3", agent_name="dv_uart")

    def test_rebuild_updates_runtime_without_changing_business_identity(self) -> None:
        self.register()
        updated = self.registry.update_runtime("dv_uart", tab_id="w1:t9", pane_id="w1:p9", agent_name="dv_uart")
        self.assertEqual(updated.agent_id, "dv_uart")
        self.assertEqual(updated.pane_id, "w1:p9")
        with self.assertRaises(AgentNotRegisteredError):
            self.registry.get_by_pane("w1:p2", session="test")
        self.assertEqual(Registry(self.path).get("dv_uart").tab_id, "w1:t9")

    def test_session_scoped_lookup_and_ambiguous_lookup(self) -> None:
        self.register(session="s1")
        # 同一业务身份不能在 Registry 中重复，因此第二 session 由另一 project/role 节点表示。
        identity = AgentIdentity("soc_a", "uart", "sw", "sw_uart")
        self.registry.register(identity, session="s2", workspace_id="w1", tab_id="w1:t1", pane_id="w1:p2")
        with self.assertRaises(AmbiguousPaneError):
            self.registry.get_by_pane("w1:p2")
        self.assertEqual(self.registry.get_by_pane("w1:p2", session="s2").agent_id, "sw_uart")

    def test_update_status_and_unregister(self) -> None:
        self.register()
        self.assertEqual(self.registry.update_status("dv_uart", "working").status, "working")
        self.assertEqual(self.registry.unregister("dv_uart").agent_id, "dv_uart")
        self.assertEqual(self.registry.list(), [])

    def test_non_object_json_raises_registry_error(self) -> None:
        from a2a_codex.registry import RegistryError

        for content in ("null", "[]", "\"text\""):
            self.path.write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaises(RegistryError):
                Registry(self.path).list()

    def test_corrupt_json_and_corrupt_record_fail_closed(self) -> None:
        from a2a_codex.registry import RegistryError

        for content in ("{", '{"format_version":1,"agents":{"dv_uart":"bad"}}'):
            self.path.write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaises(RegistryError):
                Registry(self.path).list()

    def test_session_none_is_rejected_and_sender_lookup_requires_session(self) -> None:
        with self.assertRaises(ValueError):
            self.register(session=None)
        with self.assertRaises(TypeError):
            self.registry.resolve_sender({"HERDR_PANE_ID": "w1:p2"})

    def test_registry_rejects_relative_path(self) -> None:
        with self.assertRaises(ValueError):
            Registry("relative/registry.json")

    def test_registry_concurrent_process_registrations_are_not_lost(self) -> None:
        context = multiprocessing.get_context("fork")
        processes = [context.Process(target=_registry_register_in_child, args=(str(self.path), index))
                     for index in range(6)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(30)
        for process in processes:
            self.assertFalse(process.is_alive(), "Registry 并发注册进程超时")
            self.assertEqual(process.exitcode, 0)
        records = Registry(self.path).list()
        self.assertEqual(len(records), 6)
        self.assertEqual({record.agent_id for record in records},
                         {f"dv_ip{index}" for index in range(6)})

    def test_registry_process_crash_before_replace_preserves_previous_json(self) -> None:
        existing = self.register()
        context = multiprocessing.get_context("fork")
        process = context.Process(target=_crash_registry_before_replace, args=(str(self.path),))
        process.start()
        process.join(30)
        self.assertFalse(process.is_alive(), "Registry 故障注入进程超时")
        self.assertEqual(process.exitcode, 73)
        records = Registry(self.path).list()
        self.assertEqual([record.agent_id for record in records], [existing.agent_id])

    def test_stateful_store_paths_reject_relative_paths(self) -> None:
        with self.assertRaises(ValueError):
            Registry.default_state_dir({"A2A_STATE_DIR": "relative/state"})
        with self.assertRaises(SpoolError):
            Spool("relative/spool")
        with self.assertRaises(ValueError):
            AuditLog("relative/audit.jsonl")

    def test_default_state_dir_precedence(self) -> None:
        explicit = Registry.default_state_dir({"A2A_STATE_DIR": "~/custom/a2a", "XDG_STATE_HOME": "/xdg"})
        self.assertEqual(explicit, (Path.home() / "custom/a2a").resolve())
        xdg = Registry.default_state_dir({"XDG_STATE_HOME": "/var/state"})
        self.assertEqual(xdg, Path("/var/state/a2a").resolve())
        fallback = Registry.default_state_dir({})
        self.assertEqual(fallback, (Path.home() / ".local/state/a2a").resolve())
        self.assertEqual(Registry(environ={"A2A_STATE_DIR": "/tmp/a2a-state"}).path,
                         Path("/tmp/a2a-state/registry.json").resolve())
        with self.assertRaises(ValueError):
            Registry.default_state_dir({"XDG_STATE_HOME": "relative/xdg"})


if __name__ == "__main__":
    unittest.main()
