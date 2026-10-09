"""Router 测试:正向流程、全部反向用例、动态拓扑、FIFO、多进程并发。"""
from __future__ import annotations

import copy
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import yaml

from a2a import (
    AgentIdentity,
    AuditLog,
    InvalidIdError,
    MessageNotFoundError,
    Registry,
    Router,
    SendRejected,
    Spool,
    Topology,
    TopologyStore,
    session_from_env,
)
from a2a.messages import QUEUED

SESSION = "walk1"
CONFIG = {
    "version": 1,
    "project_id": "soc_a",
    "roles": {"dv": {}, "sw": {}, "rtl": {}},
    "ips": ["uart", "gpio"],
    "edges": [
        {"id": "dv_done", "from": "dv", "to": "sw", "template": "{ip}已完成UVM验证,请开发驱动。"},
        {"id": "dv_rtl_fix", "from": "dv", "to": "rtl", "template": "{ip}验证发现RTL问题。"},
    ],
}
PANES = {"dv_uart": "w1:p2", "sw_uart": "w1:p3", "rtl_uart": "w1:p4",
         "dv_gpio": "w1:p5", "sw_gpio": "w1:p6", "rtl_gpio": "w1:p7"}


def env_of(role: str, ip: str, **changes):
    env = {"A2A_PROJECT_ID": "soc_a", "A2A_ROLE": role, "A2A_IP": ip, "HERDR_PANE_ID": PANES[f"{role}_{ip}"]}
    env.update(changes)
    return {k: v for k, v in env.items() if v is not None}


class RouterBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.topo_path = root / "topology.yaml"
        self.store = TopologyStore.create(self.topo_path, CONFIG)
        self.registry = Registry(root / "registry.json")
        self.spool = Spool(root / "spool")
        self.audit = AuditLog(root / "audit.jsonl")
        topology = self.store.current()
        for agent_id, pane in PANES.items():
            role, ip = agent_id.split("_")
            self.registry.register(AgentIdentity.create(topology, role, ip), session=SESSION,
                                   workspace_id="w1", tab_id="w1:t2", pane_id=pane, status="idle")
        self.router = Router(self.store, self.registry, self.spool, self.audit, session=SESSION)

    def assertRejected(self, code, edge_id, env, *, router=None):
        with self.assertRaises(SendRejected) as ctx:
            (router or self.router).send(edge_id, env)
        self.assertEqual(ctx.exception.code, code, ctx.exception)
        events = [e for e in self.audit.read() if e.get("msg_id") == ctx.exception.msg_id]
        self.assertEqual(len(events), 1, "每次拒绝必须在审计日志里留下恰好一条记录")
        self.assertEqual(events[0]["state"], "REJECTED")
        self.assertEqual(events[0]["reject_code"], code)
        return ctx.exception, events[0]

    def queued(self):
        return self.spool.pending()


class TestAccepted(RouterBase):
    def test_happy_path(self):
        receipt = self.router.send("dv_done", env_of("dv", "uart"))
        self.assertEqual((receipt.src, receipt.dst, receipt.state), ("dv_uart", "sw_uart", QUEUED))
        self.assertEqual(receipt.text, "uart已完成UVM验证,请开发驱动。")
        self.assertEqual(receipt.topology_revision, self.store.revision)
        pending = self.spool.pending("sw_uart")
        self.assertEqual([m.msg_id for m in pending], [receipt.msg_id])
        message = pending[0]
        self.assertEqual((message.src, message.dst, message.ip_id, message.session), ("dv_uart", "sw_uart", "uart", SESSION))
        self.assertEqual(self.router.status(receipt.msg_id).state, QUEUED)

    def test_acceptance_is_audited(self):
        receipt = self.router.send("dv_done", env_of("dv", "uart"))
        events = [e for e in self.audit.read() if e["msg_id"] == receipt.msg_id]
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["state"], events[0]["src"], events[0]["dst"]), (QUEUED, "dv_uart", "sw_uart"))
        self.assertEqual(events[0]["topology_revision"], self.store.revision)

    def test_target_ip_always_equals_sender_ip(self):
        a = self.router.send("dv_done", env_of("dv", "uart"))
        b = self.router.send("dv_done", env_of("dv", "gpio"))
        self.assertEqual((a.dst, b.dst), ("sw_uart", "sw_gpio"))
        self.assertEqual([m.dst for m in self.spool.pending("sw_uart")], ["sw_uart"])
        self.assertEqual([m.dst for m in self.spool.pending("sw_gpio")], ["sw_gpio"])

    def test_one_sender_can_use_multiple_edges(self):
        self.router.send("dv_done", env_of("dv", "uart"))
        self.router.send("dv_rtl_fix", env_of("dv", "uart"))
        self.assertEqual(self.spool.pending_targets(), ["rtl_uart", "sw_uart"])

    def test_fifo_order_per_target(self):
        receipts = [self.router.send("dv_done", env_of("dv", "uart")) for _ in range(5)]
        ids = [r.msg_id for r in receipts]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual([m.msg_id for m in self.spool.pending("sw_uart")], ids)

    def test_status_of_unknown_message(self):
        with self.assertRaises(MessageNotFoundError):
            self.router.status("0000000000000000-000000")      # 格式正确但不存在
        with self.assertRaises(InvalidIdError):
            self.router.status("nope")                          # 格式不对(msg_id 来自 agent 的命令行参数,必须校验)

    def test_works_with_a_static_topology_object(self):
        router = Router(Topology.from_dict(CONFIG), self.registry, self.spool, self.audit, session=SESSION)
        receipt = router.send("dv_done", env_of("dv", "uart"))
        self.assertEqual(receipt.topology_revision, "static")


class TestRejectedIdentity(RouterBase):
    def test_missing_environment_variables(self):
        for name in ("HERDR_PANE_ID", "A2A_PROJECT_ID", "A2A_ROLE", "A2A_IP"):
            with self.subTest(missing=name):
                self.assertRejected("identity", "dv_done", env_of("dv", "uart", **{name: None}))
        self.assertEqual(self.queued(), [])

    def test_spoofed_ip_role_or_project(self):
        self.assertRejected("identity", "dv_done", env_of("dv", "uart", A2A_IP="gpio"))
        self.assertRejected("identity", "dv_done", env_of("dv", "uart", A2A_ROLE="sw"))
        self.assertRejected("identity", "dv_done", env_of("dv", "uart", A2A_PROJECT_ID="other"))
        self.assertEqual(self.queued(), [])

    def test_someone_elses_pane(self):
        # 环境声称是 dv_gpio,但用的是 dv_uart 的 pane
        self.assertRejected("identity", "dv_done", env_of("dv", "gpio", HERDR_PANE_ID=PANES["dv_uart"]))
        self.assertEqual(self.queued(), [])

    def test_unregistered_and_stale_pane(self):
        self.assertRejected("identity", "dv_done", env_of("dv", "uart", HERDR_PANE_ID="w9:p9"))
        self.registry.unregister("dv_uart")
        self.assertRejected("identity", "dv_done", env_of("dv", "uart"))
        self.assertEqual(self.queued(), [])

    def test_other_session_cannot_impersonate(self):
        other = Router(self.store, self.registry, self.spool, self.audit, session="another_session")
        self.assertRejected("identity", "dv_done", env_of("dv", "uart"), router=other)

    def test_sender_not_running(self):
        self.registry.update_runtime("dv_uart", lifecycle="stopped")
        self.assertRejected("identity", "dv_done", env_of("dv", "uart"))

    def test_sender_removed_from_topology(self):
        self.store.remove_ip("uart")
        self.assertRejected("identity", "dv_done", env_of("dv", "uart"))

    def test_unverified_identity_is_logged_as_claimed_not_as_src(self):
        _, event = self.assertRejected("identity", "dv_done", env_of("dv", "uart", A2A_IP="gpio"))
        self.assertIsNone(event["src"])
        self.assertEqual(event["claimed_ip"], "gpio")
        self.assertEqual(event["claimed_pane"], PANES["dv_uart"])


class TestRejectedTopology(RouterBase):
    def test_unknown_edge(self):
        for edge in ("nope", "", "DV_DONE", "dv_done "):
            self.assertRejected("unknown_edge", edge, env_of("dv", "uart"))
        self.assertEqual(self.queued(), [])

    def test_wrong_direction(self):
        # 只配了 dv->sw,sw 不能借用 dv_done
        self.assertRejected("wrong_direction", "dv_done", env_of("sw", "uart"))
        self.assertRejected("wrong_direction", "dv_done", env_of("rtl", "uart"))
        self.assertEqual(self.queued(), [])

    def test_reverse_edge_must_be_configured_separately(self):
        self.assertRejected("unknown_edge", "sw_ack", env_of("sw", "uart"))           # 没配
        self.store.add_edge("sw_ack", "sw", "dv", "{ip}驱动开发完成。")
        receipt = self.router.send("sw_ack", env_of("sw", "uart"))                      # 配了才通
        self.assertEqual(receipt.dst, "dv_uart")
        self.assertRejected("wrong_direction", "sw_ack", env_of("dv", "uart"))         # 反方向仍然不通

    def test_target_not_in_topology_defence(self):
        # 按拓扑校验的规则,发送方存在则目标角色必然存在,这条是纵深防御;用打桩强行触发
        original = Topology.has_node

        def fake(self_, role, ip):
            return False if role == "sw" else original(self_, role, ip)

        with mock.patch.object(Topology, "has_node", fake):
            with self.assertRaises(SendRejected) as ctx:
                self.router.send("dv_done", env_of("dv", "uart"))
        self.assertEqual(ctx.exception.code, "target_not_in_topology")
        self.assertEqual(self.queued(), [])


class TestRejectedTarget(RouterBase):
    def test_cross_ip_is_impossible_by_construction(self):
        # sw_uart 不存在,但 sw_gpio 存在:dv_uart 发消息绝不会落到 sw_gpio
        self.registry.unregister("sw_uart")
        exc, event = self.assertRejected("target_missing", "dv_done", env_of("dv", "uart"))
        self.assertEqual(event["dst"], "sw_uart")
        self.assertEqual(self.queued(), [])
        self.assertEqual(self.spool.pending("sw_gpio"), [])

    def test_target_registered_in_another_session_is_refused(self):
        # 目标登记在另一个 herdr 会话里:broker 只在一个会话内工作,投递不到那里。要在入队之前就拒绝
        self.registry.update_runtime("sw_uart", session="OTHER")
        _, event = self.assertRejected("target_missing", "dv_done", env_of("dv", "uart"))
        self.assertIn("OTHER", event["detail"])
        self.assertEqual(self.queued(), [])

    def test_target_not_running(self):
        for lifecycle in ("stopped", "closed", "failed"):
            self.registry.update_runtime("sw_uart", lifecycle=lifecycle)
            self.assertRejected("target_not_running", "dv_done", env_of("dv", "uart"))
        self.assertEqual(self.queued(), [])

    def test_target_belonging_to_another_project_is_refused(self):
        # 注册表被破坏的防御:目标记录的 project_id 与发送方不同
        import json
        data = json.loads(self.registry.path.read_text(encoding="utf-8"))
        data["agents"]["sw_uart"]["project_id"] = "evil"
        self.registry.path.write_text(json.dumps(data), encoding="utf-8")
        self.assertRejected("target_missing", "dv_done", env_of("dv", "uart"))
        self.assertEqual(self.queued(), [])


class TestMessageText(RouterBase):
    def test_too_long(self):
        router = Router(self.store, self.registry, self.spool, self.audit, session=SESSION, max_chars=5)
        self.assertRejected("bad_message", "dv_done", env_of("dv", "uart"), router=router)

    def test_control_characters_rejected(self):
        self.store.set_template("dv_done", "{ip}完成\x07")
        self.assertRejected("bad_message", "dv_done", env_of("dv", "uart"))
        self.assertEqual(self.queued(), [])

    def test_newline_and_tab_allowed(self):
        self.store.set_template("dv_done", "{ip}完成\n请继续")
        self.assertIn("\n", self.router.send("dv_done", env_of("dv", "uart")).text)

    def test_sender_cannot_inject_text(self):
        # send() 没有 text / ip 参数;环境变量里即使塞了花括号,也只会被当作普通文字拒绝或忽略
        with self.assertRaises(TypeError):
            self.router.send("dv_done", env_of("dv", "uart"), text="任意文字")  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            self.router.send("dv_done", env_of("dv", "uart"), ip="gpio")  # type: ignore[call-arg]

    def test_literal_braces_in_template(self):
        self.store.set_template("dv_done", "{ip} {{ok}}")
        self.assertEqual(self.router.send("dv_done", env_of("dv", "uart")).text, "uart {ok}")


class TestDynamicTopology(RouterBase):
    def test_template_change_takes_effect_without_restart(self):
        first = self.router.send("dv_done", env_of("dv", "uart"))
        self.store.set_template("dv_done", "{ip}新句式")
        second = self.router.send("dv_done", env_of("dv", "uart"))
        self.assertEqual(second.text, "uart新句式")
        self.assertNotEqual(first.topology_revision, second.topology_revision)

    def test_hand_edit_is_picked_up(self):
        data = yaml.safe_load(self.topo_path.read_text(encoding="utf-8"))
        data["edges"].append({"id": "rtl_ready", "from": "rtl", "to": "dv", "template": "{ip}RTL就绪"})
        self.topo_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        self.assertEqual(self.router.send("rtl_ready", env_of("rtl", "uart")).dst, "dv_uart")

    def test_removed_edge_stops_working_immediately(self):
        self.router.send("dv_done", env_of("dv", "uart"))
        self.store.remove_edge("dv_done")
        self.assertRejected("unknown_edge", "dv_done", env_of("dv", "uart"))

    def test_broken_hand_edit_keeps_serving_the_last_good_topology(self):
        self.topo_path.write_text("version: 1\nproject_id: soc_a\nroles: {}\n", encoding="utf-8")
        receipt = self.router.send("dv_done", env_of("dv", "uart"))
        self.assertEqual(receipt.dst, "sw_uart")
        self.assertIsNotNone(self.store.last_error)

    def test_role_removal_with_cascade(self):
        self.store.remove_role("sw", cascade_edges=True)
        self.assertRejected("unknown_edge", "dv_done", env_of("dv", "uart"))


class TestSessionResolution(RouterBase):
    def test_session_from_env(self):
        self.assertEqual(session_from_env({"HERDR_SESSION": "walk1"}), "walk1")
        self.assertEqual(session_from_env({}), "default")
        self.assertEqual(session_from_env({"HERDR_SESSION": ""}), "default")

    def test_router_without_explicit_session_uses_pane_env(self):
        router = Router(self.store, self.registry, self.spool, self.audit)
        self.assertEqual(router.send("dv_done", env_of("dv", "uart", HERDR_SESSION=SESSION)).dst, "sw_uart")
        # 没有 HERDR_SESSION -> 按 default 处理,而这些 agent 登记在 walk1 -> 拒绝
        self.assertRejected("identity", "dv_done", env_of("dv", "uart"), router=router)

    def test_defaults_to_os_environ(self):
        with mock.patch.dict(os.environ, env_of("dv", "uart"), clear=False):
            self.assertEqual(self.router.send("dv_done").dst, "sw_uart")


class TestConcurrentSenders(RouterBase):
    def test_many_processes_sending_at_once(self):
        script = textwrap.dedent("""
            import os, sys
            from a2a import AuditLog, Registry, Router, Spool, TopologyStore
            root, worker = sys.argv[1], sys.argv[2]
            role, ip = worker.split("_")
            router = Router(TopologyStore(root + "/topology.yaml"), Registry(root + "/registry.json"),
                            Spool(root + "/spool"), AuditLog(root + "/audit.jsonl"), session="walk1")
            env = {"A2A_PROJECT_ID": "soc_a", "A2A_ROLE": role, "A2A_IP": ip, "HERDR_PANE_ID": sys.argv[3]}
            for _ in range(5):
                router.send("dv_done", env)
        """)
        senders = [("dv_uart", PANES["dv_uart"]), ("dv_gpio", PANES["dv_gpio"])]
        procs = []
        for _ in range(3):
            for agent_id, pane in senders:
                procs.append(subprocess.Popen([sys.executable, "-c", script, self.temp.name, agent_id, pane],
                                              env=os.environ.copy(), stderr=subprocess.PIPE))
        for proc in procs:
            _, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err.decode())
        self.assertEqual(len(self.spool.pending("sw_uart")), 15)
        self.assertEqual(len(self.spool.pending("sw_gpio")), 15)
        ids = [m.msg_id for m in self.spool.pending()]
        self.assertEqual(len(set(ids)), 30)
        entries = [e for e in self.audit.read() if e.get("state") == QUEUED]
        self.assertEqual(len(entries), 30)                      # 每行都是完整的 JSON,没有交织
        self.assertTrue(all(m.ip_id == m.dst.split("_")[1] == m.src.split("_")[1] for m in self.spool.pending()))


if __name__ == "__main__":
    unittest.main()
