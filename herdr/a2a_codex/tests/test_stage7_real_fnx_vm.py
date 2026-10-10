"""Opt-in stage-7 real fnx/model chain; never runs in the ordinary unit suite.

Explicit command (only in the isolated VM session):
  A2A_HERDR_INTEGRATION=1 A2A_STAGE7_MODEL=1 HERDR_TEST_SESSION=a2a_codex_verify \
    PYTHONPATH=herdr PYTHONDONTWRITEBYTECODE=1 \
    python3 -m unittest herdr.a2a_codex.tests.test_stage7_real_fnx_vm -v

The test installs a uniquely named extension temporarily and restores any prior file.
Both agents use --no-builtin-tools and disposable empty working directories. The
only message template is explicitly test-only and asks the receiver to say 收到.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

from a2a_codex import HerdrClient, build_launch_command
from a2a_codex.audit import AuditLog
from a2a_codex.identity import AgentIdentity
from a2a_codex.registry import Registry
from a2a_codex.spool import Spool
from a2a_codex.topology import Topology

ENABLED = os.environ.get("A2A_HERDR_INTEGRATION") == "1"
MODEL_ENABLED = os.environ.get("A2A_STAGE7_MODEL") == "1"
SESSION = os.environ.get("HERDR_TEST_SESSION")
ROOT = Path(__file__).resolve().parents[3]
EXTENSION_SOURCE = Path(__file__).resolve().parents[1] / "pi-extension" / "a2a.ts"
EXTENSION_NAME = "a2a_codex_stage7.ts"
TEST_TEMPLATE = (
    "{ip} IP：假设 UART UVM 验证已完成。此消息仅用于 A2A 链路测试；"
    "不要进行实际驱动/HAL 开发，不要读取或修改文件，只回复‘收到’。"
)


@unittest.skipUnless(ENABLED and MODEL_ENABLED and SESSION == "a2a_codex_verify",
                     "需显式启用 A2A_HERDR_INTEGRATION=1、A2A_STAGE7_MODEL=1，且指定 a2a_codex_verify")
class TestStage7RealFnx(unittest.TestCase):
    def setUp(self):
        self.dv_launcher = Path.home() / ".forenyx/fnx_dv/bin/fnx_dv"
        self.sw_launcher = Path.home() / ".forenyx/fnx_sw/bin/fnx_sw"
        if not self.dv_launcher.is_file() or not self.sw_launcher.is_file():
            self.skipTest("VM 中没有安装 fnx_dv / fnx_sw 启动器")

        self.temp = tempfile.TemporaryDirectory(prefix="a2a-codex-stage7-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.cwd_dv = self.root / "dv-work"
        self.cwd_sw = self.root / "sw-work"
        self.cwd_dv.mkdir()
        self.cwd_sw.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.topology_path = self.root / "topology.yaml"
        self.topology_path.write_text(
            "version: 1\nproject_id: soc_a\nworkspace_label: A2A stage 7 disposable test\n"
            "roles:\n  dv: {label: DV test, kind: pi, launcher: null}\n"
            "  sw: {label: SW test, kind: pi, launcher: null}\n"
            "ips: [uart]\nedges:\n"
            "  - id: dv_done\n    from: dv\n    to: sw\n"
            f"    template: {json.dumps(TEST_TEMPLATE, ensure_ascii=False)}\n",
            encoding="utf-8",
        )
        topology = Topology.load(self.topology_path)
        self.registry = Registry(self.state / "registry.json")
        self.spool = Spool(self.state / "spool")
        self.audit = AuditLog(self.state / "audit.jsonl")
        self.client = HerdrClient(SESSION)
        self._broker = None
        self._broker_log = None

        self._extension_backups = []
        self.addCleanup(self._restore_extensions)
        self._install_extension("fnx_dv")
        self._install_extension("fnx_sw")

        # Pi stores transcripts under a cwd-derived directory. Each test uses a
        # unique temporary cwd, so these directories must be new and isolated
        # from any other fnx sessions on the VM.
        self.session_dirs = {
            "dv": self._session_dir_for_cwd("fnx_dv", self.cwd_dv),
            "sw": self._session_dir_for_cwd("fnx_sw", self.cwd_sw),
        }
        for role, directory in self.session_dirs.items():
            self.assertFalse(directory.exists(),
                             f"{role} 临时 cwd 对应的 Pi session 目录应为本次新建: {directory}")
        self.session_files_before = {
            role: self._snapshot_session_files(directory)
            for role, directory in self.session_dirs.items()
        }

        self.common_env = {
            "A2A_PROJECT_ID": topology.project_id,
            "A2A_STATE_DIR": str(self.state),
            "A2A_TOPOLOGY": str(self.topology_path),
            "A2A_PYTHON": sys.executable,
            "A2A_SRC": str(ROOT / "herdr"),
            "HERDR_SESSION": SESSION,
        }
        dv_env = {**self.common_env, "A2A_ROLE": "dv", "A2A_IP": "uart"}
        sw_env = {**self.common_env, "A2A_ROLE": "sw", "A2A_IP": "uart"}
        self.workspace = self.client.create_workspace(
            label="a2a-s7-" + uuid4().hex[:8], cwd=str(self.cwd_dv), env=dv_env, focus=False)
        self.addCleanup(self._close_workspace)
        self.sw_tab = self.client.create_tab(
            workspace_id=self.workspace.workspace_id, label="a2a-s7-sw-" + uuid4().hex[:6],
            cwd=str(self.cwd_sw), env=sw_env, focus=False)
        self.assertTrue(self.workspace.pane_id and self.sw_tab.pane_id)
        self.assertNotEqual(self.workspace.workspace_id, "w1")
        self.assertNotEqual(self.workspace.pane_id, "w1:p2")

        # Herdr agent names are session-global; keep them unique so this test never
        # collides with a user's existing dv_uart/sw_uart panes. Business IDs remain stable.
        run_tag = uuid4().hex[:10]
        self.dv_name = "s7dv_" + run_tag
        self.sw_name = "s7sw_" + run_tag
        self.registry.register(
            AgentIdentity.create(topology, "dv", "uart"), session=SESSION,
            workspace_id=self.workspace.workspace_id, tab_id=self.workspace.tab_id,
            pane_id=self.workspace.pane_id, agent_name=self.dv_name,
            status="unknown", lifecycle="running")
        self.registry.register(
            AgentIdentity.create(topology, "sw", "uart"), session=SESSION,
            workspace_id=self.workspace.workspace_id, tab_id=self.sw_tab.tab_id,
            pane_id=self.sw_tab.pane_id, agent_name=self.sw_name,
            status="unknown", lifecycle="running")

        self.client.start_agent(self.workspace.pane_id,
                                build_launch_command(str(self.dv_launcher), ["--no-builtin-tools"]))
        self.client.start_agent(self.sw_tab.pane_id,
                                build_launch_command(str(self.sw_launcher), ["--no-builtin-tools"]))
        for pane_id, name in ((self.workspace.pane_id, self.dv_name), (self.sw_tab.pane_id, self.sw_name)):
            detected = self.client.wait_for_agent_detected(pane_id, timeout_s=30)
            self.assertEqual(detected.pane_id, pane_id)
            self.client.rename_agent(pane_id, name)
            ready = self.client.wait_agent(name, until=("idle", "done"), timeout_ms=60000)
            self.assertIn(ready.status, {"idle", "done"})

    def _install_extension(self, agent: str) -> None:
        destination_dir = Path.home() / ".forenyx" / agent / "agent" / "extensions"
        created_dirs = []
        cursor = destination_dir
        while not cursor.exists():
            created_dirs.append(cursor)
            cursor = cursor.parent
        destination_dir.mkdir(parents=True, exist_ok=True)
        path = destination_dir / EXTENSION_NAME
        if path.is_symlink():
            raise RuntimeError(f"拒绝覆盖符号链接插件: {path}")
        previous = path.read_bytes() if path.exists() else None
        mode = path.stat().st_mode & 0o777 if path.exists() else None
        self._extension_backups.append((path, previous, mode, created_dirs))
        shutil.copyfile(EXTENSION_SOURCE, path)

    def _restore_extensions(self) -> None:
        for path, previous, mode, created_dirs in reversed(self._extension_backups):
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(previous)
                path.chmod(mode)
            for directory in created_dirs:
                try:
                    directory.rmdir()
                except OSError:
                    pass

    def _close_workspace(self) -> None:
        if getattr(self, "workspace", None) and self.workspace.workspace_id:
            self.client.delete_workspace(self.workspace.workspace_id)

    @staticmethod
    def _session_dir_for_cwd(agent: str, cwd: Path) -> Path:
        encoded_cwd = str(cwd.resolve()).strip("/").replace("/", "-")
        return (Path.home() / ".forenyx" / agent / "agent" / "sessions"
                / f"--{encoded_cwd}--")

    @staticmethod
    def _snapshot_session_files(directory: Path) -> dict[Path, tuple[int, int]]:
        if not directory.is_dir():
            return {}
        return {
            path: (path.stat().st_size, path.stat().st_mtime_ns)
            for path in directory.rglob("*.jsonl") if path.is_file()
        }

    @staticmethod
    def _changed_session_records(directory: Path, before: dict[Path, tuple[int, int]]) -> list[dict]:
        records = []
        if not directory.is_dir():
            return records
        for path in directory.rglob("*.jsonl"):
            if not path.is_file():
                continue
            stat = path.stat()
            old_size, old_mtime = before.get(path, (-1, -1))
            if (stat.st_size, stat.st_mtime_ns) == (old_size, old_mtime):
                continue
            try:
                # Existing transcript files are append-only; inspect only bytes
                # added by this test so an older "收到" cannot satisfy the assertion.
                with path.open("rb") as stream:
                    stream.seek(max(old_size, 0))
                    lines = stream.read().decode("utf-8").splitlines()
            except (OSError, UnicodeError):
                continue
            for line in lines:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    records.append(value)
        return records

    @staticmethod
    def _assistant_messages(records: list[dict]) -> list[dict]:
        messages = []
        for record in records:
            message = record.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                messages.append(message)
            elif record.get("role") == "assistant":
                messages.append(record)
        return messages

    @staticmethod
    def _message_text(message: dict) -> str:
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                str(block.get("text", "")) for block in content
                if isinstance(block, dict) and block.get("type") in {"text", "output_text"}
            )
        return ""

    @staticmethod
    def _has_tool_call(message: dict, tool_name: str) -> bool:
        content = message.get("content", [])
        return isinstance(content, list) and any(
            isinstance(block, dict)
            and block.get("type") in {"toolCall", "tool_call"}
            and block.get("name") == tool_name
            for block in content
        )

    def _stop_broker(self) -> None:
        if self._broker is None:
            return
        if self._broker.poll() is None:
            self._broker.send_signal(signal.SIGINT)
            try:
                self._broker.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self._broker.terminate()
                self._broker.wait(timeout=10)
        if self._broker_log:
            self._broker_log.close()

    def test_real_dv_tool_call_delivers_fixed_test_message_to_sw(self):
        repo_env = dict(os.environ)
        repo_env.update({
            "PYTHONPATH": str(ROOT / "herdr"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "A2A_STATE_DIR": str(self.state),
            "A2A_TOPOLOGY": str(self.topology_path),
            "HERDR_SESSION": SESSION,
        })
        self._broker_log = (self.root / "broker.log").open("w", encoding="utf-8")
        self._broker = subprocess.Popen(
            [sys.executable, "-m", "a2a_codex.cli", "--session", SESSION,
             "--topology", str(self.topology_path), "broker", "run"],
            cwd=str(ROOT), env=repo_env, stdout=self._broker_log, stderr=subprocess.STDOUT,
        )
        self.addCleanup(self._stop_broker)
        lock = self.state / "spool" / "broker-runtime.lock"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and self._broker.poll() is None and not lock.exists():
            time.sleep(0.1)
        self.assertIsNone(self._broker.poll(), "Broker 过早退出: " + (self.root / "broker.log").read_text())
        self.assertTrue(lock.exists(), "Broker 未取得隔离状态目录的单实例锁")

        prompt = (
            "这是一个隔离的 A2A 链路测试。请假设你已完成 uart IP 的 UVM 验证，"
            "调用唯一可用工具 a2a_send，选择 dv_done，通知软件 agent。"
            "不要做任何实际验证或开发工作。"
        )
        self.client.send_prompt(self.dv_name, prompt, wait=True, timeout_ms=240000)

        expected_messages = self.spool.pending("sw_uart") + [
            message for message in self.spool.done() if message.dst == "sw_uart"
        ]
        self.assertEqual(len(expected_messages), 1,
                         "预期 sw_uart 只入队一次: %r" % expected_messages)
        message = expected_messages[0]
        self.assertEqual(message.edge_id, "dv_done")
        expected = TEST_TEMPLATE.format(ip="uart")
        self.assertEqual(message.text, expected)

        deadline = time.monotonic() + 90
        current = message
        while time.monotonic() < deadline:
            current = self.spool.get(message.msg_id)
            if current.state == "DELIVERED":
                break
            time.sleep(0.25)
        self.assertEqual(current.state, "DELIVERED", "消息未到 DELIVERED: %r" % current)

        target_before_wait = self.client.get_agent(self.sw_name)
        self.assertIn(target_before_wait.status, {"working", "idle", "done"})
        if target_before_wait.status == "working":
            completed = self.client.wait_agent(self.sw_name, until=("idle", "done"), timeout_ms=240000)
            self.assertIn(completed.status, {"idle", "done"})
        # TUI rendering may insert whitespace inside CJK text. The exact template
        # is asserted against the persisted Spool message above; use the Pi
        # transcript below as evidence of the receiver's generated response.
        dv_records = self._changed_session_records(
            self.session_dirs["dv"], self.session_files_before["dv"])
        sw_records = self._changed_session_records(
            self.session_dirs["sw"], self.session_files_before["sw"])
        dv_assistant = self._assistant_messages(dv_records)
        sw_assistant = self._assistant_messages(sw_records)
        self.assertTrue(any(self._has_tool_call(message, "a2a_send") for message in dv_assistant),
                        "DV Pi transcript 必须证明模型实际调用了 a2a_send")
        self.assertTrue(any("收到" in self._message_text(message) for message in sw_assistant),
                        "SW Pi transcript 必须有模板之后由 assistant 生成的收到回复")
        for role, assistant_messages in (("dv", dv_assistant), ("sw", sw_assistant)):
            model_messages = [message for message in assistant_messages
                              if message.get("provider") and message.get("model")]
            self.assertTrue(model_messages, f"{role} Pi transcript 应记录 provider/model")
        all_spool_messages = self.spool.pending() + self.spool.done()
        self.assertEqual(len(all_spool_messages), 1,
                         "整个 Spool 中应只有本次预期的一条 A2A 消息: %r" % all_spool_messages)
        self.assertEqual(list(self.cwd_dv.iterdir()), [], "DV 临时工作目录不应有文件")
        self.assertEqual(list(self.cwd_sw.iterdir()), [], "SW 临时工作目录不应有文件")


if __name__ == "__main__":
    unittest.main()
