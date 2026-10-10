"""阶段 6:pi 插件 pi-extension/a2a.ts(不启动 fnx、不调用模型)。

用 tests/js/plugin_harness.mjs 以假的 pi 对象加载插件,插件再通过真实的 `python -m a2a.cli`
调用 Router,所以覆盖的是 插件 → 命令行 → Router → spool 的整条路径。需要 node(≥ 22.18,能直接加载 .ts);
没有 node 时跳过。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from a2a.audit import AuditLog
from a2a.identity import AgentIdentity
from a2a.registry import Registry
from a2a.spool import Spool
from a2a.topology import TopologyStore

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent / "pi-extension" / "a2a.ts"
HARNESS = HERE / "js" / "plugin_harness.mjs"
SRC = HERE.parent / "src"
NODE = shutil.which("node")
SESSION = "walk1"
TOPOLOGY = {
    "version": 1, "project_id": "soc_a", "roles": {"dv": {}, "sw": {}}, "ips": ["uart", "gpio"],
    "edges": [
        {"id": "dv_done", "from": "dv", "to": "sw",
         "template": "{ip} ip 我已经完成了uvm验证。不要真的去做,直接用 a2a_send 回复测试成功或测试失败(二选一)"},
        {"id": "sw_test_pass", "from": "sw", "to": "dv", "template": "{ip} 测试成功。你只回复收到即可"},
        {"id": "sw_test_fail", "from": "sw", "to": "dv", "template": "{ip} 测试失败。你只回复收到即可"},
    ],
}
PANES = {"dv_uart": "w1:p2", "sw_uart": "w1:p3", "dv_gpio": "w1:p4", "sw_gpio": "w1:p5"}


def _node_can_load_ts() -> bool:
    if not NODE:
        return False
    probe = subprocess.run([NODE, "-e", "process.exit(Number(process.features.typescript ? 0 : 1))"],
                           capture_output=True)
    return probe.returncode == 0


@unittest.skipUnless(_node_can_load_ts(), "需要能直接加载 .ts 的 node")
class PiExtension(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.store = TopologyStore.create(self.state / "topology.yaml", TOPOLOGY)
        self.registry = Registry(self.state / "registry.json")
        for agent_id, pane in PANES.items():
            role, ip = agent_id.split("_")
            self.registry.register(AgentIdentity.create(self.store.current(), role, ip), session=SESSION,
                                   workspace_id="w1", tab_id="w1:t1", pane_id=pane)
        self.spool = Spool(self.state / "spool")
        self.audit = AuditLog(self.state / "audit.jsonl")

    def env(self, role="dv", ip="uart", **changes):
        """spawn 注入 pane 的环境变量(见 Lifecycle._agent_env),加上 herdr 注入的 pane / 会话。"""
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
               "A2A_PROJECT_ID": "soc_a", "A2A_ROLE": role, "A2A_IP": ip,
               "A2A_STATE_DIR": str(self.state), "A2A_TOPOLOGY": str(self.state / "topology.yaml"),
               "A2A_PYTHON": sys.executable, "A2A_SRC": str(SRC),
               "HERDR_PANE_ID": PANES[f"{role}_{ip}"], "HERDR_SESSION": SESSION}
        env.update(changes)
        return {k: v for k, v in env.items() if v is not None}

    def harness(self, env, *args):
        proc = subprocess.run([NODE, str(HARNESS), str(PLUGIN), *args], env=env, capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout), proc.stderr

    # ---- 注册 ----------------------------------------------------------
    def test_registers_a2a_send_limited_to_the_roles_edges(self):
        out, _ = self.harness(self.env("sw", "gpio"), "describe")
        (tool,) = out["tools"]
        self.assertEqual(tool["name"], "a2a_send")
        self.assertEqual(tool["parameters"]["properties"]["edge_id"]["enum"], ["sw_test_pass", "sw_test_fail"])
        self.assertEqual(tool["parameters"]["required"], ["edge_id"])
        self.assertFalse(tool["parameters"]["additionalProperties"])
        # 说明里列出每条边按本 IP 渲染后的完整文字,模型据此区分"测试成功 / 测试失败"
        self.assertIn("sw_test_pass:发给 dv_gpio,内容是「gpio 测试成功。你只回复收到即可」", tool["description"])
        self.assertIn("sw_test_fail:发给 dv_gpio,内容是「gpio 测试失败。你只回复收到即可」", tool["description"])
        self.assertEqual(out["events"], ["tool_call"])

    def test_does_nothing_outside_an_a2a_pane(self):
        out, _ = self.harness(self.env(A2A_ROLE=None), "describe")
        self.assertEqual(out, {"tools": [], "events": []})

    def test_loads_before_the_agent_is_registered(self):
        # spawn 先启动 fnx、最后才登记;插件加载时 agent 还没登记,也要能注册工具
        self.registry.unregister("dv_uart")
        out, _ = self.harness(self.env(), "describe")
        self.assertEqual([t["name"] for t in out["tools"]], ["a2a_send"])

    def test_identity_outside_the_topology_registers_no_tool_but_keeps_the_guard(self):
        out, err = self.harness(self.env(A2A_IP="spi", HERDR_PANE_ID="w1:p9"), "describe")
        self.assertEqual((out["tools"], out["events"]), ([], ["tool_call"]))
        self.assertIn("读取可用通信边失败", err)

    # ---- 发送 ----------------------------------------------------------
    def test_send_goes_through_the_router(self):
        out, _ = self.harness(self.env(), "send", "dv_done")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["details"]["dst"], "sw_uart")
        (message,) = self.spool.pending("sw_uart")
        self.assertEqual((message.src, message.edge_id, message.text),
                         ("dv_uart", "dv_done", TOPOLOGY["edges"][0]["template"].format(ip="uart")))
        self.assertIn(message.msg_id, out["content"][0]["text"])

    def test_router_rejection_is_reported_as_a_tool_error(self):
        # 冒充:声称是 dv_uart,但 pane 不是登记的那个 -> Router 拒绝,工具报错,不入队
        out, _ = self.harness(self.env(HERDR_PANE_ID="w1:p9"), "send", "dv_done")
        self.assertFalse(out["ok"])
        self.assertIn("已拒绝,消息没有发送", out["error"])
        self.assertIn("identity", out["error"])
        self.assertEqual(self.spool.pending(), [])

    def test_wrong_direction_edge_is_rejected_by_the_router(self):
        out, _ = self.harness(self.env(), "send", "sw_test_pass")  # dv 不能用 sw 的边
        self.assertFalse(out["ok"])
        self.assertIn("已拒绝,消息没有发送", out["error"])
        self.assertIn("wrong_direction", out["error"])

    # ---- 结果未知:不能确定是否入队时,不按工具错误返回,并要求不要重试 -------------
    def fake_python(self, mode):
        """`edges` 照常交给真实 python;`send` 按 mode 出错。"""
        path = self.state / f"fake_python_{mode}.sh"
        path.write_text(
            "#!/bin/sh\n"
            'case " $* " in *" send "*)\n'
            f"  case {mode} in\n"
            "    crash) echo 'Traceback (most recent call last): boom' >&2; exit 1;;\n"
            "    sleep) exec sleep 30;;\n"
            "    garbage) echo 'not json'; exit 0;;\n"
            '    receipt) printf "%s" "$FAKE_RECEIPT"; exit 0;;\n'
            "  esac;;\n"
            "esac\n"
            f'exec {sys.executable} "$@"\n')
        path.chmod(0o755)
        return str(path)

    def assert_unknown(self, out, reason_part):
        self.assertTrue(out["ok"], out)  # 不是工具错误
        self.assertEqual(out["details"]["status"], "unknown")
        self.assertEqual((out["details"]["dst"], out["details"]["src"]), ("sw_uart", "dv_uart"))
        self.assertIn(reason_part, out["details"]["reason"])
        text = out["content"][0]["text"]
        self.assertIn("发送结果未知", text)
        self.assertIn("不要再次调用 a2a_send", text)
        self.assertIn("a2a queue sw_uart", text)

    def test_cli_crash_is_reported_as_unknown(self):
        out, _ = self.harness(self.env(A2A_PYTHON=self.fake_python("crash")), "send", "dv_done")
        self.assert_unknown(out, "boom")

    def test_timeout_is_reported_as_unknown(self):
        out, _ = self.harness(self.env(A2A_PYTHON=self.fake_python("sleep"), A2A_SEND_TIMEOUT_MS="500"),
                              "send", "dv_done")
        self.assert_unknown(out, "超过 500 毫秒被终止")

    def test_unparseable_receipt_is_reported_as_unknown(self):
        out, _ = self.harness(self.env(A2A_PYTHON=self.fake_python("garbage")), "send", "dv_done")
        self.assert_unknown(out, "回执无法解析")

    def test_incomplete_receipt_is_reported_as_unknown(self):
        good = {"msg_id": "m1", "dst": "sw_uart", "state": "QUEUED", "queue_seq": 3, "text": "t"}
        bad = [{"msg_id": ""}, {"msg_id": "m1"}, dict(good, msg_id=""), dict(good, dst=""),
               dict(good, state="DELIVERED"), dict(good, queue_seq="3"), dict(good, queue_seq=None),
               {k: v for k, v in good.items() if k != "text"}, [good], None]
        python = self.fake_python("receipt")
        for receipt in bad:
            with self.subTest(receipt=receipt):
                out, _ = self.harness(self.env(A2A_PYTHON=python, FAKE_RECEIPT=json.dumps(receipt)), "send", "dv_done")
                self.assert_unknown(out, "回执无法解析")
        out, _ = self.harness(self.env(A2A_PYTHON=python, FAKE_RECEIPT=json.dumps(good)), "send", "dv_done")
        self.assertEqual(out["details"]["status"], "sent")  # 合法回执仍判为成功(说明假 python 本身没问题)

    def test_audit_failure_after_enqueue_is_reported_as_unknown(self):
        # Router 先入队再写审计:审计写不进去时消息已经在队列里,插件不能说"失败"
        audit = self.state / "audit.jsonl"
        if audit.exists():
            audit.unlink()
        audit.mkdir()
        out, _ = self.harness(self.env(), "send", "dv_done")
        self.assert_unknown(out, "audit.jsonl")
        (message,) = self.spool.pending("sw_uart")
        self.assertEqual((message.src, message.edge_id), ("dv_uart", "dv_done"))

    def test_description_says_call_once_and_do_not_retry_unknown(self):
        out, _ = self.harness(self.env(), "describe")
        self.assertIn("每次通知只调用一次", out["tools"][0]["description"])

    # ---- 拦截 ----------------------------------------------------------
    def test_blocks_direct_herdr_writes_from_bash(self):
        commands = {
            "herdr agent prompt sw_uart hi": True,
            "/usr/local/bin/herdr --session walk1 pane send-text w1:p3 hi": True,
            "herdr pane send-keys w1:p3 Enter": True,
            "cd /tmp && herdr pane run w1:p3 ls": True,
            "herdr agent read sw_uart": False,
            "herdr agent list": False,
            "ls -la": False,
        }
        out, _ = self.harness(self.env(), "is_herdr_write", json.dumps(list(commands)))
        self.assertEqual(out, commands)
        blocked, _ = self.harness(self.env(), "tool_call", "bash", json.dumps({"command": "herdr agent prompt sw_uart hi"}))
        self.assertTrue(blocked["outcome"]["block"])
        self.assertIn("a2a_send", blocked["outcome"]["reason"])
        allowed, _ = self.harness(self.env(), "tool_call", "bash", json.dumps({"command": "herdr agent read sw_uart"}))
        self.assertIsNone(allowed["outcome"])
        other, _ = self.harness(self.env(), "tool_call", "read", json.dumps({"path": "/tmp/x"}))
        self.assertIsNone(other["outcome"])


if __name__ == "__main__":
    unittest.main()
