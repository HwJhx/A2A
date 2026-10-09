"""拓扑单元测试:校验(移植自 a2a_codex)、动态修改、热加载、跨进程并发。"""
from __future__ import annotations

import copy
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

from a2a import AgentIdentity, IdentityError, Topology, TopologyError, TopologyStore

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


def cfg(**changes):
    data = copy.deepcopy(CONFIG)
    data.update(changes)
    return data


class TopologyValidationTests(unittest.TestCase):
    def test_valid_topology_and_stable_identity(self):
        topology = Topology.from_dict(CONFIG)
        identity = AgentIdentity.create(topology, "dv", "uart")
        self.assertEqual(identity.agent_id, "dv_uart")
        self.assertEqual(topology.get_edge("dv_done").target_role, "sw")
        self.assertFalse(topology.has_node("sw", "i2c"))

    def test_load_yaml_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "topology.yaml"
            path.write_text("version: 1\nproject_id: p\nroles: {dv: {}}\nips: [uart]\nedges: []\n", encoding="utf-8")
            self.assertEqual(Topology.load(path).project_id, "p")

    def test_missing_or_broken_file(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(TopologyError):
                Topology.load(Path(temp) / "nope.yaml")
            bad = Path(temp) / "bad.yaml"
            bad.write_text("a: [unclosed", encoding="utf-8")
            with self.assertRaises(TopologyError):
                Topology.load(bad)

    def test_rejects_invalid_node_and_edge(self):
        topology = Topology.from_dict(CONFIG)
        with self.assertRaises(IdentityError):
            AgentIdentity.create(topology, "fake", "uart")
        with self.assertRaises(IdentityError):
            AgentIdentity.create(topology, "dv", "unknown")
        with self.assertRaises(TopologyError):
            Topology.from_dict(cfg(edges=[{"id": "bad", "from": "dv", "to": "missing", "template": "x"}]))

    def test_rejects_duplicates_self_loops_and_bad_ids(self):
        with self.assertRaises(TopologyError):
            Topology.from_dict(cfg(ips=["uart", "uart"]))
        with self.assertRaises(TopologyError):
            Topology.from_dict(cfg(edges=[
                {"id": "a", "from": "dv", "to": "sw", "template": "x"},
                {"id": "a", "from": "sw", "to": "dv", "template": "y"},
            ]))
        with self.assertRaises(TopologyError):
            Topology.from_dict(cfg(edges=[{"id": "loop", "from": "dv", "to": "dv", "template": "x"}]))
        for bad_id in ("Upper", "1x", "has space", "x" * 65):
            with self.assertRaises(TopologyError, msg=bad_id):
                Topology.from_dict(cfg(edges=[{"id": bad_id, "from": "dv", "to": "sw", "template": "x"}]))
        for bad_ip in ("UART", "has.dot", "x" * 33):
            with self.assertRaises(TopologyError, msg=bad_ip):
                Topology.from_dict(cfg(ips=[bad_ip]))

    def test_rejects_unsafe_templates(self):
        for template in ("{target}", "{ip.__class__}", "{ip!r}", "{ip:>10}", "{}", "{0}", "{ip", "  "):
            with self.assertRaises(TopologyError, msg=template):
                Topology.from_dict(cfg(edges=[{"id": "bad", "from": "dv", "to": "sw", "template": template}]))
        # 字面花括号需要写成 {{ }},允许
        Topology.from_dict(cfg(edges=[{"id": "ok", "from": "dv", "to": "sw", "template": "{ip} {{done}}"}]))

    def test_rejects_malformed_values(self):
        with self.assertRaises(TopologyError):
            Topology.from_dict(cfg(edges=[{"id": "bad", "from": ["dv"], "to": "sw", "template": "x"}]))
        with self.assertRaises(TopologyError):
            Topology.from_dict(cfg(version=True))
        with self.assertRaises(TopologyError):
            Topology.from_dict(cfg(version=2))
        with self.assertRaises(TopologyError):
            Topology.from_dict(cfg(roles={}))
        with self.assertRaises(TopologyError):
            Topology.from_dict(cfg(roles={"dv": {"launcher": "relative/path"}}))
        with self.assertRaises(TopologyError):
            Topology.from_dict([])

    def test_empty_ips_and_edges_are_allowed_for_dynamic_topologies(self):
        topology = Topology.from_dict(cfg(ips=[], edges=[]))
        self.assertEqual(topology.ips, ())
        self.assertEqual(list(topology.nodes()), [])
        data = copy.deepcopy(CONFIG)
        del data["ips"], data["edges"]
        self.assertEqual(Topology.from_dict(data).edges, {})

    def test_queries(self):
        topology = Topology.from_dict(CONFIG)
        self.assertEqual({e.edge_id for e in topology.edges_from("dv")}, {"dv_done", "dv_rtl"})
        self.assertEqual(topology.edges_from("sw"), [])
        self.assertEqual(len(list(topology.nodes())), 6)  # 3 角色 x 2 IP
        with self.assertRaises(TopologyError):
            topology.get_edge("nope")

    def test_to_dict_round_trip(self):
        topology = Topology.from_dict(CONFIG)
        again = Topology.from_dict(yaml.safe_load(yaml.safe_dump(topology.to_dict(), allow_unicode=True)))
        self.assertEqual(again, topology)

    def test_example_config_is_valid(self):
        example = Path(__file__).resolve().parent.parent / "config" / "topology.example.yaml"
        topology = Topology.load(example)
        self.assertEqual(topology.project_id, "soc_a")
        self.assertIn("dv_done", topology.edges)


class TopologyStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "topology.yaml"
        self.store = TopologyStore.create(self.path, CONFIG)

    def test_create_refuses_to_overwrite(self):
        with self.assertRaises(TopologyError):
            TopologyStore.create(self.path, CONFIG)

    def test_relative_path_rejected(self):
        with self.assertRaises(TopologyError):
            TopologyStore("topology.yaml")

    def test_missing_file_rejected(self):
        with self.assertRaises(TopologyError):
            TopologyStore(Path(self.temp.name) / "none.yaml")

    # ---- 动态修改 ---------------------------------------------------
    def test_mutations_persist_and_are_visible_to_other_stores(self):
        self.store.add_ip("spi")
        self.store.add_edge("sw_ack", "sw", "dv", "{ip} ack")
        self.store.set_template("dv_done", "{ip} done!")
        self.store.add_role("arch", label="架构")
        other = TopologyStore(self.path).current()
        self.assertIn("spi", other.ips)
        self.assertEqual(other.get_edge("sw_ack").source_role, "sw")
        self.assertEqual(other.get_edge("dv_done").template, "{ip} done!")
        self.assertEqual(other.roles["arch"].label, "架构")

    def test_remove_operations(self):
        self.store.remove_edge("dv_rtl")
        self.store.remove_ip("gpio")
        topology = self.store.current()
        self.assertNotIn("dv_rtl", topology.edges)
        self.assertEqual(topology.ips, ("uart",))

    def test_invalid_mutations_are_rejected_and_leave_the_file_untouched(self):
        before = self.path.read_bytes()
        cases = [
            lambda: self.store.add_ip("uart"),                       # 重复
            lambda: self.store.add_ip("BAD IP"),                     # 非法名
            lambda: self.store.remove_ip("nope"),
            lambda: self.store.add_edge("x", "dv", "ghost", "t"),    # 未知角色
            lambda: self.store.add_edge("dv_done", "dv", "sw", "t"), # 重复 id
            lambda: self.store.add_edge("loop", "dv", "dv", "t"),
            lambda: self.store.add_edge("bad_t", "dv", "sw", "{target}"),
            lambda: self.store.remove_edge("nope"),
            lambda: self.store.set_template("nope", "x"),
            lambda: self.store.set_template("dv_done", "{ip!r}"),
            lambda: self.store.add_role("dv"),                       # 重复
            lambda: self.store.set_role_launcher("dv", "relative"),
            lambda: self.store.remove_role("ghost"),
        ]
        for index, call in enumerate(cases):
            with self.assertRaises(TopologyError, msg=f"case {index}"):
                call()
        self.assertEqual(self.path.read_bytes(), before)

    def test_remove_role_requires_cascade_when_edges_reference_it(self):
        with self.assertRaises(TopologyError) as ctx:
            self.store.remove_role("sw")
        self.assertIn("dv_done", str(ctx.exception))
        topology = self.store.remove_role("sw", cascade_edges=True)
        self.assertNotIn("sw", topology.roles)
        self.assertNotIn("dv_done", topology.edges)
        self.assertIn("dv_rtl", topology.edges)  # 不相关的边保留

    def test_set_role_launcher(self):
        self.store.set_role_launcher("sw", "/opt/fnx_sw")
        self.assertEqual(self.store.current().roles["sw"].launcher, "/opt/fnx_sw")
        self.store.set_role_launcher("sw", None)
        self.assertIsNone(self.store.current().roles["sw"].launcher)

    def test_backup_holds_previous_version(self):
        before = self.path.read_text(encoding="utf-8")
        self.store.add_ip("spi")
        self.assertEqual(self.store.backup_path.read_text(encoding="utf-8"), before)

    def test_written_file_is_valid_yaml_with_header_and_chinese_preserved(self):
        self.store.add_role("arch", label="架构智能体")
        text = self.path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#"))
        self.assertIn("架构智能体", text)  # 没有被转义成 \u 序列

    def test_revision_changes_only_when_content_changes(self):
        first = self.store.revision
        self.assertRegex(first, r"^[0-9a-f]{12}$")
        self.store.add_ip("spi")
        second = self.store.revision
        self.assertNotEqual(first, second)
        self.assertEqual(TopologyStore(self.path).revision, second)  # 另一实例读到同一修订号

    def test_update_refuses_when_disk_file_is_already_invalid(self):
        self.path.write_text("version: 1\nproject_id: soc_a\nroles: {}\n", encoding="utf-8")
        with self.assertRaises(TopologyError):
            self.store.add_ip("spi")

    # ---- 热加载 -----------------------------------------------------
    def test_hot_reload_picks_up_manual_edits(self):
        self.assertFalse(self.store.reload_if_changed())
        data = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        data["ips"].append("i2c")
        self.path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        self.assertIn("i2c", self.store.current().ips)
        self.assertFalse(self.store.reload_if_changed())  # 没有新变化

    def test_invalid_manual_edit_keeps_last_good_topology(self):
        good_revision = self.store.revision
        self.path.write_text("version: 1\nproject_id: soc_a\nroles: {}\nips: []\n", encoding="utf-8")
        topology = self.store.current()
        self.assertEqual(topology.ips, ("uart", "gpio"))  # 仍是上一份合法拓扑
        self.assertEqual(self.store.revision, good_revision)
        self.assertIn("roles", self.store.last_error)
        # 修复后恢复,并清除错误
        self.path.write_text(yaml.safe_dump(cfg(ips=["only"])), encoding="utf-8")
        self.assertEqual(self.store.current().ips, ("only",))
        self.assertIsNone(self.store.last_error)

    def test_deleted_file_keeps_serving_last_good(self):
        self.path.unlink()
        self.assertEqual(self.store.current().project_id, "soc_a")
        self.assertIn("无法访问", self.store.last_error)

    def test_hot_reload_sees_changes_made_through_another_store(self):
        other = TopologyStore(self.path)
        other.add_ip("spi")
        self.assertIn("spi", self.store.current().ips)

    # ---- 跨进程并发 ------------------------------------------------
    def test_concurrent_writers_from_many_processes_lose_nothing(self):
        script = textwrap.dedent("""
            import sys
            from a2a import TopologyStore
            path, worker = sys.argv[1], int(sys.argv[2])
            store = TopologyStore(path)
            for n in range(5):
                store.add_ip("p%d-%d" % (worker, n))
                store.add_edge("e%d-%d" % (worker, n), "dv", "sw", "{ip} w%d n%d" % (worker, n))
        """)
        workers = 6
        procs = [subprocess.Popen([sys.executable, "-c", script, str(self.path), str(w)],
                                  env=os.environ.copy(), stderr=subprocess.PIPE) for w in range(workers)]
        for proc in procs:
            _, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err.decode())
        topology = TopologyStore(self.path).current()
        self.assertEqual(len(topology.ips), 2 + workers * 5)       # 原有 2 个 + 新增 30 个
        self.assertEqual(len(topology.edges), 2 + workers * 5)
        self.assertEqual(len(set(topology.ips)), len(topology.ips))

    def test_crash_before_replace_leaves_the_file_intact(self):
        before = self.path.read_bytes()
        script = textwrap.dedent("""
            import os, sys
            from a2a import TopologyStore
            store = TopologyStore(sys.argv[1])
            real = os.replace
            calls = []
            def dying_replace(src, dst):
                calls.append(dst)
                if str(dst) == sys.argv[1]:   # 写目标文件的那一次替换之前直接崩溃(备份文件的替换放行)
                    os._exit(137)
                return real(src, dst)
            os.replace = dying_replace
            store.add_ip("crashy")
        """)
        proc = subprocess.run([sys.executable, "-c", script, str(self.path)], env=os.environ.copy())
        self.assertEqual(proc.returncode, 137)
        self.assertEqual(self.path.read_bytes(), before)           # 原文件没有被改动或写坏
        self.assertNotIn("crashy", TopologyStore(self.path).current().ips)
        TopologyStore(self.path).add_ip("after_crash")              # 之后仍能正常修改(遗留的临时文件无害)
        self.assertIn("after_crash", TopologyStore(self.path).current().ips)


if __name__ == "__main__":
    unittest.main()
