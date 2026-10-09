"""身份测试:AgentIdentity 与 resolve_sender 的每一个失败分支。"""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from a2a import (
    AgentIdentity,
    IdentityError,
    IdentityMismatchError,
    NodeNotInTopologyError,
    Registry,
    SenderNotRegisteredError,
    SenderNotRunningError,
    Topology,
    identity_env,
    resolve_sender,
)

CONFIG = {
    "version": 1,
    "project_id": "soc_a",
    "roles": {"dv": {}, "sw": {}},
    "ips": ["uart", "gpio"],
    "edges": [{"id": "dv_done", "from": "dv", "to": "sw", "template": "{ip} done"}],
}
SESSION = "walk1"


def make_env(**changes):
    env = {"A2A_PROJECT_ID": "soc_a", "A2A_ROLE": "dv", "A2A_IP": "uart", "HERDR_PANE_ID": "w1:p2"}
    env.update(changes)
    return {k: v for k, v in env.items() if v is not None}


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.topology = Topology.from_dict(CONFIG)

    def test_create_and_env(self):
        identity = AgentIdentity.create(self.topology, "dv", "uart")
        self.assertEqual(identity.agent_id, "dv_uart")
        self.assertEqual(identity.env, {"A2A_PROJECT_ID": "soc_a", "A2A_ROLE": "dv", "A2A_IP": "uart"})
        self.assertEqual(identity_env("soc_a", "dv", "uart"), identity.env)

    def test_agent_id_has_no_project_prefix(self):
        self.assertEqual(AgentIdentity.create(self.topology, "sw", "gpio").agent_id, "sw_gpio")

    def test_agent_id_must_be_role_ip_and_short(self):
        with self.assertRaises(IdentityError):
            AgentIdentity("soc_a", "uart", "dv", "soc_a_dv_uart")
        with self.assertRaises(IdentityError):
            AgentIdentity("soc_a", "x" * 31, "dv", "dv_" + "x" * 31)  # 超过 32 字符
        with self.assertRaises(IdentityError):
            AgentIdentity("SOC", "uart", "dv", "dv_uart")

    def test_create_rejects_nodes_not_in_topology(self):
        with self.assertRaises(IdentityError):
            AgentIdentity.create(self.topology, "ghost", "uart")
        with self.assertRaises(IdentityError):
            AgentIdentity.create(self.topology, "dv", "spi")

    def test_from_environment(self):
        env = make_env()
        self.assertEqual(AgentIdentity.from_environment(env, self.topology).agent_id, "dv_uart")
        with self.assertRaises(IdentityMismatchError):
            AgentIdentity.from_environment(make_env(A2A_PROJECT_ID="other"), self.topology)
        with self.assertRaises(IdentityError):
            AgentIdentity.from_environment({"A2A_ROLE": "dv"}, self.topology)


class ResolveSenderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.registry = Registry(Path(self.temp.name) / "registry.json")
        self.topology = Topology.from_dict(CONFIG)
        self.identity = AgentIdentity.create(self.topology, "dv", "uart")
        self.registry.register(self.identity, session=SESSION, workspace_id="w1", tab_id="w1:t2",
                               pane_id="w1:p2", status="idle")

    def resolve(self, env=None, topology=None, session=SESSION):
        return resolve_sender(env if env is not None else make_env(), self.registry,
                              topology or self.topology, session=session)

    def test_happy_path(self):
        record = self.resolve()
        self.assertEqual(record.agent_id, "dv_uart")
        self.assertEqual(record.pane_id, "w1:p2")

    # ---- 环境变量缺失 ----------------------------------------------
    def test_each_missing_variable_is_rejected(self):
        for name in ("HERDR_PANE_ID", "A2A_PROJECT_ID", "A2A_ROLE", "A2A_IP"):
            with self.subTest(missing=name), self.assertRaises(IdentityError) as ctx:
                self.resolve(make_env(**{name: None}))
            self.assertIn(name, str(ctx.exception))

    def test_empty_values_count_as_missing(self):
        with self.assertRaises(IdentityError):
            self.resolve(make_env(HERDR_PANE_ID=""))

    # ---- 对不上 ----------------------------------------------------
    def test_wrong_project_is_rejected(self):
        with self.assertRaises(IdentityMismatchError):
            self.resolve(make_env(A2A_PROJECT_ID="other"))

    def test_spoofed_role_or_ip_is_rejected(self):
        with self.assertRaises(IdentityMismatchError):
            self.resolve(make_env(A2A_ROLE="sw"))
        with self.assertRaises(IdentityMismatchError):
            self.resolve(make_env(A2A_IP="gpio"))

    def test_pane_belonging_to_someone_else_is_rejected(self):
        # 环境变量声称是 sw_uart,但 w1:p2 登记的是 dv_uart
        with self.assertRaises(IdentityMismatchError):
            self.resolve(make_env(A2A_ROLE="sw"))

    # ---- 未登记 / 已注销 --------------------------------------------
    def test_unregistered_pane_is_rejected(self):
        with self.assertRaises(SenderNotRegisteredError):
            self.resolve(make_env(HERDR_PANE_ID="w9:p9"))

    def test_unregistered_after_purge_is_rejected(self):
        self.registry.unregister("dv_uart")
        with self.assertRaises(SenderNotRegisteredError):
            self.resolve()

    def test_stale_pane_id_after_rebuild_is_rejected(self):
        self.registry.update_runtime("dv_uart", pane_id="w1:p9")
        with self.assertRaises(SenderNotRegisteredError):
            self.resolve()                                  # 旧 pane 号不再有效
        self.assertEqual(self.resolve(make_env(HERDR_PANE_ID="w1:p9")).agent_id, "dv_uart")

    # ---- 会话 ------------------------------------------------------
    def test_session_is_required_and_scoped(self):
        for bad in (None, ""):
            with self.assertRaises(ValueError):
                self.resolve(session=bad)
        with self.assertRaises(SenderNotRegisteredError):
            self.resolve(session="another_session")        # 别的会话里同号的 pane 不能冒充

    def test_ambiguous_pane_without_matching_session_is_not_resolved(self):
        sw = AgentIdentity.create(self.topology, "sw", "uart")
        self.registry.register(sw, session="other", workspace_id="w1", tab_id="w1:t2", pane_id="w1:p2")
        # 同一个 w1:p2 在两个会话里;指定会话时各自正确,不会串
        self.assertEqual(self.resolve().agent_id, "dv_uart")
        self.assertEqual(self.resolve(make_env(A2A_ROLE="sw"), session="other").agent_id, "sw_uart")

    # ---- 生命周期 / 拓扑 -------------------------------------------
    def test_non_running_agent_cannot_send(self):
        for lifecycle in ("stopped", "closed", "failed"):
            self.registry.update_runtime("dv_uart", lifecycle=lifecycle)
            with self.subTest(lifecycle=lifecycle), self.assertRaises(SenderNotRunningError):
                self.resolve()

    def test_node_removed_from_topology_cannot_send(self):
        data = copy.deepcopy(CONFIG)
        data["ips"] = ["gpio"]                              # uart 已从拓扑删除,注册表里还有旧记录
        with self.assertRaises(NodeNotInTopologyError):
            self.resolve(topology=Topology.from_dict(data))
        data = copy.deepcopy(CONFIG)
        data["roles"] = {"sw": {}}
        data["edges"] = []
        with self.assertRaises(NodeNotInTopologyError):
            self.resolve(topology=Topology.from_dict(data))

    def test_all_failures_are_identity_errors(self):
        for exc in (IdentityMismatchError, SenderNotRegisteredError, SenderNotRunningError,
                    NodeNotInTopologyError):
            self.assertTrue(issubclass(exc, IdentityError))


if __name__ == "__main__":
    unittest.main()
