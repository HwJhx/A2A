"""Pi extension unit tests: real Python CLI/Router, fake pi host, no Herdr/model."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

import yaml

from a2a_codex.audit import AuditLog
from a2a_codex.identity import AgentIdentity
from a2a_codex.registry import Registry
from a2a_codex.spool import Spool
from a2a_codex.topology import TopologyStore

ROOT = Path(__file__).resolve().parents[3]
PLUGIN = Path(__file__).resolve().parents[1] / "pi-extension" / "a2a.ts"
HARNESS = Path(__file__).resolve().parent / "js" / "plugin_harness.mjs"
NODE = shutil.which("node")


def _node_supports_ts() -> bool:
    if not NODE:
        return False
    probe = subprocess.run([NODE, "-e", "process.exit(Number(process.features.typescript ? 0 : 1))"],
                          capture_output=True)
    return probe.returncode == 0


@unittest.skipUnless(_node_supports_ts(), "需要支持直接加载 .ts 的 Node.js")
class TestPiExtension(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="a2a-codex-pi-")
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        config = {
            "version": 1, "project_id": "soc_a", "roles": {"dv": {}, "sw": {}}, "ips": ["uart"],
            "edges": [{"id": "dv_done", "from": "dv", "to": "sw",
                       "template": "{ip} 已假设完成 UVM；仅回复收到，不要实际开发。"}],
        }
        self.topology_path = self.state / "topology.yaml"
        self.topology_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
        self.store = TopologyStore(self.topology_path)
        registry = Registry(self.state / "registry.json")
        for role, pane in (("dv", "w1:p1"), ("sw", "w1:p2")):
            registry.register(AgentIdentity.create(self.store.current, role, "uart"), session="test",
                              workspace_id="w1", tab_id="w1:t1", pane_id=pane,
                              status="idle", lifecycle="running")
        self.spool = Spool(self.state / "spool")
        self.audit = AuditLog(self.state / "audit.jsonl")

    def env(self, **overrides):
        env = dict(os.environ)
        env.update({
            "A2A_PROJECT_ID": "soc_a", "A2A_ROLE": "dv", "A2A_IP": "uart",
            "A2A_STATE_DIR": str(self.state), "A2A_TOPOLOGY": str(self.topology_path),
            "A2A_PYTHON": sys.executable, "A2A_SRC": str(ROOT / "herdr"),
            "HERDR_PANE_ID": "w1:p1", "HERDR_SESSION": "test",
            "PYTHONPATH": str(ROOT / "herdr"), "PYTHONDONTWRITEBYTECODE": "1",
        })
        env.update(overrides)
        return env

    def run_plugin(self, action, *args, env=None):
        proc = subprocess.run([NODE, str(HARNESS), str(PLUGIN), action, *args],
                              env=env or self.env(), capture_output=True, text=True, timeout=45)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def fake_python(self):
        path = self.state / "fake-python"
        script = f"""#!{sys.executable}
import json
import os
import sys
import time

args = sys.argv[1:]
if args[:3] == ["-m", "a2a_codex.cli", "edges"]:
    os.execv({sys.executable!r}, [{sys.executable!r}, *args])
if len(args) >= 4 and args[:2] == ["-m", "a2a_codex.cli"] and args[2] == "send":
    mode = os.environ.get("A2A_TEST_SEND_MODE", "exit7")
    if mode == "exit7":
        print("simulated post-submit error", file=sys.stderr)
        raise SystemExit(7)
    if mode == "timeout":
        time.sleep(10)
        raise SystemExit(0)
    if mode == "non_json":
        print("not json")
        raise SystemExit(0)
    if mode == "receipt":
        print(os.environ["A2A_TEST_RECEIPT"])
        raise SystemExit(0)
raise SystemExit(9)
"""
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    def valid_receipt(self):
        return {
            "msg_id": "m-test-1", "edge_id": "dv_done", "src": "dv_uart", "dst": "sw_uart",
            "text": "uart 已假设完成 UVM；仅回复收到，不要实际开发。", "state": "QUEUED",
            "topology_revision": "test-revision",
        }

    def receipt_env(self, receipt):
        return self.env(A2A_PYTHON=self.fake_python(), A2A_TEST_SEND_MODE="receipt",
                        A2A_TEST_RECEIPT=json.dumps(receipt, ensure_ascii=False))

    def test_registers_only_fixed_source_edges(self):
        result = self.run_plugin("describe")
        (tool,) = result["tools"]
        self.assertEqual(tool["name"], "a2a_send")
        self.assertEqual(tool["parameters"]["properties"]["edge_id"]["enum"], ["dv_done"])
        self.assertIn("不要重试", tool["description"])

    def test_send_uses_router_template_and_registry_identity(self):
        result = self.run_plugin("send", "dv_done")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["details"]["status"], "sent")
        (message,) = self.spool.pending("sw_uart")
        self.assertEqual((message.src, message.dst, message.edge_id), ("dv_uart", "sw_uart", "dv_done"))
        self.assertEqual(message.text, "uart 已假设完成 UVM；仅回复收到，不要实际开发。")

    def test_identity_rejection_is_not_misreported_as_unknown_or_sent(self):
        result = self.run_plugin("send", "dv_done", env=self.env(HERDR_PANE_ID="w1:p9"))
        self.assertFalse(result["ok"])
        self.assertIn("已拒绝", result["error"])
        self.assertEqual(self.spool.pending(), [])

    def test_non_rejection_failure_is_normal_unknown_result_and_says_do_not_retry(self):
        env = self.env(A2A_PYTHON=self.fake_python(), A2A_TEST_SEND_MODE="exit7")
        result = self.run_plugin("send", "dv_done", env=env)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["details"]["status"], "unknown")
        self.assertIn("不要重试", result["content"][0]["text"])

    def test_python_start_failure_is_unknown(self):
        env = self.env(A2A_PYTHON=self.fake_python(), A2A_TEST_REMOVE_PYTHON_BEFORE_SEND="1")
        result = self.run_plugin("send", "dv_done", env=env)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["details"]["status"], "unknown")
        self.assertIn("不要重试", result["content"][0]["text"])

    def test_timeout_is_unknown_and_started_at_remains_the_pre_send_time(self):
        env = self.env(A2A_PYTHON=self.fake_python(), A2A_TEST_SEND_MODE="timeout",
                       A2A_SEND_TIMEOUT_MS="1200")
        before = time.time()
        result = self.run_plugin("send", "dv_done", env=env)
        finished = time.time()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["details"]["status"], "unknown")
        started = datetime.fromisoformat(result["details"]["started_at"].replace("Z", "+00:00")).timestamp()
        self.assertGreaterEqual(started, before - 0.5)
        self.assertLess(started, finished - 0.8)
        self.assertIn(result["details"]["started_at"], result["content"][0]["text"])

    def test_zero_exit_with_non_json_stdout_is_unknown(self):
        env = self.env(A2A_PYTHON=self.fake_python(), A2A_TEST_SEND_MODE="non_json")
        result = self.run_plugin("send", "dv_done", env=env)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["details"]["status"], "unknown")
        self.assertIn("不要重试", result["content"][0]["text"])

    def test_receipt_missing_required_fields_is_unknown(self):
        receipt = self.valid_receipt()
        for field in tuple(receipt):
            with self.subTest(missing=field):
                incomplete = dict(receipt)
                incomplete.pop(field)
                result = self.run_plugin("send", "dv_done", env=self.receipt_env(incomplete))
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["details"]["status"], "unknown")

    def test_receipt_with_wrong_edge_or_identity_is_unknown(self):
        mutations = {
            "edge_id": "other_edge",
            "src": "dv_spi",
            "dst": "sw_spi",
            "text": "free text",
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                receipt = self.valid_receipt()
                receipt[field] = value
                result = self.run_plugin("send", "dv_done", env=self.receipt_env(receipt))
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["details"]["status"], "unknown")

    def test_router_enqueue_followed_by_audit_failure_is_unknown(self):
        # Router persists the message before AuditLog.record(); a failure in that gap
        # must not look like a definite rejection because the message is already queued.
        (self.state / "audit.jsonl").mkdir()
        result = self.run_plugin("send", "dv_done")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["details"]["status"], "unknown")
        self.assertIn("不要重试", result["content"][0]["text"])
        (message,) = self.spool.pending("sw_uart")
        started = datetime.fromisoformat(result["details"]["started_at"].replace("Z", "+00:00"))
        created = datetime.fromisoformat(message.created_at.replace("Z", "+00:00"))
        self.assertLessEqual(started, created)
        self.assertIn(result["details"]["started_at"], result["content"][0]["text"])

    def test_direct_herdr_write_guard_is_active(self):
        result = self.run_plugin("guard", "herdr agent prompt w1:p2 hello")
        self.assertTrue(result["block"])


if __name__ == "__main__":
    unittest.main()
