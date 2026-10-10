"""Opt-in integration test against a real Herdr server, without model calls.

Run inside the Ubuntu VM after selecting an isolated, already-running Herdr
session (for example ``HERDR_TEST_SESSION=test1``):

    A2A_HERDR_INTEGRATION=1 HERDR_TEST_SESSION=test1 \\
      PYTHONPATH=herdr PYTHONDONTWRITEBYTECODE=1 \\
      python3 -m unittest discover -s herdr/a2a_codex/tests -p test_integration_herdr_vm.py -v

Each test creates and closes a uniquely named workspace in that named session.
It does not create/stop a Herdr session, touch existing workspaces, or invoke a model.
"""
from __future__ import annotations

import fcntl
import json
import os
import shlex
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

from a2a_codex import HerdrClient
from a2a_codex.audit import AuditLog
from a2a_codex.identity import AgentIdentity
from a2a_codex.messages import DELIVERY_UNCERTAIN, DELIVERED, DISPATCHING, QUEUED, Message, new_msg_id, now_iso
from a2a_codex.registry import Registry
from a2a_codex.spool import Spool
from a2a_codex.topology import Topology


ENABLED = os.environ.get("A2A_HERDR_INTEGRATION") == "1"
SESSION = os.environ.get("HERDR_TEST_SESSION")


@unittest.skipUnless(ENABLED and SESSION,
                     "设置 A2A_HERDR_INTEGRATION=1 和 HERDR_TEST_SESSION 指定隔离的运行中 session")
class TestHerdrIntegrationVM(unittest.TestCase):
    def setUp(self) -> None:
        self.client = HerdrClient(SESSION)
        self.cwd = tempfile.TemporaryDirectory(prefix="a2a-herdr-it-")
        self.addCleanup(self.cwd.cleanup)
        self.workspace = self.client.create_workspace(
            label="a2a-it-%s" % uuid4().hex[:10],
            cwd=str(Path(self.cwd.name).resolve()), focus=False,
        )
        self.addCleanup(self._close_workspace)

    def _close_workspace(self) -> None:
        if self.workspace.workspace_id:
            self.client.delete_workspace(self.workspace.workspace_id)

    def test_detect_rename_wait_prompt_and_read_without_model(self) -> None:
        pane_id = self.workspace.pane_id
        self.assertIsNotNone(pane_id)
        self.assertNotEqual(pane_id, "w1:p2")
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

    def test_cli_broker_delivers_fixed_edge_template_to_fake_pi_tty(self) -> None:
        workspace_id = self.workspace.workspace_id
        source_pane = self.workspace.pane_id
        self.assertTrue(workspace_id)
        self.assertTrue(source_pane)
        self.assertNotEqual(workspace_id, "w1")
        self.assertNotEqual(source_pane, "w1:p2")

        target_tab = self.client.create_tab(
            workspace_id=workspace_id,
            label="a2a-target-%s" % uuid4().hex[:8],
            cwd=str(Path(self.cwd.name).resolve()), focus=False,
        )
        target_pane = target_tab.pane_id
        self.assertTrue(target_pane)
        self.assertNotEqual(target_pane, "w1:p2")

        test_id = uuid4().hex[:10]
        state_dir = Path(self.cwd.name) / "state"
        topology_path = Path(self.cwd.name) / "topology.yaml"
        topology_path.write_text(
            "version: 1\n"
            "project_id: soc_a\n"
            "workspace_label: A2A integration test\n"
            "roles:\n"
            "  dv: {label: fake sender, kind: pi, launcher: null}\n"
            "  sw: {label: fake receiver, kind: pi, launcher: null}\n"
            "ips: [uart]\n"
            "edges:\n"
            "  - id: dv_done\n"
            "    from: dv\n"
            "    to: sw\n"
            "    template: \"{ip}已完成 UVM 验证，请开始驱动程序和 HAL 框架开发。\"\n",
            encoding="utf-8",
        )
        topology = Topology.load(topology_path)
        registry = Registry(path=state_dir / "registry.json")
        registry.register(
            AgentIdentity.create(topology, "dv", "uart"), session=SESSION,
            workspace_id=workspace_id, tab_id=self.workspace.tab_id,
            pane_id=source_pane, agent_name="a2a_src_%s" % test_id,
            status="idle", lifecycle="running",
        )
        target_name = "a2a_dst_%s" % test_id
        registry.register(
            AgentIdentity.create(topology, "sw", "uart"), session=SESSION,
            workspace_id=workspace_id, tab_id=target_tab.tab_id,
            pane_id=target_pane, agent_name=target_name,
            status="idle", lifecycle="running",
        )

        prompt_log = Path(self.cwd.name) / "fake-pi-prompts.jsonl"
        ready_file = Path(self.cwd.name) / "fake-pi-ready"
        fake_program = Path(__file__).resolve().parent / "fixtures" / "fake_pi_logger.py"
        fake_command = "exec -a pi %s %s --session %s --log %s --ready %s" % (
            shlex.quote(sys.executable), shlex.quote(str(fake_program)),
            shlex.quote(SESSION), shlex.quote(str(prompt_log)), shlex.quote(str(ready_file)),
        )
        self.client.start_agent(target_pane, "bash -c %s" % shlex.quote(fake_command))

        detected = self.client.wait_for_agent_detected(target_pane, timeout_s=20)
        self.assertEqual(detected.pane_id, target_pane)
        self.assertEqual(self.client.wait_agent(
            target_pane, until=("idle",), timeout_ms=10000).status, "idle")
        self.client.rename_agent(target_pane, target_name)
        self.assertEqual(self.client.get_agent(target_name).pane_id, target_pane)
        self.assertTrue(self._wait_for_path(ready_file, timeout_s=5),
                        "fake pi 未完成 idle report-agent 上报")

        cli_bin = Path(self.cwd.name) / "bin"
        cli_bin.mkdir()
        a2a_command = cli_bin / "a2a"
        a2a_command.write_text(
            "#!%s\nfrom a2a_codex.cli import main\nraise SystemExit(main())\n" % sys.executable,
            encoding="utf-8",
        )
        a2a_command.chmod(0o755)
        self.cli_env = dict(os.environ)
        self.cli_env.update({
            "PATH": str(cli_bin) + os.pathsep + os.environ.get("PATH", ""),
            "A2A_STATE_DIR": str(state_dir),
            "A2A_TOPOLOGY": str(topology_path),
            "HERDR_SESSION": SESSION,
        })

        broker_log = Path(self.cwd.name) / "broker.log"
        self.broker_output = broker_log.open("w", encoding="utf-8")
        self.broker = subprocess.Popen(
            ["a2a", "broker", "run"], env=self.cli_env,
            cwd=str(Path(__file__).resolve().parents[3]),
            stdout=self.broker_output, stderr=subprocess.STDOUT, text=True,
        )
        self.addCleanup(self._stop_broker)
        runtime_lock = state_dir / "spool" / "broker-runtime.lock"
        self.assertTrue(self._wait_for_lock(runtime_lock, self.broker, timeout_s=10),
                        "Broker 未持有临时状态目录的运行锁: %s" % broker_log.read_text(encoding="utf-8"))

        send_env = dict(self.cli_env)
        send_env.update({
            "A2A_PROJECT_ID": "soc_a", "A2A_ROLE": "dv", "A2A_IP": "uart",
            "HERDR_PANE_ID": source_pane,
        })
        receipt = self._run_a2a("send", "dv_done", env=send_env)
        receipt_json = json.loads(receipt)
        self.assertEqual(receipt_json["edge_id"], "dv_done")
        expected_prompt = "uart已完成 UVM 验证，请开始驱动程序和 HAL 框架开发。"
        self.assertEqual(receipt_json["text"], expected_prompt)

        final_status = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            final_status = json.loads(self._run_a2a("status", receipt_json["msg_id"]))
            if final_status.get("state") == "DELIVERED":
                break
            time.sleep(0.1)
        self.assertIsNotNone(final_status)
        self.assertEqual(final_status["state"], "DELIVERED",
                         "最终 a2a status 非 DELIVERED: %r" % final_status)
        self.assertEqual(final_status["text"], expected_prompt)
        self.assertEqual(self.client.get_agent(target_name).status, "working")
        self.assertTrue(self._wait_for_prompt(prompt_log, expected_prompt, timeout_s=5),
                        "fake pi 日志没有收到固定模板文本")

    def test_cli_broker_preserves_fifo_for_two_edges_to_one_target(self) -> None:
        workspace_id = self.workspace.workspace_id
        source_pane = self.workspace.pane_id
        self.assertTrue(workspace_id)
        self.assertTrue(source_pane)
        self.assertNotEqual(workspace_id, "w1")
        self.assertNotEqual(source_pane, "w1:p2")

        target_tab = self.client.create_tab(
            workspace_id=workspace_id,
            label="a2a-fifo-%s" % uuid4().hex[:8],
            cwd=str(Path(self.cwd.name).resolve()), focus=False,
        )
        target_pane = target_tab.pane_id
        self.assertTrue(target_pane)
        self.assertNotEqual(target_pane, "w1:p2")

        test_id = uuid4().hex[:10]
        state_dir = Path(self.cwd.name) / "fifo-state"
        topology_path = Path(self.cwd.name) / "fifo-topology.yaml"
        topology_path.write_text(
            "version: 1\n"
            "project_id: soc_a\n"
            "workspace_label: A2A FIFO integration test\n"
            "roles:\n"
            "  dv: {label: fake sender, kind: pi, launcher: null}\n"
            "  sw: {label: fake receiver, kind: pi, launcher: null}\n"
            "ips: [uart]\n"
            "edges:\n"
            "  - id: dv_first\n"
            "    from: dv\n"
            "    to: sw\n"
            "    template: \"FIRST::{ip}::固定模板一\"\n"
            "  - id: dv_second\n"
            "    from: dv\n"
            "    to: sw\n"
            "    template: \"SECOND::{ip}::固定模板二\"\n",
            encoding="utf-8",
        )
        topology = Topology.load(topology_path)
        registry = Registry(path=state_dir / "registry.json")
        registry.register(
            AgentIdentity.create(topology, "dv", "uart"), session=SESSION,
            workspace_id=workspace_id, tab_id=self.workspace.tab_id,
            pane_id=source_pane, agent_name="a2a_fifo_src_%s" % test_id,
            status="idle", lifecycle="running",
        )
        target_name = "a2a_fifo_dst_%s" % test_id
        registry.register(
            AgentIdentity.create(topology, "sw", "uart"), session=SESSION,
            workspace_id=workspace_id, tab_id=target_tab.tab_id,
            pane_id=target_pane, agent_name=target_name,
            status="idle", lifecycle="running",
        )

        prompt_log = Path(self.cwd.name) / "fake-pi-fifo.jsonl"
        ready_file = Path(self.cwd.name) / "fake-pi-fifo-ready"
        fake_program = Path(__file__).resolve().parent / "fixtures" / "fake_pi_logger.py"
        fake_command = "exec -a pi %s %s --session %s --log %s --ready %s --return-idle-after-ms 2000" % (
            shlex.quote(sys.executable), shlex.quote(str(fake_program)),
            shlex.quote(SESSION), shlex.quote(str(prompt_log)), shlex.quote(str(ready_file)),
        )
        self.client.start_agent(target_pane, "bash -c %s" % shlex.quote(fake_command))
        detected = self.client.wait_for_agent_detected(target_pane, timeout_s=20)
        self.assertEqual(detected.pane_id, target_pane)
        self.assertEqual(self.client.wait_agent(
            target_pane, until=("idle",), timeout_ms=10000).status, "idle")
        self.client.rename_agent(target_pane, target_name)
        self.assertEqual(self.client.get_agent(target_name).pane_id, target_pane)
        self.assertTrue(self._wait_for_path(ready_file, timeout_s=5),
                        "fake pi 未完成 idle report-agent 上报")

        cli_bin = Path(self.cwd.name) / "fifo-bin"
        cli_bin.mkdir()
        a2a_command = cli_bin / "a2a"
        a2a_command.write_text(
            "#!%s\nfrom a2a_codex.cli import main\nraise SystemExit(main())\n" % sys.executable,
            encoding="utf-8",
        )
        a2a_command.chmod(0o755)
        self.cli_env = dict(os.environ)
        self.cli_env.update({
            "PATH": str(cli_bin) + os.pathsep + os.environ.get("PATH", ""),
            "A2A_STATE_DIR": str(state_dir),
            "A2A_TOPOLOGY": str(topology_path),
            "HERDR_SESSION": SESSION,
        })
        send_env = dict(self.cli_env)
        send_env.update({
            "A2A_PROJECT_ID": "soc_a", "A2A_ROLE": "dv", "A2A_IP": "uart",
            "HERDR_PANE_ID": source_pane,
        })

        first = json.loads(self._run_a2a("send", "dv_first", env=send_env))
        second = json.loads(self._run_a2a("send", "dv_second", env=send_env))
        expected_first = "FIRST::uart::固定模板一"
        expected_second = "SECOND::uart::固定模板二"
        self.assertEqual(first["edge_id"], "dv_first")
        self.assertEqual(first["text"], expected_first)
        self.assertEqual(second["edge_id"], "dv_second")
        self.assertEqual(second["text"], expected_second)
        self.assertNotEqual(first["msg_id"], second["msg_id"])

        broker_log = Path(self.cwd.name) / "fifo-broker.log"
        self.broker_output = broker_log.open("w", encoding="utf-8")
        self.broker = subprocess.Popen(
            ["a2a", "broker", "run"], env=self.cli_env,
            cwd=str(Path(__file__).resolve().parents[3]),
            stdout=self.broker_output, stderr=subprocess.STDOUT, text=True,
        )
        self.addCleanup(self._stop_broker)
        runtime_lock = state_dir / "spool" / "broker-runtime.lock"
        self.assertTrue(self._wait_for_lock(runtime_lock, self.broker, timeout_s=10),
                        "Broker 未持有临时状态目录的运行锁: %s" % broker_log.read_text(encoding="utf-8"))

        final_statuses = []
        for receipt in (first, second):
            final_status = None
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                final_status = json.loads(self._run_a2a("status", receipt["msg_id"]))
                if final_status.get("state") == "DELIVERED":
                    break
                time.sleep(0.1)
            self.assertIsNotNone(final_status)
            self.assertEqual(final_status["state"], "DELIVERED",
                             "最终 a2a status 非 DELIVERED: %r" % final_status)
            self.assertEqual(final_status["text"], receipt["text"])
            final_statuses.append(final_status)

        deadline = time.monotonic() + 5
        events = []
        expected_states = ["idle", "working", "idle", "working", "idle"]
        while time.monotonic() < deadline:
            if prompt_log.exists():
                try:
                    events = [json.loads(line) for line in prompt_log.read_text(encoding="utf-8").splitlines()]
                except (OSError, ValueError):
                    events = []
                prompts = [event["prompt"] for event in events if event.get("event") == "prompt"]
                states = [event["state"] for event in events if event.get("event") == "state"]
                if prompts == [expected_first, expected_second] and states == expected_states:
                    break
            time.sleep(0.05)
        prompts = [event["prompt"] for event in events if event.get("event") == "prompt"]
        self.assertEqual(prompts, [expected_first, expected_second],
                         "fake pi 收到的模板顺序不符合 FIFO")
        states = [event["state"] for event in events if event.get("event") == "state"]
        self.assertEqual(states, expected_states)
        self.assertEqual([status["state"] for status in final_statuses],
                         ["DELIVERED", "DELIVERED"])
        self.assertIn(self.client.get_agent(target_name).status, {"idle", "done"})

    def test_cli_broker_dispatches_different_targets_in_parallel(self) -> None:
        workspace_id = self.workspace.workspace_id
        source_uart_pane = self.workspace.pane_id
        self.assertTrue(workspace_id)
        self.assertTrue(source_uart_pane)
        self.assertNotEqual(workspace_id, "w1")
        self.assertNotEqual(source_uart_pane, "w1:p2")

        source_spi_tab = self.client.create_tab(
            workspace_id=workspace_id, label="a2a-src-spi-%s" % uuid4().hex[:8],
            cwd=str(Path(self.cwd.name).resolve()), focus=False,
        )
        target_tabs = {
            ip_id: self.client.create_tab(
                workspace_id=workspace_id,
                label="a2a-target-%s-%s" % (ip_id, uuid4().hex[:8]),
                cwd=str(Path(self.cwd.name).resolve()), focus=False,
            )
            for ip_id in ("uart", "spi")
        }
        source_panes = {"uart": source_uart_pane, "spi": source_spi_tab.pane_id}
        target_panes = {ip_id: tab.pane_id for ip_id, tab in target_tabs.items()}
        for pane_id in [*source_panes.values(), *target_panes.values()]:
            self.assertTrue(pane_id)
            self.assertNotEqual(pane_id, "w1:p2")
        self.assertEqual(len(set(source_panes.values()) | set(target_panes.values())), 4)

        test_id = uuid4().hex[:10]
        state_dir = Path(self.cwd.name) / "parallel-state"
        topology_path = Path(self.cwd.name) / "parallel-topology.yaml"
        topology_path.write_text(
            "version: 1\n"
            "project_id: soc_a\n"
            "workspace_label: A2A multi-target integration test\n"
            "roles:\n"
            "  dv: {label: fake sender, kind: pi, launcher: null}\n"
            "  sw: {label: fake receiver, kind: pi, launcher: null}\n"
            "ips: [uart, spi]\n"
            "edges:\n"
            "  - id: dv_uart_done\n"
            "    from: dv\n"
            "    to: sw\n"
            "    template: \"UART_FIXED::{ip}::并行目标模板\"\n"
            "  - id: dv_spi_done\n"
            "    from: dv\n"
            "    to: sw\n"
            "    template: \"SPI_FIXED::{ip}::并行目标模板\"\n",
            encoding="utf-8",
        )
        topology = Topology.load(topology_path)
        registry = Registry(path=state_dir / "registry.json")
        for ip_id in ("uart", "spi"):
            source_tab_id = self.workspace.tab_id if ip_id == "uart" else source_spi_tab.tab_id
            registry.register(
                AgentIdentity.create(topology, "dv", ip_id), session=SESSION,
                workspace_id=workspace_id, tab_id=source_tab_id,
                pane_id=source_panes[ip_id], agent_name="a2a_src_%s_%s" % (ip_id, test_id),
                status="idle", lifecycle="running",
            )
            registry.register(
                AgentIdentity.create(topology, "sw", ip_id), session=SESSION,
                workspace_id=workspace_id, tab_id=target_tabs[ip_id].tab_id,
                pane_id=target_panes[ip_id], agent_name="a2a_dst_%s_%s" % (ip_id, test_id),
                status="idle", lifecycle="running",
            )

        barrier_dir = Path(self.cwd.name) / "target-barrier"
        fake_program = Path(__file__).resolve().parent / "fixtures" / "fake_pi_logger.py"
        target_names = {}
        target_logs = {}
        for ip_id, peer_ip in (("uart", "spi"), ("spi", "uart")):
            target_name = "a2a_dst_%s_%s" % (ip_id, test_id)
            target_names[ip_id] = target_name
            target_log = Path(self.cwd.name) / ("fake-pi-%s.jsonl" % ip_id)
            target_logs[ip_id] = target_log
            ready_file = Path(self.cwd.name) / ("fake-pi-%s-ready" % ip_id)
            fake_command = (
                "exec -a pi %s %s --session %s --log %s --ready %s "
                "--return-idle-after-ms 1500 --barrier-dir %s --barrier-key %s "
                "--barrier-peer-key %s --barrier-timeout-ms 8000"
            ) % (
                shlex.quote(sys.executable), shlex.quote(str(fake_program)),
                shlex.quote(SESSION), shlex.quote(str(target_log)), shlex.quote(str(ready_file)),
                shlex.quote(str(barrier_dir)), shlex.quote(ip_id), shlex.quote(peer_ip),
            )
            self.client.start_agent(target_panes[ip_id], "bash -c %s" % shlex.quote(fake_command))
            detected = self.client.wait_for_agent_detected(target_panes[ip_id], timeout_s=20)
            self.assertEqual(detected.pane_id, target_panes[ip_id])
            self.assertEqual(self.client.wait_agent(
                target_panes[ip_id], until=("idle",), timeout_ms=10000).status, "idle")
            self.client.rename_agent(target_panes[ip_id], target_name)
            self.assertEqual(self.client.get_agent(target_name).pane_id, target_panes[ip_id])
            self.assertTrue(self._wait_for_path(ready_file, timeout_s=5),
                            "%s fake pi 未完成 idle report-agent 上报" % ip_id)

        cli_bin = Path(self.cwd.name) / "parallel-bin"
        cli_bin.mkdir()
        a2a_command = cli_bin / "a2a"
        a2a_command.write_text(
            "#!%s\nfrom a2a_codex.cli import main\nraise SystemExit(main())\n" % sys.executable,
            encoding="utf-8",
        )
        a2a_command.chmod(0o755)
        self.cli_env = dict(os.environ)
        self.cli_env.update({
            "PATH": str(cli_bin) + os.pathsep + os.environ.get("PATH", ""),
            "A2A_STATE_DIR": str(state_dir),
            "A2A_TOPOLOGY": str(topology_path),
            "HERDR_SESSION": SESSION,
        })

        receipts = {}
        for ip_id, edge_id in (("uart", "dv_uart_done"), ("spi", "dv_spi_done")):
            send_env = dict(self.cli_env)
            send_env.update({
                "A2A_PROJECT_ID": "soc_a", "A2A_ROLE": "dv", "A2A_IP": ip_id,
                "HERDR_PANE_ID": source_panes[ip_id],
            })
            receipt = json.loads(self._run_a2a("send", edge_id, env=send_env))
            expected_prompt = "%s_FIXED::%s::并行目标模板" % (ip_id.upper(), ip_id)
            self.assertEqual(receipt["edge_id"], edge_id)
            self.assertEqual(receipt["dst"], "sw_%s" % ip_id)
            self.assertEqual(receipt["text"], expected_prompt)
            receipts[ip_id] = receipt
        self.assertNotEqual(receipts["uart"]["msg_id"], receipts["spi"]["msg_id"])

        broker_log = Path(self.cwd.name) / "parallel-broker.log"
        self.broker_output = broker_log.open("w", encoding="utf-8")
        self.broker = subprocess.Popen(
            ["a2a", "broker", "run"], env=self.cli_env,
            cwd=str(Path(__file__).resolve().parents[3]),
            stdout=self.broker_output, stderr=subprocess.STDOUT, text=True,
        )
        self.addCleanup(self._stop_broker)
        runtime_lock = state_dir / "spool" / "broker-runtime.lock"
        self.assertTrue(self._wait_for_lock(runtime_lock, self.broker, timeout_s=10),
                        "Broker 未持有临时状态目录的运行锁: %s" % broker_log.read_text(encoding="utf-8"))

        final_statuses = {}
        for ip_id in ("uart", "spi"):
            receipt = receipts[ip_id]
            final_status = None
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                final_status = json.loads(self._run_a2a("status", receipt["msg_id"]))
                if final_status.get("state") == "DELIVERED":
                    break
                time.sleep(0.1)
            self.assertIsNotNone(final_status)
            self.assertEqual(final_status["state"], "DELIVERED",
                             "%s 最终 status 非 DELIVERED: %r" % (ip_id, final_status))
            self.assertEqual(final_status["text"], receipt["text"])
            final_statuses[ip_id] = final_status

        for ip_id in ("uart", "spi"):
            self.assertTrue(self._wait_for_path(
                barrier_dir / (ip_id + ".arrived"), timeout_s=2),
                "%s fake pi 未到达共享 barrier" % ip_id,
            )
        for ip_id in ("uart", "spi"):
            log_path = target_logs[ip_id]
            deadline = time.monotonic() + 5
            events = []
            while time.monotonic() < deadline:
                if log_path.exists():
                    try:
                        events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
                    except (OSError, ValueError):
                        events = []
                    states = [event["state"] for event in events if event.get("event") == "state"]
                    barriers = [event["state"] for event in events if event.get("event") == "barrier"]
                    if states == ["idle", "working", "idle"] and barriers == ["arrived", "released"]:
                        break
                time.sleep(0.05)
            prompts = [event["prompt"] for event in events if event.get("event") == "prompt"]
            states = [event["state"] for event in events if event.get("event") == "state"]
            barriers = [event["state"] for event in events if event.get("event") == "barrier"]
            expected_prompt = "%s_FIXED::%s::并行目标模板" % (ip_id.upper(), ip_id)
            self.assertEqual(prompts, [expected_prompt], "%s fake pi prompt 不匹配" % ip_id)
            self.assertEqual(states, ["idle", "working", "idle"], "%s fake pi 状态序列异常" % ip_id)
            self.assertEqual(barriers, ["arrived", "released"], "%s barrier 未被两端共同释放" % ip_id)
            self.assertEqual(final_statuses[ip_id]["state"], "DELIVERED")

    def test_broker_process_crash_recovers_dispatching_without_resend(self) -> None:
        workspace_id = self.workspace.workspace_id
        source_pane = self.workspace.pane_id
        self.assertTrue(workspace_id)
        self.assertTrue(source_pane)
        self.assertNotEqual(workspace_id, "w1")
        self.assertNotEqual(source_pane, "w1:p2")

        target_tab = self.client.create_tab(
            workspace_id=workspace_id, label="a2a-crash-target-%s" % uuid4().hex[:8],
            cwd=str(Path(self.cwd.name).resolve()), focus=False,
        )
        target_pane = target_tab.pane_id
        self.assertTrue(target_pane)
        self.assertNotEqual(target_pane, "w1:p2")

        test_id = uuid4().hex[:10]
        state_dir = Path(self.cwd.name) / "crash-state"
        topology_path = Path(self.cwd.name) / "crash-topology.yaml"
        topology_path.write_text(
            "version: 1\n"
            "project_id: soc_a\n"
            "workspace_label: A2A broker crash integration test\n"
            "roles:\n"
            "  dv: {label: fake sender, kind: pi, launcher: null}\n"
            "  sw: {label: fake receiver, kind: pi, launcher: null}\n"
            "ips: [uart]\n"
            "edges:\n"
            "  - id: dv_first\n"
            "    from: dv\n"
            "    to: sw\n"
            "    template: \"CRASH_FIRST::{ip}::固定消息\"\n"
            "  - id: dv_later\n"
            "    from: dv\n"
            "    to: sw\n"
            "    template: \"CRASH_LATER::{ip}::不得越过队列头\"\n",
            encoding="utf-8",
        )
        topology = Topology.load(topology_path)
        registry = Registry(path=state_dir / "registry.json")
        registry.register(
            AgentIdentity.create(topology, "dv", "uart"), session=SESSION,
            workspace_id=workspace_id, tab_id=self.workspace.tab_id,
            pane_id=source_pane, agent_name="a2a_crash_src_%s" % test_id,
            status="idle", lifecycle="running",
        )
        target_name = "a2a_crash_dst_%s" % test_id
        registry.register(
            AgentIdentity.create(topology, "sw", "uart"), session=SESSION,
            workspace_id=workspace_id, tab_id=target_tab.tab_id,
            pane_id=target_pane, agent_name=target_name,
            status="idle", lifecycle="running",
        )

        prompt_log = Path(self.cwd.name) / "fake-pi-crash.jsonl"
        ready_file = Path(self.cwd.name) / "fake-pi-crash-ready"
        fake_program = Path(__file__).resolve().parent / "fixtures" / "fake_pi_logger.py"
        fake_command = "exec -a pi %s %s --session %s --log %s --ready %s --hold-without-working" % (
            shlex.quote(sys.executable), shlex.quote(str(fake_program)),
            shlex.quote(SESSION), shlex.quote(str(prompt_log)), shlex.quote(str(ready_file)),
        )
        self.client.start_agent(target_pane, "bash -c %s" % shlex.quote(fake_command))
        detected = self.client.wait_for_agent_detected(target_pane, timeout_s=20)
        self.assertEqual(detected.pane_id, target_pane)
        self.assertEqual(self.client.wait_agent(
            target_pane, until=("idle",), timeout_ms=10000).status, "idle")
        self.client.rename_agent(target_pane, target_name)
        self.assertEqual(self.client.get_agent(target_name).pane_id, target_pane)
        self.assertTrue(self._wait_for_path(ready_file, timeout_s=5),
                        "fake pi 未完成初始 idle report-agent 上报")

        cli_bin = Path(self.cwd.name) / "crash-bin"
        cli_bin.mkdir()
        a2a_command = cli_bin / "a2a"
        a2a_command.write_text(
            "#!%s\nfrom a2a_codex.cli import main\nraise SystemExit(main())\n" % sys.executable,
            encoding="utf-8",
        )
        a2a_command.chmod(0o755)
        self.cli_env = dict(os.environ)
        self.cli_env.update({
            "PATH": str(cli_bin) + os.pathsep + os.environ.get("PATH", ""),
            "A2A_STATE_DIR": str(state_dir),
            "A2A_TOPOLOGY": str(topology_path),
            "HERDR_SESSION": SESSION,
        })
        send_env = dict(self.cli_env)
        send_env.update({
            "A2A_PROJECT_ID": "soc_a", "A2A_ROLE": "dv", "A2A_IP": "uart",
            "HERDR_PANE_ID": source_pane,
        })
        first = json.loads(self._run_a2a("send", "dv_first", env=send_env))
        later = json.loads(self._run_a2a("send", "dv_later", env=send_env))
        self.assertEqual(first["state"], "QUEUED")
        self.assertEqual(later["state"], "QUEUED")
        self.assertNotEqual(first["msg_id"], later["msg_id"])

        broker_log = Path(self.cwd.name) / "broker-before-sigkill.log"
        self.broker_output = broker_log.open("w", encoding="utf-8")
        self.broker = subprocess.Popen(
            ["a2a", "broker", "run"], env=self.cli_env,
            cwd=str(Path(__file__).resolve().parents[3]),
            stdout=self.broker_output, stderr=subprocess.STDOUT, text=True,
        )
        self.addCleanup(self._kill_broker_if_running)
        runtime_lock = state_dir / "spool" / "broker-runtime.lock"
        self.assertTrue(self._wait_for_lock(runtime_lock, self.broker, timeout_s=10),
                        "初始 Broker 未持有运行锁: %s" % broker_log.read_text(encoding="utf-8"))

        expected_first = "CRASH_FIRST::uart::固定消息"
        first_status = None
        later_status = None
        prompt_events = []
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            first_status = json.loads(self._run_a2a("status", first["msg_id"]))
            later_status = json.loads(self._run_a2a("status", later["msg_id"]))
            if prompt_log.exists():
                try:
                    prompt_events = [json.loads(line) for line in prompt_log.read_text(encoding="utf-8").splitlines()]
                except (OSError, ValueError):
                    prompt_events = []
            prompts = [event.get("prompt") for event in prompt_events if event.get("event") == "prompt"]
            if first_status.get("state") == "DISPATCHING" and prompts == [expected_first]:
                break
            time.sleep(0.05)
        self.assertEqual(first_status.get("state"), "DISPATCHING",
                         "SIGKILL 前未观测到 DISPATCHING: %r" % first_status)
        self.assertEqual(later_status.get("state"), "QUEUED",
                         "队列后续消息在崩溃前越过队列头: %r" % later_status)
        self.assertEqual([event.get("prompt") for event in prompt_events
                          if event.get("event") == "prompt"], [expected_first])
        self.assertIn("working_suppressed", [event.get("state") for event in prompt_events
                                             if event.get("event") == "state"])

        crashed_broker = self.broker
        crashed_broker.send_signal(signal.SIGKILL)
        crashed_returncode = crashed_broker.wait(timeout=10)
        self.broker = None
        if not self.broker_output.closed:
            self.broker_output.close()
        self.assertEqual(crashed_returncode, -signal.SIGKILL,
                         "Broker 未由测试进程 SIGKILL: returncode=%s" % crashed_returncode)

        restarted_log = Path(self.cwd.name) / "broker-after-restart.log"
        self.broker_output = restarted_log.open("w", encoding="utf-8")
        self.broker = subprocess.Popen(
            ["a2a", "broker", "run"], env=self.cli_env,
            cwd=str(Path(__file__).resolve().parents[3]),
            stdout=self.broker_output, stderr=subprocess.STDOUT, text=True,
        )
        self.addCleanup(self._stop_broker)
        self.assertTrue(self._wait_for_lock(runtime_lock, self.broker, timeout_s=10),
                        "重启 Broker 未持有运行锁: %s" % restarted_log.read_text(encoding="utf-8"))

        recovered_first = None
        recovered_later = None
        queue_view = []
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            recovered_first = json.loads(self._run_a2a("status", first["msg_id"]))
            recovered_later = json.loads(self._run_a2a("status", later["msg_id"]))
            queue_view = json.loads(self._run_a2a("queue", "sw_uart"))
            if (recovered_first.get("state") == "DELIVERY_UNCERTAIN"
                    and queue_view and queue_view[0].get("paused")):
                break
            time.sleep(0.05)
        self.assertEqual(recovered_first.get("state"), "DELIVERY_UNCERTAIN",
                         "遗留 DISPATCHING 未恢复为 DELIVERY_UNCERTAIN: %r" % recovered_first)
        self.assertEqual(recovered_later.get("state"), "QUEUED",
                         "恢复后后续消息越过不确定队列头: %r" % recovered_later)
        self.assertEqual(queue_view[0]["head"]["msg_id"], first["msg_id"])
        self.assertTrue(queue_view[0]["paused"], "不确定队列头未暂停目标队列")
        self.assertEqual(queue_view[0]["paused_message_count"], 1)

        # 观察一个 broker polling interval，排除恢复后误重发或越过队列头。
        time.sleep(1.5)
        final_first = json.loads(self._run_a2a("status", first["msg_id"]))
        final_later = json.loads(self._run_a2a("status", later["msg_id"]))
        try:
            prompt_events = [json.loads(line) for line in prompt_log.read_text(encoding="utf-8").splitlines()]
        except (OSError, ValueError):
            prompt_events = []
        self.assertEqual(final_first["state"], "DELIVERY_UNCERTAIN")
        self.assertEqual(final_later["state"], "QUEUED")
        self.assertEqual([event.get("prompt") for event in prompt_events
                          if event.get("event") == "prompt"], [expected_first],
                         "Broker 重启后重复 prompt 或投递了后续消息")

    def test_broker_crash_before_herdr_prompt_recovers_without_delivery(self) -> None:
        workspace_id = self.workspace.workspace_id
        source_pane = self.workspace.pane_id
        self.assertTrue(workspace_id)
        self.assertTrue(source_pane)
        self.assertNotEqual(workspace_id, "w1")
        self.assertNotEqual(source_pane, "w1:p2")

        target_tab = self.client.create_tab(
            workspace_id=workspace_id, label="a2a-preprompt-%s" % uuid4().hex[:8],
            cwd=str(Path(self.cwd.name).resolve()), focus=False,
        )
        target_pane = target_tab.pane_id
        self.assertTrue(target_pane)
        self.assertNotEqual(target_pane, "w1:p2")

        test_id = uuid4().hex[:10]
        state_dir = Path(self.cwd.name) / "preprompt-state"
        topology_path = Path(self.cwd.name) / "preprompt-topology.yaml"
        topology_path.write_text(
            "version: 1\n"
            "project_id: soc_a\n"
            "workspace_label: A2A pre-prompt crash integration test\n"
            "roles:\n"
            "  dv: {label: fake sender, kind: pi, launcher: null}\n"
            "  sw: {label: fake receiver, kind: pi, launcher: null}\n"
            "ips: [uart]\n"
            "edges:\n"
            "  - id: dv_first\n"
            "    from: dv\n"
            "    to: sw\n"
            "    template: \"PREPROMPT_FIRST::{ip}::必须未送达\"\n"
            "  - id: dv_later\n"
            "    from: dv\n"
            "    to: sw\n"
            "    template: \"PREPROMPT_LATER::{ip}::不得越过队列头\"\n",
            encoding="utf-8",
        )
        topology = Topology.load(topology_path)
        registry = Registry(path=state_dir / "registry.json")
        registry.register(
            AgentIdentity.create(topology, "dv", "uart"), session=SESSION,
            workspace_id=workspace_id, tab_id=self.workspace.tab_id,
            pane_id=source_pane, agent_name="a2a_pre_src_%s" % test_id,
            status="idle", lifecycle="running",
        )
        target_name = "a2a_pre_dst_%s" % test_id
        registry.register(
            AgentIdentity.create(topology, "sw", "uart"), session=SESSION,
            workspace_id=workspace_id, tab_id=target_tab.tab_id,
            pane_id=target_pane, agent_name=target_name,
            status="idle", lifecycle="running",
        )

        prompt_log = Path(self.cwd.name) / "fake-pi-preprompt.jsonl"
        ready_file = Path(self.cwd.name) / "fake-pi-preprompt-ready"
        fake_program = Path(__file__).resolve().parent / "fixtures" / "fake_pi_logger.py"
        fake_command = "exec -a pi %s %s --session %s --log %s --ready %s" % (
            shlex.quote(sys.executable), shlex.quote(str(fake_program)),
            shlex.quote(SESSION), shlex.quote(str(prompt_log)), shlex.quote(str(ready_file)),
        )
        self.client.start_agent(target_pane, "bash -c %s" % shlex.quote(fake_command))
        detected = self.client.wait_for_agent_detected(target_pane, timeout_s=20)
        self.assertEqual(detected.pane_id, target_pane)
        self.assertEqual(self.client.wait_agent(
            target_pane, until=("idle",), timeout_ms=10000).status, "idle")
        self.client.rename_agent(target_pane, target_name)
        self.assertEqual(self.client.get_agent(target_name).pane_id, target_pane)
        self.assertTrue(self._wait_for_path(ready_file, timeout_s=5),
                        "fake pi 未完成初始 idle report-agent 上报")

        real_herdr = shutil.which("herdr", path=os.environ.get("PATH"))
        self.assertTrue(real_herdr, "测试环境找不到原始 herdr 可执行文件")
        cli_bin = Path(self.cwd.name) / "preprompt-bin"
        cli_bin.mkdir()
        a2a_command = cli_bin / "a2a"
        a2a_command.write_text(
            "#!%s\nfrom a2a_codex.cli import main\nraise SystemExit(main())\n" % sys.executable,
            encoding="utf-8",
        )
        a2a_command.chmod(0o755)

        intercepted = Path(self.cwd.name) / "prompt-intercepted.json"
        wrapper_events = Path(self.cwd.name) / "prompt-wrapper-events.jsonl"
        expected_pid_file = Path(self.cwd.name) / "expected-broker.pid"
        authorize_kill = Path(self.cwd.name) / "authorize-broker-kill"
        wrapper = cli_bin / "herdr"
        wrapper_source = '''import json, os, signal, sys, time
REAL_HERDR = @REAL_HERDR@
SESSION = @SESSION@
INTERCEPTED = @INTERCEPTED@
EVENTS = @EVENTS@
EXPECTED_PID = @EXPECTED_PID@
AUTHORIZE = @AUTHORIZE@

def record(event):
    with open(EVENTS, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(event) + "\\n")
        stream.flush()
        os.fsync(stream.fileno())

argv = sys.argv[1:]
is_prompt = any(argv[index:index + 2] == ["agent", "prompt"]
                for index in range(max(0, len(argv) - 1)))
if is_prompt:
    parent_pid = os.getppid()
    if len(argv) < 4 or argv[:2] != ["--session", SESSION]:
        record({"event": "unexpected_prompt_session_or_args", "parent_pid": parent_pid,
                "argv": argv[:4]})
        sys.exit(96)
    if os.path.exists(INTERCEPTED):
        record({"event": "unexpected_prompt", "parent_pid": parent_pid})
        sys.exit(97)
    payload = json.dumps({"event": "intercepted", "parent_pid": parent_pid,
                          "argv": argv}).encode("utf-8")
    temporary = INTERCEPTED + ".tmp"
    with open(temporary, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, INTERCEPTED)
    record({"event": "intercepted", "parent_pid": parent_pid})
    deadline = time.monotonic() + 20
    while not os.path.exists(AUTHORIZE) and time.monotonic() < deadline:
        if os.getppid() != parent_pid:
            record({"event": "parent_exited_before_authorization", "parent_pid": parent_pid})
            sys.exit(98)
        time.sleep(0.01)
    if not os.path.exists(AUTHORIZE):
        record({"event": "authorization_timeout", "parent_pid": parent_pid})
        sys.exit(99)
    try:
        expected_pid = int(open(EXPECTED_PID, encoding="utf-8").read().strip())
    except (OSError, ValueError):
        record({"event": "invalid_expected_pid", "parent_pid": parent_pid})
        sys.exit(100)
    if parent_pid != expected_pid:
        record({"event": "parent_pid_mismatch", "parent_pid": parent_pid,
                "expected_pid": expected_pid})
        sys.exit(101)
    record({"event": "sigkill_parent", "parent_pid": parent_pid,
            "expected_pid": expected_pid})
    os.kill(parent_pid, signal.SIGKILL)
    os._exit(0)

os.execv(REAL_HERDR, [REAL_HERDR, *argv])
'''
        for placeholder, value in (
            ("@REAL_HERDR@", repr(real_herdr)), ("@SESSION@", repr(SESSION)),
            ("@INTERCEPTED@", repr(str(intercepted))), ("@EVENTS@", repr(str(wrapper_events))),
            ("@EXPECTED_PID@", repr(str(expected_pid_file))), ("@AUTHORIZE@", repr(str(authorize_kill))),
        ):
            wrapper_source = wrapper_source.replace(placeholder, value)
        wrapper.write_text("#!%s\n" % sys.executable + wrapper_source, encoding="utf-8")
        wrapper.chmod(0o755)

        self.cli_env = dict(os.environ)
        self.cli_env.update({
            "PATH": str(cli_bin) + os.pathsep + os.environ.get("PATH", ""),
            "A2A_STATE_DIR": str(state_dir),
            "A2A_TOPOLOGY": str(topology_path),
            "HERDR_SESSION": SESSION,
        })
        send_env = dict(self.cli_env)
        send_env.update({
            "A2A_PROJECT_ID": "soc_a", "A2A_ROLE": "dv", "A2A_IP": "uart",
            "HERDR_PANE_ID": source_pane,
        })
        first = json.loads(self._run_a2a("send", "dv_first", env=send_env))
        later = json.loads(self._run_a2a("send", "dv_later", env=send_env))
        self.assertEqual(first["state"], "QUEUED")
        self.assertEqual(later["state"], "QUEUED")

        broker_log = Path(self.cwd.name) / "preprompt-broker.log"
        self.broker_output = broker_log.open("w", encoding="utf-8")
        self.broker = subprocess.Popen(
            ["a2a", "broker", "run"], env=self.cli_env,
            cwd=str(Path(__file__).resolve().parents[3]),
            stdout=self.broker_output, stderr=subprocess.STDOUT, text=True,
        )
        self.addCleanup(self._kill_broker_if_running)
        runtime_lock = state_dir / "spool" / "broker-runtime.lock"
        self.assertTrue(self._wait_for_lock(runtime_lock, self.broker, timeout_s=10),
                        "Broker 未持有运行锁: %s" % broker_log.read_text(encoding="utf-8"))
        self.assertTrue(self._wait_for_path(intercepted, timeout_s=15),
                        "Herdr prompt wrapper 未拦截 agent prompt: %s" % broker_log.read_text(encoding="utf-8"))

        intercept_record = json.loads(intercepted.read_text(encoding="utf-8"))
        self.assertEqual(intercept_record["event"], "intercepted")
        self.assertEqual(intercept_record["parent_pid"], self.broker.pid,
                         "prompt wrapper 的父进程不是此测试启动的 Broker")
        self.assertEqual(intercept_record["argv"][2:4], ["agent", "prompt"])
        self.assertEqual(intercept_record["argv"][4], target_pane)
        self.assertEqual(intercept_record["argv"][5], "PREPROMPT_FIRST::uart::必须未送达")
        first_status = json.loads(self._run_a2a("status", first["msg_id"]))
        later_status = json.loads(self._run_a2a("status", later["msg_id"]))
        self.assertEqual(first_status["state"], "DISPATCHING",
                         "prompt 被拦截时首消息未持久化为 DISPATCHING: %r" % first_status)
        self.assertEqual(later_status["state"], "QUEUED")
        self.assertEqual(self._read_prompt_events(prompt_log), [],
                         "调用真实 Herdr prompt 前 fake pi 已收到 prompt")
        self.assertIsNone(self.broker.poll(), "wrapper 等待授权期间 Broker 已提前退出")

        expected_pid_file.write_text("%s\n" % self.broker.pid, encoding="ascii")
        authorize_kill.write_text("SIGKILL only the verified Broker parent\n", encoding="ascii")
        crashed_broker = self.broker
        crashed_returncode = crashed_broker.wait(timeout=10)
        self.broker = None
        if not self.broker_output.closed:
            self.broker_output.close()
        self.assertEqual(crashed_returncode, -signal.SIGKILL,
                         "wrapper 未 SIGKILL 测试 Broker: returncode=%s" % crashed_returncode)
        events = [json.loads(line) for line in wrapper_events.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([event["event"] for event in events], ["intercepted", "sigkill_parent"])
        self.assertEqual(events[1]["parent_pid"], crashed_broker.pid)
        self.assertEqual(events[1]["expected_pid"], crashed_broker.pid)
        self.assertEqual(self._read_prompt_events(prompt_log), [],
                         "prompt wrapper 拦截并杀死 Broker 后 fake pi 仍收到 prompt")

        restarted_log = Path(self.cwd.name) / "preprompt-broker-restarted.log"
        self.broker_output = restarted_log.open("w", encoding="utf-8")
        self.broker = subprocess.Popen(
            ["a2a", "broker", "run"], env=self.cli_env,
            cwd=str(Path(__file__).resolve().parents[3]),
            stdout=self.broker_output, stderr=subprocess.STDOUT, text=True,
        )
        self.addCleanup(self._stop_broker)
        self.assertTrue(self._wait_for_lock(runtime_lock, self.broker, timeout_s=10),
                        "重启 Broker 未持有运行锁: %s" % restarted_log.read_text(encoding="utf-8"))

        recovered_first = None
        recovered_later = None
        queue_view = []
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            recovered_first = json.loads(self._run_a2a("status", first["msg_id"]))
            recovered_later = json.loads(self._run_a2a("status", later["msg_id"]))
            queue_view = json.loads(self._run_a2a("queue", "sw_uart"))
            if (recovered_first.get("state") == "DELIVERY_UNCERTAIN"
                    and queue_view and queue_view[0].get("paused")):
                break
            time.sleep(0.05)
        self.assertEqual(recovered_first.get("state"), "DELIVERY_UNCERTAIN")
        self.assertEqual(recovered_later.get("state"), "QUEUED")
        self.assertEqual(queue_view[0]["head"]["msg_id"], first["msg_id"])
        self.assertTrue(queue_view[0]["paused"])
        self.assertEqual(queue_view[0]["paused_message_count"], 1)

        time.sleep(1.5)
        self.assertEqual(json.loads(self._run_a2a("status", first["msg_id"]))["state"],
                         "DELIVERY_UNCERTAIN")
        self.assertIn(json.loads(self._run_a2a("status", later["msg_id"]))["state"],
                      {"QUEUED", "WAITING_TARGET"})
        self.assertEqual(self._read_prompt_events(prompt_log), [])
        events = [json.loads(line) for line in wrapper_events.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([event["event"] for event in events], ["intercepted", "sigkill_parent"],
                         "重启后出现额外的 Herdr agent prompt 调用")

    def test_broker_crash_before_dispatching_preserves_queue_then_delivers_once(self) -> None:
        workspace_id = self.workspace.workspace_id
        source_pane = self.workspace.pane_id
        self.assertTrue(workspace_id)
        self.assertTrue(source_pane)
        self.assertNotEqual(workspace_id, "w1")
        self.assertNotEqual(source_pane, "w1:p2")

        target_tab = self.client.create_tab(
            workspace_id=workspace_id, label="a2a-predispatch-%s" % uuid4().hex[:8],
            cwd=str(Path(self.cwd.name).resolve()), focus=False,
        )
        target_pane = target_tab.pane_id
        self.assertTrue(target_pane)
        self.assertNotEqual(target_pane, "w1:p2")

        test_id = uuid4().hex[:10]
        state_dir = Path(self.cwd.name) / "predispatch-state"
        topology_path = Path(self.cwd.name) / "predispatch-topology.yaml"
        topology_path.write_text(
            "version: 1\n"
            "project_id: soc_a\n"
            "workspace_label: A2A pre-DISPATCHING crash integration test\n"
            "roles:\n"
            "  dv: {label: fake sender, kind: pi, launcher: null}\n"
            "  sw: {label: fake receiver, kind: pi, launcher: null}\n"
            "ips: [uart]\n"
            "edges:\n"
            "  - id: dv_first\n"
            "    from: dv\n"
            "    to: sw\n"
            "    template: \"PREDISPATCH_FIRST::{ip}::先投递\"\n"
            "  - id: dv_later\n"
            "    from: dv\n"
            "    to: sw\n"
            "    template: \"PREDISPATCH_LATER::{ip}::不得越过队列头\"\n",
            encoding="utf-8",
        )
        topology = Topology.load(topology_path)
        registry = Registry(path=state_dir / "registry.json")
        registry.register(
            AgentIdentity.create(topology, "dv", "uart"), session=SESSION,
            workspace_id=workspace_id, tab_id=self.workspace.tab_id,
            pane_id=source_pane, agent_name="a2a_predispatch_src_%s" % test_id,
            status="idle", lifecycle="running",
        )
        target_name = "a2a_predispatch_dst_%s" % test_id
        registry.register(
            AgentIdentity.create(topology, "sw", "uart"), session=SESSION,
            workspace_id=workspace_id, tab_id=target_tab.tab_id,
            pane_id=target_pane, agent_name=target_name,
            status="idle", lifecycle="running",
        )

        prompt_log = Path(self.cwd.name) / "fake-pi-predispatch.jsonl"
        ready_file = Path(self.cwd.name) / "fake-pi-predispatch-ready"
        fake_program = Path(__file__).resolve().parent / "fixtures" / "fake_pi_logger.py"
        fake_command = "exec -a pi %s %s --session %s --log %s --ready %s --return-idle-after-ms 1200" % (
            shlex.quote(sys.executable), shlex.quote(str(fake_program)),
            shlex.quote(SESSION), shlex.quote(str(prompt_log)), shlex.quote(str(ready_file)),
        )
        self.client.start_agent(target_pane, "bash -c %s" % shlex.quote(fake_command))
        detected = self.client.wait_for_agent_detected(target_pane, timeout_s=20)
        self.assertEqual(detected.pane_id, target_pane)
        self.assertEqual(self.client.wait_agent(
            target_pane, until=("idle",), timeout_ms=10000).status, "idle")
        self.client.rename_agent(target_pane, target_name)
        self.assertEqual(self.client.get_agent(target_name).pane_id, target_pane)
        self.assertTrue(self._wait_for_path(ready_file, timeout_s=5),
                        "fake pi 未完成初始 idle report-agent 上报")

        real_herdr = shutil.which("herdr", path=os.environ.get("PATH"))
        self.assertTrue(real_herdr, "测试环境找不到原始 herdr 可执行文件")
        cli_bin = Path(self.cwd.name) / "predispatch-bin"
        cli_bin.mkdir()
        a2a_command = cli_bin / "a2a"
        a2a_command.write_text(
            "#!%s\nfrom a2a_codex.cli import main\nraise SystemExit(main())\n" % sys.executable,
            encoding="utf-8",
        )
        a2a_command.chmod(0o755)

        expected_pid_file = Path(self.cwd.name) / "expected-predispatch-broker.pid"
        authorize_kill = Path(self.cwd.name) / "authorize-predispatch-broker-kill"
        ready_marker = Path(self.cwd.name) / "herdr-ready-before-dispatch.json"
        wrapper_events = Path(self.cwd.name) / "predispatch-wrapper-events.jsonl"
        wrapper = cli_bin / "herdr"
        wrapper_source = '''import json, os, signal, subprocess, sys, time
REAL_HERDR = @REAL_HERDR@
SESSION = @SESSION@
TARGET = @TARGET@
EXPECTED_PID = @EXPECTED_PID@
AUTHORIZE = @AUTHORIZE@
MARKER = @MARKER@
EVENTS = @EVENTS@

def record(event):
    with open(EVENTS, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(event) + "\\n")
        stream.flush()
        os.fsync(stream.fileno())

def status_from(value):
    if not isinstance(value, dict):
        return None
    result = value.get("result", value)
    if isinstance(result, dict) and isinstance(result.get("agent"), dict):
        result = result["agent"]
    if isinstance(result, dict):
        return result.get("agent_status") or result.get("status")
    return None

argv = sys.argv[1:]
parent_pid = os.getppid()
expected = None
deadline = time.monotonic() + 5
while time.monotonic() < deadline:
    try:
        expected = int(open(EXPECTED_PID, encoding="ascii").read().strip())
        break
    except (OSError, ValueError):
        time.sleep(0.01)
intercept = (expected == parent_pid and argv[:2] == ["--session", SESSION]
            and argv[2:4] == ["agent", "get"] and len(argv) == 5
            and argv[4] == TARGET)
if intercept:
    completed = subprocess.run([REAL_HERDR, *argv], capture_output=True, text=True)
    try:
        body = json.loads(completed.stdout)
    except (ValueError, TypeError):
        body = None
    status = status_from(body)
    record({"event": "real_agent_get_returned", "parent_pid": parent_pid,
            "target": TARGET, "returncode": completed.returncode, "status": status})
    if completed.returncode == 0 and status in {"idle", "done"}:
        marker_tmp = MARKER + ".tmp"
        with open(marker_tmp, "w", encoding="utf-8") as stream:
            json.dump({"parent_pid": parent_pid, "wrapper_pid": os.getpid(),
                       "target": TARGET, "status": status, "argv": argv}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(marker_tmp, MARKER)
        wait_deadline = time.monotonic() + 20
        while not os.path.exists(AUTHORIZE) and time.monotonic() < wait_deadline:
            if os.getppid() != parent_pid:
                record({"event": "parent_changed_before_authorization", "parent_pid": parent_pid})
                sys.exit(96)
            time.sleep(0.01)
        if not os.path.exists(AUTHORIZE):
            record({"event": "authorization_timeout", "parent_pid": parent_pid})
            sys.exit(97)
        try:
            authorized_pid = int(open(EXPECTED_PID, encoding="ascii").read().strip())
        except (OSError, ValueError):
            record({"event": "invalid_expected_pid", "parent_pid": parent_pid})
            sys.exit(98)
        if authorized_pid != parent_pid or os.getppid() != parent_pid:
            record({"event": "parent_pid_mismatch", "parent_pid": parent_pid,
                    "authorized_pid": authorized_pid, "current_ppid": os.getppid()})
            sys.exit(99)
        record({"event": "sigkill_authorized_parent", "parent_pid": parent_pid})
        os.kill(parent_pid, signal.SIGKILL)
        os._exit(0)
    sys.stdout.write(completed.stdout or "")
    sys.stderr.write(completed.stderr or "")
    sys.exit(completed.returncode)

os.execv(REAL_HERDR, [REAL_HERDR, *argv])
'''
        for placeholder, value in (
            ("@REAL_HERDR@", repr(real_herdr)), ("@SESSION@", repr(SESSION)),
            ("@TARGET@", repr(target_pane)), ("@EXPECTED_PID@", repr(str(expected_pid_file))),
            ("@AUTHORIZE@", repr(str(authorize_kill))), ("@MARKER@", repr(str(ready_marker))),
            ("@EVENTS@", repr(str(wrapper_events))),
        ):
            wrapper_source = wrapper_source.replace(placeholder, value)
        wrapper.write_text("#!%s\n" % sys.executable + wrapper_source, encoding="utf-8")
        wrapper.chmod(0o755)

        self.cli_env = dict(os.environ)
        self.cli_env.update({
            "PATH": str(cli_bin) + os.pathsep + os.environ.get("PATH", ""),
            "A2A_STATE_DIR": str(state_dir),
            "A2A_TOPOLOGY": str(topology_path),
            "HERDR_SESSION": SESSION,
        })
        send_env = dict(self.cli_env)
        send_env.update({
            "A2A_PROJECT_ID": "soc_a", "A2A_ROLE": "dv", "A2A_IP": "uart",
            "HERDR_PANE_ID": source_pane,
        })
        first = json.loads(self._run_a2a("send", "dv_first", env=send_env))
        later = json.loads(self._run_a2a("send", "dv_later", env=send_env))
        self.assertEqual(first["state"], "QUEUED")
        self.assertEqual(later["state"], "QUEUED")
        expected_first = "PREDISPATCH_FIRST::uart::先投递"
        expected_later = "PREDISPATCH_LATER::uart::不得越过队列头"
        self.assertEqual(first["text"], expected_first)
        self.assertEqual(later["text"], expected_later)

        broker_log = Path(self.cwd.name) / "predispatch-broker.log"
        self.broker_output = broker_log.open("w", encoding="utf-8")
        self.broker = subprocess.Popen(
            ["a2a", "broker", "run"], env=self.cli_env,
            cwd=str(Path(__file__).resolve().parents[3]),
            stdout=self.broker_output, stderr=subprocess.STDOUT, text=True,
        )
        crashed_broker = self.broker
        self.addCleanup(self._kill_broker_if_running)
        expected_pid_file.write_text("%s\n" % crashed_broker.pid, encoding="ascii")
        runtime_lock = state_dir / "spool" / "broker-runtime.lock"
        self.assertTrue(self._wait_for_lock(runtime_lock, crashed_broker, timeout_s=10),
                        "Broker 未持有运行锁: %s" % broker_log.read_text(encoding="utf-8"))
        self.assertTrue(self._wait_for_path(ready_marker, timeout_s=15),
                        "wrapper 未在真实 Herdr agent get 返回 READY 后进入注入窗口: %s" %
                        broker_log.read_text(encoding="utf-8"))

        marker = json.loads(ready_marker.read_text(encoding="utf-8"))
        self.assertEqual(marker["parent_pid"], crashed_broker.pid)
        self.assertEqual(marker["target"], target_pane)
        self.assertIn(marker["status"], {"idle", "done"})
        self.assertEqual(marker["argv"][2:5], ["agent", "get", target_pane])
        first_status = json.loads(self._run_a2a("status", first["msg_id"]))
        later_status = json.loads(self._run_a2a("status", later["msg_id"]))
        queue_view = json.loads(self._run_a2a("queue", "sw_uart"))
        self.assertIn(first_status["state"], {"QUEUED", "WAITING_TARGET"}, first_status)
        self.assertEqual(later_status["state"], "QUEUED")
        self.assertTrue(queue_view)
        self.assertEqual(queue_view[0]["head"]["msg_id"], first["msg_id"])
        self.assertEqual(self._read_prompt_events(prompt_log), [],
                         "READY 查询返回后、DISPATCHING 前 fake pi 不应收到 prompt")
        self.assertIsNone(crashed_broker.poll(), "wrapper 等待显式授权时 Broker 已提前退出")

        authorize_kill.write_text("authorize exact test Broker PID only\n", encoding="ascii")
        self.assertEqual(crashed_broker.wait(timeout=10), -signal.SIGKILL,
                         "wrapper 未按授权精确 SIGKILL 测试 Broker")
        self.broker = None
        if not self.broker_output.closed:
            self.broker_output.close()
        events = [json.loads(line) for line in wrapper_events.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([event["event"] for event in events],
                         ["real_agent_get_returned", "sigkill_authorized_parent"])
        self.assertEqual(events[0]["parent_pid"], crashed_broker.pid)
        self.assertEqual(events[0]["status"], marker["status"])
        self.assertEqual(events[1]["parent_pid"], crashed_broker.pid)
        self.assertEqual(self._read_prompt_events(prompt_log), [])
        self.assertIn(json.loads(self._run_a2a("status", first["msg_id"]))["state"],
                      {"QUEUED", "WAITING_TARGET"})
        self.assertEqual(json.loads(self._run_a2a("status", later["msg_id"]))["state"], "QUEUED")

        restarted_log = Path(self.cwd.name) / "predispatch-broker-restarted.log"
        self.broker_output = restarted_log.open("w", encoding="utf-8")
        self.broker = subprocess.Popen(
            ["a2a", "broker", "run"], env=self.cli_env,
            cwd=str(Path(__file__).resolve().parents[3]),
            stdout=self.broker_output, stderr=subprocess.STDOUT, text=True,
        )
        self.addCleanup(self._stop_broker)
        self.assertTrue(self._wait_for_lock(runtime_lock, self.broker, timeout_s=10),
                        "重启 Broker 未持有运行锁: %s" % restarted_log.read_text(encoding="utf-8"))

        recovered_first = None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            recovered_first = json.loads(self._run_a2a("status", first["msg_id"]))
            if recovered_first.get("state") == "DELIVERED":
                break
            time.sleep(0.05)
        self.assertEqual(recovered_first.get("state"), "DELIVERED", recovered_first)
        self.assertEqual(self._read_prompt_events(prompt_log), [{
            "event": "prompt", "prompt": expected_first,
        }], "首消息 DELIVERED 时后续消息不应已越过队列头")
        self.assertIn(json.loads(self._run_a2a("status", later["msg_id"]))["state"],
                      {"QUEUED", "WAITING_TARGET"})

        recovered_later = None
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            recovered_later = json.loads(self._run_a2a("status", later["msg_id"]))
            if recovered_later.get("state") == "DELIVERED":
                break
            time.sleep(0.05)
        self.assertEqual(recovered_later.get("state"), "DELIVERED", recovered_later)
        self.assertEqual(self._read_prompt_events(prompt_log), [
            {"event": "prompt", "prompt": expected_first},
            {"event": "prompt", "prompt": expected_later},
        ], "Broker 重启后 prompt 数量/顺序错误")
        self.assertEqual(json.loads(self._run_a2a("status", first["msg_id"]))["state"], "DELIVERED")
        self.assertEqual(json.loads(self._run_a2a("status", later["msg_id"]))["state"], "DELIVERED")

    @staticmethod
    def _wait_for_path(path: Path, *, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if path.exists():
                return True
            time.sleep(0.05)
        return path.exists()

    @staticmethod
    def _wait_for_prompt(path: Path, expected: str, *, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if path.exists():
                try:
                    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                except (OSError, ValueError):
                    records = []
                if any(record.get("prompt") == expected for record in records):
                    return True
            time.sleep(0.05)
        return False

    @staticmethod
    def _read_prompt_events(path: Path) -> list[dict]:
        if not path.exists():
            return []
        try:
            events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        except (OSError, ValueError):
            return []
        return [event for event in events if event.get("event") == "prompt"]

    @staticmethod
    def _wait_for_lock(path: Path, process: subprocess.Popen, *, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return False
            if path.exists():
                with path.open("a+b") as stream:
                    try:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return True
                    else:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            time.sleep(0.05)
        return False

    def _run_a2a(self, *args: str, env=None) -> str:
        result = subprocess.run(
            ["a2a", *args], env=env or self.cli_env,
            cwd=str(Path(__file__).resolve().parents[3]),
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0,
                         "a2a %s 失败: stdout=%s stderr=%s" %
                         (" ".join(args), result.stdout, result.stderr))
        return result.stdout

    def _stop_broker(self) -> None:
        broker = getattr(self, "broker", None)
        if broker is not None and broker.poll() is None:
            broker.send_signal(signal.SIGINT)
            broker.wait(timeout=15)
        output = getattr(self, "broker_output", None)
        if output is not None and not output.closed:
            output.close()
        if broker is not None:
            self.assertEqual(broker.returncode, 130, "Broker 未按 SIGINT 优雅退出")

    def _kill_broker_if_running(self) -> None:
        broker = getattr(self, "broker", None)
        if broker is not None and broker.poll() is None:
            broker.send_signal(signal.SIGKILL)
            broker.wait(timeout=10)
        output = getattr(self, "broker_output", None)
        if output is not None and not output.closed:
            output.close()


@unittest.skipUnless(ENABLED and SESSION,
                     "设置 A2A_HERDR_INTEGRATION=1 和 HERDR_TEST_SESSION 指定隔离的运行中 session")
class TestRulingProcessRecoveryVM(unittest.TestCase):
    """真实 CLI 子进程/文件系统测试；裁定恢复不调用 Herdr pane 或模型。"""

    def setUp(self) -> None:
        self.cwd = tempfile.TemporaryDirectory(prefix="a2a-ruling-process-it-")
        self.addCleanup(self.cwd.cleanup)
        self.processes: list[tuple[str, subprocess.Popen, object]] = []
        self.state_dir = Path(self.cwd.name) / "state"
        self.audit = AuditLog(self.state_dir / "audit.jsonl")
        self.spool = Spool(self.state_dir / "spool")
        self.env = dict(os.environ)
        self.env.update({
            "A2A_STATE_DIR": str(self.state_dir),
            "HERDR_SESSION": SESSION,
        })

    def _run_a2a(self, *args: str, env=None) -> str:
        result = subprocess.run(
            ["a2a", *args], env=env or self.env,
            cwd=str(Path(__file__).resolve().parents[3]),
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0,
                         "a2a %s 失败: stdout=%s stderr=%s" %
                         (" ".join(args), result.stdout, result.stderr))
        return result.stdout

    @staticmethod
    def _wait_for_path(path: Path, *, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if path.exists():
                return True
            time.sleep(0.025)
        return path.exists()

    def _wait_for_lock(self, path: Path, process: subprocess.Popen,
                       *, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return False
            if path.exists():
                with path.open("a+b") as stream:
                    try:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return True
                    else:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            time.sleep(0.025)
        return False

    def _start_broker(self, env, log_path: Path) -> subprocess.Popen:
        output = log_path.open("w", encoding="utf-8")
        process = subprocess.Popen(
            ["a2a", "broker", "run"], env=env,
            cwd=str(Path(__file__).resolve().parents[3]),
            stdout=output, stderr=subprocess.STDOUT, text=True,
        )
        self.processes.append(("broker", process, output))
        self.addCleanup(self._cleanup_processes)
        return process

    def _stop_broker(self, process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            process.wait(timeout=15)
        self.assertEqual(process.returncode, 130, "Broker 未按 SIGINT 退出")

    def _cleanup_processes(self) -> None:
        for kind, process, output in reversed(self.processes):
            if process.poll() is None:
                process.send_signal(signal.SIGKILL)
                process.wait(timeout=10)
            if not output.closed:
                output.close()

    def test_resolve_process_crash_after_audit_recovers_once_on_broker_startup(self) -> None:
        now = now_iso()
        message = self.spool.enqueue(Message(
            msg_id=new_msg_id(), created_at=now, edge_id="dv_done", src="dv_uart",
            dst="sw_uart", project_id="soc_a", ip_id="uart", session=SESSION,
            text="裁定恢复测试消息", state=QUEUED, topology_revision="test-revision",
            updated_at=now,
        ))
        dispatching = self.spool.update(message.msg_id, state=DISPATCHING,
                                        detail="test setup before prompt")
        self.audit.record({"event": "STATE_TRANSITION", "msg_id": message.msg_id,
                           "dst": message.dst, "state": DISPATCHING,
                           "detail": "test setup before prompt"})
        uncertain = self.spool.update(message.msg_id, state=DELIVERY_UNCERTAIN,
                                      detail="test setup uncertain head")
        self.audit.record({"event": "STATE_TRANSITION", "msg_id": message.msg_id,
                           "dst": message.dst, "state": DELIVERY_UNCERTAIN,
                           "detail": "test setup uncertain head"})
        self.assertEqual(uncertain.queue_seq, dispatching.queue_seq)
        self.assertFalse(self.spool.slot_released(message.dst, message.queue_seq))

        real_bin = Path(self.cwd.name) / "normal-bin"
        real_bin.mkdir()
        normal_a2a = real_bin / "a2a"
        normal_a2a.write_text(
            "#!%s\nfrom a2a_codex.cli import main\nraise SystemExit(main())\n" % sys.executable,
            encoding="utf-8",
        )
        normal_a2a.chmod(0o755)
        normal_env = dict(self.env)
        normal_env["PATH"] = str(real_bin) + os.pathsep + os.environ.get("PATH", "")

        injection_bin = Path(self.cwd.name) / "resolve-injection-bin"
        injection_bin.mkdir()
        marker = Path(self.cwd.name) / "ruling-audit-fsynced.json"
        injection_a2a = injection_bin / "a2a"
        injection_source = '''import json, os, signal, sys
from a2a_codex.audit import AuditLog

MARKER = @MARKER@
original_record = AuditLog.record

def record_then_kill(self, event):
    persisted = original_record(self, event)
    if event.get("event") == "OPERATOR_RULING":
        marker_tmp = MARKER + ".tmp"
        with open(marker_tmp, "w", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "event": persisted}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(marker_tmp, MARKER)
        os.kill(os.getpid(), signal.SIGKILL)
    return persisted

AuditLog.record = record_then_kill
from a2a_codex.cli import main
raise SystemExit(main())
'''.replace("@MARKER@", repr(str(marker)))
        injection_a2a.write_text("#!%s\n" % sys.executable + injection_source,
                                 encoding="utf-8")
        injection_a2a.chmod(0o755)
        injection_env = dict(self.env)
        injection_env["PATH"] = str(injection_bin) + os.pathsep + os.environ.get("PATH", "")

        resolve_log = Path(self.cwd.name) / "resolve-process.log"
        resolve_output = resolve_log.open("w", encoding="utf-8")
        resolve_process = subprocess.Popen(
            ["a2a", "resolve", message.msg_id, "delivered", "--actor", "test-operator",
             "--reason", "process crash recovery test"],
            env=injection_env, cwd=str(Path(__file__).resolve().parents[3]),
            stdout=resolve_output, stderr=subprocess.STDOUT, text=True,
        )
        self.processes.append(("resolve", resolve_process, resolve_output))
        self.addCleanup(self._cleanup_processes)
        self.assertTrue(self._wait_for_path(marker, timeout_s=10),
                        "resolve launcher 未观察到 OPERATOR_RULING fsync marker: %s" %
                        resolve_log.read_text(encoding="utf-8"))
        self.assertEqual(resolve_process.wait(timeout=10), -signal.SIGKILL,
                         "resolve 进程未在 OPERATOR_RULING 落盘后收到 SIGKILL")
        if not resolve_output.closed:
            resolve_output.close()

        marker_record = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(marker_record["pid"], resolve_process.pid,
                         "注入器记录的进程 PID 不是本测试启动的 resolve PID")
        self.assertEqual(marker_record["event"]["event"], "OPERATOR_RULING")
        self.assertEqual(marker_record["event"]["msg_id"], message.msg_id)

        events = self.audit.read(strict=True)
        rulings = [event for event in events if event.get("event") == "OPERATOR_RULING"
                   and event.get("msg_id") == message.msg_id]
        self.assertEqual(len(rulings), 1,
                         "resolve 被杀后未留下且仅留下一个完整 OPERATOR_RULING")
        ruling_id = rulings[0]["ruling_id"]
        self.assertEqual(self.spool.get(message.msg_id).state, DELIVERY_UNCERTAIN,
                         "OPERATOR_RULING 落盘前后不应已有裁定状态副作用")
        self.assertFalse(self.spool.slot_released(message.dst, message.queue_seq),
                         "OPERATOR_RULING 落盘后、恢复前槽位不应放行")
        self.assertFalse(any(event.get("event") in {"RULING_APPLIED", "QUEUE_RELEASED"}
                             and event.get("ruling_id") == ruling_id for event in events),
                         "SIGKILL 前已出现裁定副作用审计事件")

        recovery_log = Path(self.cwd.name) / "broker-recovery-1.log"
        first_broker = self._start_broker(normal_env, recovery_log)
        runtime_lock = self.state_dir / "spool" / "broker-runtime.lock"
        self.assertTrue(self._wait_for_lock(runtime_lock, first_broker, timeout_s=10),
                        "恢复 Broker 未持有锁: %s" % recovery_log.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 10
        recovered = None
        while time.monotonic() < deadline:
            recovered = json.loads(self._run_a2a("status", message.msg_id, env=normal_env))
            if recovered.get("state") == "DELIVERED":
                break
            time.sleep(0.05)
        self.assertEqual(recovered["state"], "DELIVERED",
                         "Broker 启动恢复未补做 delivered 裁定: %r" % recovered)
        self.assertTrue(self.spool.slot_released(message.dst, message.queue_seq))
        self.assertEqual(self._run_a2a("queue", message.dst, env=normal_env).strip(), "[]")

        def assert_single_effects():
            current_events = self.audit.read(strict=True)
            self.assertEqual(sum(event.get("event") == "OPERATOR_RULING"
                                 and event.get("ruling_id") == ruling_id
                                 for event in current_events), 1)
            self.assertEqual(sum(event.get("event") == "RULING_APPLIED"
                                 and event.get("ruling_id") == ruling_id
                                 and event.get("msg_id") == message.msg_id
                                 for event in current_events), 1)
            self.assertEqual(sum(event.get("event") == "QUEUE_RELEASED"
                                 and event.get("ruling_id") == ruling_id
                                 and event.get("msg_id") == message.msg_id
                                 for event in current_events), 1)

        assert_single_effects()
        self._stop_broker(first_broker)

        second_log = Path(self.cwd.name) / "broker-recovery-2.log"
        second_broker = self._start_broker(normal_env, second_log)
        self.assertTrue(self._wait_for_lock(runtime_lock, second_broker, timeout_s=10),
                        "第二次恢复 Broker 未持有锁: %s" % second_log.read_text(encoding="utf-8"))
        time.sleep(0.75)
        self.assertEqual(json.loads(self._run_a2a("status", message.msg_id,
                                                  env=normal_env))["state"], "DELIVERED")
        self.assertTrue(self.spool.slot_released(message.dst, message.queue_seq))
        assert_single_effects()
        self._stop_broker(second_broker)

    def test_resolve_crash_after_delivered_transition_audit_recovers_idempotently(self) -> None:
        now = now_iso()
        message = self.spool.enqueue(Message(
            msg_id=new_msg_id(), created_at=now, edge_id="dv_done", src="dv_uart",
            dst="sw_uart", project_id="soc_a", ip_id="uart", session=SESSION,
            text="裁定状态迁移审计崩溃恢复测试消息", state=QUEUED,
            topology_revision="test-revision", updated_at=now,
        ))
        self.spool.update(message.msg_id, state=DISPATCHING, detail="test setup before prompt")
        self.audit.record({"event": "STATE_TRANSITION", "msg_id": message.msg_id,
                           "dst": message.dst, "state": DISPATCHING,
                           "detail": "test setup before prompt"})
        uncertain = self.spool.update(message.msg_id, state=DELIVERY_UNCERTAIN,
                                      detail="test setup uncertain head")
        self.audit.record({"event": "STATE_TRANSITION", "msg_id": message.msg_id,
                           "dst": message.dst, "state": DELIVERY_UNCERTAIN,
                           "detail": "test setup uncertain head"})
        self.assertEqual(uncertain.state, DELIVERY_UNCERTAIN)
        self.assertFalse(self.spool.slot_released(message.dst, message.queue_seq))

        normal_bin = Path(self.cwd.name) / "transition-normal-bin"
        normal_bin.mkdir()
        normal_a2a = normal_bin / "a2a"
        normal_a2a.write_text(
            "#!%s\nfrom a2a_codex.cli import main\nraise SystemExit(main())\n" % sys.executable,
            encoding="utf-8",
        )
        normal_a2a.chmod(0o755)
        normal_env = dict(self.env)
        normal_env["PATH"] = str(normal_bin) + os.pathsep + os.environ.get("PATH", "")

        injection_bin = Path(self.cwd.name) / "transition-injection-bin"
        injection_bin.mkdir()
        marker = Path(self.cwd.name) / "delivered-transition-fsynced.json"
        injection_a2a = injection_bin / "a2a"
        injection_source = '''import json, os, signal, sys
from a2a_codex.audit import AuditLog

MARKER = @MARKER@
MSG_ID = @MSG_ID@
original_record = AuditLog.record

def record_then_kill(self, event):
    persisted = original_record(self, event)
    if (event.get("event") == "STATE_TRANSITION"
            and event.get("state") == "DELIVERED"
            and event.get("msg_id") == MSG_ID
            and isinstance(event.get("ruling_id"), str)):
        marker_tmp = MARKER + ".tmp"
        with open(marker_tmp, "w", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "event": persisted}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(marker_tmp, MARKER)
        os.kill(os.getpid(), signal.SIGKILL)
    return persisted

AuditLog.record = record_then_kill
from a2a_codex.cli import main
raise SystemExit(main())
'''.replace("@MARKER@", repr(str(marker))).replace("@MSG_ID@", repr(message.msg_id))
        injection_a2a.write_text("#!%s\n" % sys.executable + injection_source,
                                 encoding="utf-8")
        injection_a2a.chmod(0o755)
        injection_env = dict(self.env)
        injection_env["PATH"] = str(injection_bin) + os.pathsep + os.environ.get("PATH", "")

        resolve_log = Path(self.cwd.name) / "transition-resolve-process.log"
        resolve_output = resolve_log.open("w", encoding="utf-8")
        resolve_process = subprocess.Popen(
            ["a2a", "resolve", message.msg_id, "delivered", "--actor", "test-operator",
             "--reason", "crash after delivered transition audit"],
            env=injection_env, cwd=str(Path(__file__).resolve().parents[3]),
            stdout=resolve_output, stderr=subprocess.STDOUT, text=True,
        )
        self.processes.append(("resolve-transition", resolve_process, resolve_output))
        self.addCleanup(self._cleanup_processes)
        self.assertTrue(self._wait_for_path(marker, timeout_s=10),
                        "未在 delivered STATE_TRANSITION fsync 后观察到崩溃 marker: %s" %
                        resolve_log.read_text(encoding="utf-8"))
        self.assertEqual(resolve_process.wait(timeout=10), -signal.SIGKILL,
                         "resolve 未在裁定状态迁移审计落盘后被 SIGKILL")
        if not resolve_output.closed:
            resolve_output.close()

        marker_record = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(marker_record["pid"], resolve_process.pid,
                         "注入器记录的 PID 不是本测试启动的 resolve 进程")
        transition = marker_record["event"]
        self.assertEqual(transition["event"], "STATE_TRANSITION")
        self.assertEqual(transition["state"], DELIVERED)
        self.assertEqual(transition["msg_id"], message.msg_id)
        ruling_id = transition["ruling_id"]

        events = self.audit.read(strict=True)
        self.assertEqual(sum(event.get("event") == "OPERATOR_RULING"
                             and event.get("ruling_id") == ruling_id for event in events), 1)
        self.assertEqual(sum(event.get("event") == "STATE_TRANSITION"
                             and event.get("ruling_id") == ruling_id
                             and event.get("msg_id") == message.msg_id
                             and event.get("state") == DELIVERED for event in events), 1)
        self.assertFalse(any(event.get("event") in {"QUEUE_RELEASED", "RULING_APPLIED"}
                             and event.get("ruling_id") == ruling_id for event in events),
                         "被杀的 resolve 已越过状态迁移审计副作用窗口")
        self.assertEqual(self.spool.get(message.msg_id).state, DELIVERY_UNCERTAIN,
                         "状态迁移审计落盘后、Spool.update 前应仍为 DELIVERY_UNCERTAIN")
        self.assertFalse(self.spool.slot_released(message.dst, message.queue_seq),
                         "状态迁移审计落盘后、恢复前槽位不应放行")

        def assert_effect_counts():
            current_events = self.audit.read(strict=True)
            self.assertEqual(sum(event.get("event") == "STATE_TRANSITION"
                                 and event.get("ruling_id") == ruling_id
                                 and event.get("msg_id") == message.msg_id
                                 and event.get("state") == DELIVERED for event in current_events), 1)
            self.assertEqual(sum(event.get("event") == "QUEUE_RELEASED"
                                 and event.get("ruling_id") == ruling_id
                                 and event.get("msg_id") == message.msg_id for event in current_events), 1)
            self.assertEqual(sum(event.get("event") == "RULING_APPLIED"
                                 and event.get("ruling_id") == ruling_id
                                 and event.get("msg_id") == message.msg_id for event in current_events), 1)

        recovery_log = Path(self.cwd.name) / "transition-broker-recovery-1.log"
        first_broker = self._start_broker(normal_env, recovery_log)
        runtime_lock = self.state_dir / "spool" / "broker-runtime.lock"
        self.assertTrue(self._wait_for_lock(runtime_lock, first_broker, timeout_s=10),
                        "恢复 Broker 未持有锁: %s" % recovery_log.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 10
        recovered = None
        while time.monotonic() < deadline:
            recovered = json.loads(self._run_a2a("status", message.msg_id, env=normal_env))
            if recovered.get("state") == DELIVERED:
                break
            time.sleep(0.05)
        self.assertEqual(recovered["state"], DELIVERED,
                         "Broker 未恢复裁定状态迁移: %r" % recovered)
        self.assertTrue(self.spool.slot_released(message.dst, message.queue_seq))
        self.assertEqual(self._run_a2a("queue", message.dst, env=normal_env).strip(), "[]")
        assert_effect_counts()
        self._stop_broker(first_broker)

        recovery_log_2 = Path(self.cwd.name) / "transition-broker-recovery-2.log"
        second_broker = self._start_broker(normal_env, recovery_log_2)
        self.assertTrue(self._wait_for_lock(runtime_lock, second_broker, timeout_s=10),
                        "第二次恢复 Broker 未持有锁: %s" % recovery_log_2.read_text(encoding="utf-8"))
        time.sleep(0.75)
        self.assertEqual(json.loads(self._run_a2a("status", message.msg_id,
                                                  env=normal_env))["state"], DELIVERED)
        self.assertTrue(self.spool.slot_released(message.dst, message.queue_seq))
        assert_effect_counts()
        self._stop_broker(second_broker)

    def test_resolve_crash_after_slot_release_recovers_queue_audit_idempotently(self) -> None:
        now = now_iso()
        message = self.spool.enqueue(Message(
            msg_id=new_msg_id(), created_at=now, edge_id="dv_done", src="dv_uart",
            dst="sw_uart", project_id="soc_a", ip_id="uart", session=SESSION,
            text="槽位放行后裁定崩溃恢复测试消息", state=QUEUED,
            topology_revision="test-revision", updated_at=now,
        ))
        self.spool.update(message.msg_id, state=DISPATCHING, detail="test setup before prompt")
        self.audit.record({"event": "STATE_TRANSITION", "msg_id": message.msg_id,
                           "dst": message.dst, "state": DISPATCHING,
                           "detail": "test setup before prompt"})
        self.spool.update(message.msg_id, state=DELIVERY_UNCERTAIN,
                          detail="test setup uncertain head")
        self.audit.record({"event": "STATE_TRANSITION", "msg_id": message.msg_id,
                           "dst": message.dst, "state": DELIVERY_UNCERTAIN,
                           "detail": "test setup uncertain head"})
        self.assertFalse(self.spool.slot_released(message.dst, message.queue_seq))

        normal_bin = Path(self.cwd.name) / "slot-release-normal-bin"
        normal_bin.mkdir()
        normal_a2a = normal_bin / "a2a"
        normal_a2a.write_text(
            "#!%s\nfrom a2a_codex.cli import main\nraise SystemExit(main())\n" % sys.executable,
            encoding="utf-8",
        )
        normal_a2a.chmod(0o755)
        normal_env = dict(self.env)
        normal_env["PATH"] = str(normal_bin) + os.pathsep + os.environ.get("PATH", "")

        injection_bin = Path(self.cwd.name) / "slot-release-injection-bin"
        injection_bin.mkdir()
        marker = Path(self.cwd.name) / "slot-release-persisted.json"
        injection_a2a = injection_bin / "a2a"
        injection_source = '''import json, os, signal, sys
from a2a_codex.spool import Spool

MARKER = @MARKER@
original_release = Spool._release_slot_unlocked

def release_then_kill(self, dst, queue_seq, **kwargs):
    released = original_release(self, dst, queue_seq, **kwargs)
    if dst == @DST@ and queue_seq == @QUEUE_SEQ@ and kwargs.get("ruling_id"):
        marker_tmp = MARKER + ".tmp"
        with open(marker_tmp, "w", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "dst": dst, "queue_seq": queue_seq,
                       "ruling_id": kwargs["ruling_id"], "released": released}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(marker_tmp, MARKER)
        os.kill(os.getpid(), signal.SIGKILL)
    return released

Spool._release_slot_unlocked = release_then_kill
from a2a_codex.cli import main
raise SystemExit(main())
'''.replace("@MARKER@", repr(str(marker)))
        injection_source = injection_source.replace("@DST@", repr(message.dst))
        injection_source = injection_source.replace("@QUEUE_SEQ@", repr(message.queue_seq))
        injection_a2a.write_text("#!%s\n" % sys.executable + injection_source,
                                 encoding="utf-8")
        injection_a2a.chmod(0o755)
        injection_env = dict(self.env)
        injection_env["PATH"] = str(injection_bin) + os.pathsep + os.environ.get("PATH", "")

        resolve_log = Path(self.cwd.name) / "slot-release-resolve-process.log"
        resolve_output = resolve_log.open("w", encoding="utf-8")
        resolve_process = subprocess.Popen(
            ["a2a", "resolve", message.msg_id, "delivered", "--actor", "test-operator",
             "--reason", "crash after durable slot release"],
            env=injection_env, cwd=str(Path(__file__).resolve().parents[3]),
            stdout=resolve_output, stderr=subprocess.STDOUT, text=True,
        )
        self.processes.append(("resolve-slot-release", resolve_process, resolve_output))
        self.addCleanup(self._cleanup_processes)
        self.assertTrue(self._wait_for_path(marker, timeout_s=10),
                        "未在槽位持久放行后观察到崩溃 marker: %s" %
                        resolve_log.read_text(encoding="utf-8"))
        self.assertEqual(resolve_process.wait(timeout=10), -signal.SIGKILL,
                         "resolve 未在槽位持久放行后收到 SIGKILL")
        if not resolve_output.closed:
            resolve_output.close()

        marker_record = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(marker_record["pid"], resolve_process.pid,
                         "注入器记录的 PID 不是本测试启动的 resolve 进程")
        self.assertEqual(marker_record["dst"], message.dst)
        self.assertEqual(marker_record["queue_seq"], message.queue_seq)
        self.assertTrue(marker_record["released"], "测试未命中首次持久放行")
        ruling_id = marker_record["ruling_id"]

        events = self.audit.read(strict=True)
        self.assertEqual(self.spool.get(message.msg_id).state, DELIVERED,
                         "槽位放行之前，消息状态迁移应已持久化为 DELIVERED")
        self.assertTrue(self.spool.slot_released(message.dst, message.queue_seq),
                        "注入点应位于槽位持久放行之后")
        self.assertFalse(any(event.get("event") in {"QUEUE_RELEASED", "RULING_APPLIED"}
                             and event.get("ruling_id") == ruling_id for event in events),
                         "SIGKILL 前不应已有后续裁定审计副作用")

        def assert_effects_once():
            current_events = self.audit.read(strict=True)
            self.assertEqual(sum(event.get("event") == "STATE_TRANSITION"
                                 and event.get("ruling_id") == ruling_id
                                 and event.get("msg_id") == message.msg_id
                                 and event.get("state") == DELIVERED
                                 for event in current_events), 1)
            self.assertEqual(sum(event.get("event") == "QUEUE_RELEASED"
                                 and event.get("ruling_id") == ruling_id
                                 and event.get("msg_id") == message.msg_id
                                 for event in current_events), 1)
            self.assertEqual(sum(event.get("event") == "RULING_APPLIED"
                                 and event.get("ruling_id") == ruling_id
                                 and event.get("msg_id") == message.msg_id
                                 for event in current_events), 1)

        recovery_log = Path(self.cwd.name) / "slot-release-broker-recovery-1.log"
        first_broker = self._start_broker(normal_env, recovery_log)
        runtime_lock = self.state_dir / "spool" / "broker-runtime.lock"
        self.assertTrue(self._wait_for_lock(runtime_lock, first_broker, timeout_s=10),
                        "恢复 Broker 未持有锁: %s" % recovery_log.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current_events = self.audit.read(strict=True)
            if any(event.get("event") == "RULING_APPLIED"
                   and event.get("ruling_id") == ruling_id for event in current_events):
                break
            time.sleep(0.05)
        self.assertTrue(self.spool.slot_released(message.dst, message.queue_seq))
        self.assertEqual(self.spool.get(message.msg_id).state, DELIVERED)
        self.assertIsNone(self.spool.queue_head(message.dst))
        assert_effects_once()
        self._stop_broker(first_broker)

        recovery_log_2 = Path(self.cwd.name) / "slot-release-broker-recovery-2.log"
        second_broker = self._start_broker(normal_env, recovery_log_2)
        self.assertTrue(self._wait_for_lock(runtime_lock, second_broker, timeout_s=10),
                        "第二次恢复 Broker 未持有锁: %s" %
                        recovery_log_2.read_text(encoding="utf-8"))
        time.sleep(0.75)
        self.assertTrue(self.spool.slot_released(message.dst, message.queue_seq))
        self.assertEqual(self.spool.get(message.msg_id).state, DELIVERED)
        assert_effects_once()
        self._stop_broker(second_broker)

    def test_terminal_retry_crash_after_enqueue_recovers_without_duplicate(self) -> None:
        now = now_iso()
        message = self.spool.enqueue(Message(
            msg_id=new_msg_id(), created_at=now, edge_id="dv_done", src="dv_uart",
            dst="sw_uart", project_id="soc_a", ip_id="uart", session=SESSION,
            text="终态重试入队崩溃恢复测试消息", state=QUEUED,
            topology_revision="test-revision", updated_at=now,
        ))
        failed = self.spool.update(message.msg_id, state="TARGET_BLOCKED",
                                   detail="test setup deterministic terminal failure")
        self.audit.record({"event": "STATE_TRANSITION", "msg_id": message.msg_id,
                           "dst": message.dst, "state": failed.state,
                           "detail": "test setup deterministic terminal failure"})
        self.assertFalse(self.spool.slot_released(message.dst, message.queue_seq))

        normal_bin = Path(self.cwd.name) / "retry-normal-bin"
        normal_bin.mkdir()
        normal_a2a = normal_bin / "a2a"
        normal_a2a.write_text(
            "#!%s\nfrom a2a_codex.cli import main\nraise SystemExit(main())\n" % sys.executable,
            encoding="utf-8",
        )
        normal_a2a.chmod(0o755)
        normal_env = dict(self.env)
        normal_env["PATH"] = str(normal_bin) + os.pathsep + os.environ.get("PATH", "")

        injection_bin = Path(self.cwd.name) / "retry-injection-bin"
        injection_bin.mkdir()
        marker = Path(self.cwd.name) / "retry-message-enqueued.json"
        injection_a2a = injection_bin / "a2a"
        injection_source = '''import json, os, signal, sys
from a2a_codex.spool import Spool

MARKER = @MARKER@
ORIGINAL_MSG_ID = @MSG_ID@
original_enqueue = Spool.enqueue_retry

def enqueue_then_kill(self, message, *, original_msg_id):
    result = original_enqueue(self, message, original_msg_id=original_msg_id)
    if original_msg_id == ORIGINAL_MSG_ID:
        marker_tmp = MARKER + ".tmp"
        with open(marker_tmp, "w", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "original_msg_id": original_msg_id,
                       "retry_msg_id": result.msg_id, "queue_seq": result.queue_seq,
                       "retry_of": result.retry_of}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(marker_tmp, MARKER)
        os.kill(os.getpid(), signal.SIGKILL)
    return result

Spool.enqueue_retry = enqueue_then_kill
from a2a_codex.cli import main
raise SystemExit(main())
'''.replace("@MARKER@", repr(str(marker))).replace("@MSG_ID@", repr(message.msg_id))
        injection_a2a.write_text("#!%s\n" % sys.executable + injection_source,
                                 encoding="utf-8")
        injection_a2a.chmod(0o755)
        injection_env = dict(self.env)
        injection_env["PATH"] = str(injection_bin) + os.pathsep + os.environ.get("PATH", "")

        resolve_log = Path(self.cwd.name) / "retry-resolve-process.log"
        resolve_output = resolve_log.open("w", encoding="utf-8")
        resolve_process = subprocess.Popen(
            ["a2a", "resolve", message.msg_id, "retry", "--actor", "test-operator",
             "--reason", "crash after durable retry enqueue"],
            env=injection_env, cwd=str(Path(__file__).resolve().parents[3]),
            stdout=resolve_output, stderr=subprocess.STDOUT, text=True,
        )
        self.processes.append(("resolve-terminal-retry", resolve_process, resolve_output))
        self.addCleanup(self._cleanup_processes)
        self.assertTrue(self._wait_for_path(marker, timeout_s=10),
                        "未在重试消息持久入队后观察到崩溃 marker: %s" %
                        resolve_log.read_text(encoding="utf-8"))
        self.assertEqual(resolve_process.wait(timeout=10), -signal.SIGKILL,
                         "resolve 未在重试消息持久入队后收到 SIGKILL")
        if not resolve_output.closed:
            resolve_output.close()

        marker_record = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(marker_record["pid"], resolve_process.pid,
                         "注入器记录的 PID 不是本测试启动的 resolve 进程")
        self.assertEqual(marker_record["original_msg_id"], message.msg_id)
        self.assertEqual(marker_record["retry_of"], message.msg_id)
        self.assertEqual(marker_record["queue_seq"], message.queue_seq)
        retry_msg_id = marker_record["retry_msg_id"]

        events = self.audit.read(strict=True)
        rulings = [event for event in events if event.get("event") == "OPERATOR_RULING"
                   and event.get("msg_id") == message.msg_id
                   and event.get("ruling") == "retry_terminal"]
        self.assertEqual(len(rulings), 1)
        ruling_id = rulings[0]["ruling_id"]
        self.assertEqual(rulings[0]["retry_msg_id"], retry_msg_id)
        self.assertFalse(any(event.get("event") in {"RETRY_ENQUEUED", "RULING_APPLIED"}
                             and event.get("ruling_id") == ruling_id for event in events),
                         "SIGKILL 前不应已有重试入队/裁定完成审计")
        self.assertEqual(self.spool.get(message.msg_id).state, failed.state)
        retry = self.spool.get(retry_msg_id)
        self.assertEqual(retry.retry_of, message.msg_id)
        self.assertEqual(retry.queue_seq, message.queue_seq)

        recovery_log = Path(self.cwd.name) / "retry-broker-recovery.log"
        broker = self._start_broker(normal_env, recovery_log)
        runtime_lock = self.state_dir / "spool" / "broker-runtime.lock"
        self.assertTrue(self._wait_for_lock(runtime_lock, broker, timeout_s=10),
                        "恢复 Broker 未持有锁: %s" % recovery_log.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current_events = self.audit.read(strict=True)
            if any(event.get("event") == "RULING_APPLIED"
                   and event.get("ruling_id") == ruling_id for event in current_events):
                break
            time.sleep(0.05)

        current_events = self.audit.read(strict=True)
        self.assertEqual(sum(event.get("event") == "RETRY_ENQUEUED"
                             and event.get("ruling_id") == ruling_id
                             and event.get("msg_id") == retry_msg_id for event in current_events), 1)
        self.assertEqual(sum(event.get("event") == "RULING_APPLIED"
                             and event.get("ruling_id") == ruling_id
                             for event in current_events), 1)
        retry_messages = [item for item in self.spool.pending() + self.spool.done()
                          if item.retry_of == message.msg_id]
        self.assertEqual(len(retry_messages), 1,
                         "恢复不得为同一 ruling 创建重复重试消息")
        self.assertEqual(retry_messages[0].msg_id, retry_msg_id,
                         "恢复必须复用 OPERATOR_RULING 中固定的 retry_msg_id")
        self.assertEqual(retry_messages[0].queue_seq, message.queue_seq)
        self._stop_broker(broker)

        second_log = Path(self.cwd.name) / "retry-broker-recovery-2.log"
        second_broker = self._start_broker(normal_env, second_log)
        self.assertTrue(self._wait_for_lock(runtime_lock, second_broker, timeout_s=10),
                        "第二次恢复 Broker 未持有锁: %s" % second_log.read_text(encoding="utf-8"))
        time.sleep(0.75)
        current_events = self.audit.read(strict=True)
        self.assertEqual(sum(event.get("event") == "RETRY_ENQUEUED"
                             and event.get("ruling_id") == ruling_id
                             and event.get("msg_id") == retry_msg_id for event in current_events), 1)
        self.assertEqual(sum(event.get("event") == "RULING_APPLIED"
                             and event.get("ruling_id") == ruling_id
                             for event in current_events), 1)
        retry_messages = [item for item in self.spool.pending() + self.spool.done()
                          if item.retry_of == message.msg_id]
        self.assertEqual(len(retry_messages), 1)
        self.assertEqual(retry_messages[0].msg_id, retry_msg_id)
        self._stop_broker(second_broker)

    def test_resolve_crash_after_queue_released_audit_recovers_applied_once(self) -> None:
        now = now_iso()
        message = self.spool.enqueue(Message(
            msg_id=new_msg_id(), created_at=now, edge_id="dv_done", src="dv_uart",
            dst="sw_uart", project_id="soc_a", ip_id="uart", session=SESSION,
            text="QUEUE_RELEASED 审计后裁定崩溃恢复测试消息", state=QUEUED,
            topology_revision="test-revision", updated_at=now,
        ))
        self.spool.update(message.msg_id, state=DISPATCHING, detail="test setup before prompt")
        self.audit.record({"event": "STATE_TRANSITION", "msg_id": message.msg_id,
                           "dst": message.dst, "state": DISPATCHING,
                           "detail": "test setup before prompt"})
        self.spool.update(message.msg_id, state=DELIVERY_UNCERTAIN,
                          detail="test setup uncertain head")
        self.audit.record({"event": "STATE_TRANSITION", "msg_id": message.msg_id,
                           "dst": message.dst, "state": DELIVERY_UNCERTAIN,
                           "detail": "test setup uncertain head"})

        normal_bin = Path(self.cwd.name) / "queue-audit-normal-bin"
        normal_bin.mkdir()
        normal_a2a = normal_bin / "a2a"
        normal_a2a.write_text(
            "#!%s\nfrom a2a_codex.cli import main\nraise SystemExit(main())\n" % sys.executable,
            encoding="utf-8",
        )
        normal_a2a.chmod(0o755)
        normal_env = dict(self.env)
        normal_env["PATH"] = str(normal_bin) + os.pathsep + os.environ.get("PATH", "")

        injection_bin = Path(self.cwd.name) / "queue-audit-injection-bin"
        injection_bin.mkdir()
        marker = Path(self.cwd.name) / "queue-released-audit-fsynced.json"
        injection_a2a = injection_bin / "a2a"
        injection_source = '''import json, os, signal, sys
from a2a_codex.audit import AuditLog

MARKER = @MARKER@
MSG_ID = @MSG_ID@
original_record = AuditLog.record

def record_then_kill(self, event):
    persisted = original_record(self, event)
    if (event.get("event") == "QUEUE_RELEASED"
            and event.get("msg_id") == MSG_ID
            and isinstance(event.get("ruling_id"), str)):
        marker_tmp = MARKER + ".tmp"
        with open(marker_tmp, "w", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "event": persisted}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(marker_tmp, MARKER)
        os.kill(os.getpid(), signal.SIGKILL)
    return persisted

AuditLog.record = record_then_kill
from a2a_codex.cli import main
raise SystemExit(main())
'''.replace("@MARKER@", repr(str(marker))).replace("@MSG_ID@", repr(message.msg_id))
        injection_a2a.write_text("#!%s\n" % sys.executable + injection_source,
                                 encoding="utf-8")
        injection_a2a.chmod(0o755)
        injection_env = dict(self.env)
        injection_env["PATH"] = str(injection_bin) + os.pathsep + os.environ.get("PATH", "")

        resolve_log = Path(self.cwd.name) / "queue-audit-resolve-process.log"
        resolve_output = resolve_log.open("w", encoding="utf-8")
        resolve_process = subprocess.Popen(
            ["a2a", "resolve", message.msg_id, "delivered", "--actor", "test-operator",
             "--reason", "crash after durable QUEUE_RELEASED audit"],
            env=injection_env, cwd=str(Path(__file__).resolve().parents[3]),
            stdout=resolve_output, stderr=subprocess.STDOUT, text=True,
        )
        self.processes.append(("resolve-queue-audit", resolve_process, resolve_output))
        self.addCleanup(self._cleanup_processes)
        self.assertTrue(self._wait_for_path(marker, timeout_s=10),
                        "未在 QUEUE_RELEASED fsync 后观察到崩溃 marker: %s" %
                        resolve_log.read_text(encoding="utf-8"))
        self.assertEqual(resolve_process.wait(timeout=10), -signal.SIGKILL,
                         "resolve 未在 QUEUE_RELEASED 审计落盘后收到 SIGKILL")
        if not resolve_output.closed:
            resolve_output.close()

        marker_record = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(marker_record["pid"], resolve_process.pid)
        released_event = marker_record["event"]
        self.assertEqual(released_event["event"], "QUEUE_RELEASED")
        self.assertEqual(released_event["msg_id"], message.msg_id)
        ruling_id = released_event["ruling_id"]
        events = self.audit.read(strict=True)
        self.assertEqual(self.spool.get(message.msg_id).state, DELIVERED)
        self.assertTrue(self.spool.slot_released(message.dst, message.queue_seq))
        self.assertEqual(sum(event.get("event") == "QUEUE_RELEASED"
                             and event.get("ruling_id") == ruling_id
                             and event.get("msg_id") == message.msg_id for event in events), 1)
        self.assertFalse(any(event.get("event") == "RULING_APPLIED"
                             and event.get("ruling_id") == ruling_id for event in events),
                         "崩溃必须发生在 RULING_APPLIED 之前")

        def assert_ruling_applied_once():
            current_events = self.audit.read(strict=True)
            self.assertEqual(sum(event.get("event") == "QUEUE_RELEASED"
                                 and event.get("ruling_id") == ruling_id
                                 and event.get("msg_id") == message.msg_id
                                 for event in current_events), 1)
            self.assertEqual(sum(event.get("event") == "RULING_APPLIED"
                                 and event.get("ruling_id") == ruling_id
                                 and event.get("msg_id") == message.msg_id
                                 for event in current_events), 1)

        recovery_log = Path(self.cwd.name) / "queue-audit-broker-recovery-1.log"
        first_broker = self._start_broker(normal_env, recovery_log)
        runtime_lock = self.state_dir / "spool" / "broker-runtime.lock"
        self.assertTrue(self._wait_for_lock(runtime_lock, first_broker, timeout_s=10),
                        "恢复 Broker 未持有锁: %s" % recovery_log.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if any(event.get("event") == "RULING_APPLIED"
                   and event.get("ruling_id") == ruling_id
                   for event in self.audit.read(strict=True)):
                break
            time.sleep(0.05)
        assert_ruling_applied_once()
        self.assertEqual(self.spool.get(message.msg_id).state, DELIVERED)
        self.assertTrue(self.spool.slot_released(message.dst, message.queue_seq))
        self._stop_broker(first_broker)

        recovery_log_2 = Path(self.cwd.name) / "queue-audit-broker-recovery-2.log"
        second_broker = self._start_broker(normal_env, recovery_log_2)
        self.assertTrue(self._wait_for_lock(runtime_lock, second_broker, timeout_s=10),
                        "第二次恢复 Broker 未持有锁: %s" %
                        recovery_log_2.read_text(encoding="utf-8"))
        time.sleep(0.75)
        assert_ruling_applied_once()
        self.assertEqual(self.spool.get(message.msg_id).state, DELIVERED)
        self.assertTrue(self.spool.slot_released(message.dst, message.queue_seq))
        self._stop_broker(second_broker)

    def test_terminal_retry_crash_after_retry_audit_recovers_applied_once(self) -> None:
        now = now_iso()
        message = self.spool.enqueue(Message(
            msg_id=new_msg_id(), created_at=now, edge_id="dv_done", src="dv_uart",
            dst="sw_uart", project_id="soc_a", ip_id="uart", session=SESSION,
            text="RETRY_ENQUEUED 审计后裁定崩溃恢复测试消息", state=QUEUED,
            topology_revision="test-revision", updated_at=now,
        ))
        failed = self.spool.update(message.msg_id, state="TARGET_BLOCKED",
                                   detail="test setup deterministic terminal failure")
        self.audit.record({"event": "STATE_TRANSITION", "msg_id": message.msg_id,
                           "dst": message.dst, "state": failed.state,
                           "detail": "test setup deterministic terminal failure"})

        normal_bin = Path(self.cwd.name) / "retry-audit-normal-bin"
        normal_bin.mkdir()
        normal_a2a = normal_bin / "a2a"
        normal_a2a.write_text(
            "#!%s\nfrom a2a_codex.cli import main\nraise SystemExit(main())\n" % sys.executable,
            encoding="utf-8",
        )
        normal_a2a.chmod(0o755)
        normal_env = dict(self.env)
        normal_env["PATH"] = str(normal_bin) + os.pathsep + os.environ.get("PATH", "")

        injection_bin = Path(self.cwd.name) / "retry-audit-injection-bin"
        injection_bin.mkdir()
        marker = Path(self.cwd.name) / "retry-enqueued-audit-fsynced.json"
        injection_a2a = injection_bin / "a2a"
        injection_source = '''import json, os, signal, sys
from a2a_codex.audit import AuditLog

MARKER = @MARKER@
ORIGINAL_MSG_ID = @MSG_ID@
original_record = AuditLog.record

def record_then_kill(self, event):
    persisted = original_record(self, event)
    if (event.get("event") == "RETRY_ENQUEUED"
            and event.get("retry_of") == ORIGINAL_MSG_ID
            and isinstance(event.get("ruling_id"), str)):
        marker_tmp = MARKER + ".tmp"
        with open(marker_tmp, "w", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "event": persisted}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(marker_tmp, MARKER)
        os.kill(os.getpid(), signal.SIGKILL)
    return persisted

AuditLog.record = record_then_kill
from a2a_codex.cli import main
raise SystemExit(main())
'''.replace("@MARKER@", repr(str(marker))).replace("@MSG_ID@", repr(message.msg_id))
        injection_a2a.write_text("#!%s\n" % sys.executable + injection_source,
                                 encoding="utf-8")
        injection_a2a.chmod(0o755)
        injection_env = dict(self.env)
        injection_env["PATH"] = str(injection_bin) + os.pathsep + os.environ.get("PATH", "")

        resolve_log = Path(self.cwd.name) / "retry-audit-resolve-process.log"
        resolve_output = resolve_log.open("w", encoding="utf-8")
        resolve_process = subprocess.Popen(
            ["a2a", "resolve", message.msg_id, "retry", "--actor", "test-operator",
             "--reason", "crash after durable RETRY_ENQUEUED audit"],
            env=injection_env, cwd=str(Path(__file__).resolve().parents[3]),
            stdout=resolve_output, stderr=subprocess.STDOUT, text=True,
        )
        self.processes.append(("resolve-retry-audit", resolve_process, resolve_output))
        self.addCleanup(self._cleanup_processes)
        self.assertTrue(self._wait_for_path(marker, timeout_s=10),
                        "未在 RETRY_ENQUEUED fsync 后观察到崩溃 marker: %s" %
                        resolve_log.read_text(encoding="utf-8"))
        self.assertEqual(resolve_process.wait(timeout=10), -signal.SIGKILL,
                         "resolve 未在 RETRY_ENQUEUED 审计落盘后收到 SIGKILL")
        if not resolve_output.closed:
            resolve_output.close()

        marker_record = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(marker_record["pid"], resolve_process.pid)
        retry_event = marker_record["event"]
        self.assertEqual(retry_event["event"], "RETRY_ENQUEUED")
        self.assertEqual(retry_event["retry_of"], message.msg_id)
        retry_msg_id = retry_event["msg_id"]
        ruling_id = retry_event["ruling_id"]
        events = self.audit.read(strict=True)
        self.assertEqual(sum(event.get("event") == "RETRY_ENQUEUED"
                             and event.get("ruling_id") == ruling_id
                             and event.get("msg_id") == retry_msg_id for event in events), 1)
        self.assertFalse(any(event.get("event") == "RULING_APPLIED"
                             and event.get("ruling_id") == ruling_id for event in events),
                         "崩溃必须发生在 RULING_APPLIED 之前")
        retry = self.spool.get(retry_msg_id)
        self.assertEqual(retry.retry_of, message.msg_id)
        self.assertEqual(retry.queue_seq, message.queue_seq)

        def assert_retry_applied_once():
            current_events = self.audit.read(strict=True)
            self.assertEqual(sum(event.get("event") == "RETRY_ENQUEUED"
                                 and event.get("ruling_id") == ruling_id
                                 and event.get("msg_id") == retry_msg_id
                                 for event in current_events), 1)
            self.assertEqual(sum(event.get("event") == "RULING_APPLIED"
                                 and event.get("ruling_id") == ruling_id
                                 for event in current_events), 1)
            retry_messages = [item for item in self.spool.pending() + self.spool.done()
                              if item.retry_of == message.msg_id]
            self.assertEqual(len(retry_messages), 1)
            self.assertEqual(retry_messages[0].msg_id, retry_msg_id)
            self.assertEqual(retry_messages[0].queue_seq, message.queue_seq)

        recovery_log = Path(self.cwd.name) / "retry-audit-broker-recovery-1.log"
        first_broker = self._start_broker(normal_env, recovery_log)
        runtime_lock = self.state_dir / "spool" / "broker-runtime.lock"
        self.assertTrue(self._wait_for_lock(runtime_lock, first_broker, timeout_s=10),
                        "恢复 Broker 未持有锁: %s" % recovery_log.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if any(event.get("event") == "RULING_APPLIED"
                   and event.get("ruling_id") == ruling_id
                   for event in self.audit.read(strict=True)):
                break
            time.sleep(0.05)
        assert_retry_applied_once()
        self._stop_broker(first_broker)

        recovery_log_2 = Path(self.cwd.name) / "retry-audit-broker-recovery-2.log"
        second_broker = self._start_broker(normal_env, recovery_log_2)
        self.assertTrue(self._wait_for_lock(runtime_lock, second_broker, timeout_s=10),
                        "第二次恢复 Broker 未持有锁: %s" %
                        recovery_log_2.read_text(encoding="utf-8"))
        time.sleep(0.75)
        assert_retry_applied_once()
        self._stop_broker(second_broker)

    def test_abandon_crash_after_failed_state_persisted_recovers_slot_release(self) -> None:
        now = now_iso()
        message = self.spool.enqueue(Message(
            msg_id=new_msg_id(), created_at=now, edge_id="dv_done", src="dv_uart",
            dst="sw_uart", project_id="soc_a", ip_id="uart", session=SESSION,
            text="操作员放弃后 Spool 状态已写入的崩溃恢复测试消息", state=QUEUED,
            topology_revision="test-revision", updated_at=now,
        ))
        self.spool.update(message.msg_id, state=DISPATCHING, detail="test setup before prompt")
        self.audit.record({"event": "STATE_TRANSITION", "msg_id": message.msg_id,
                           "dst": message.dst, "state": DISPATCHING,
                           "detail": "test setup before prompt"})
        self.spool.update(message.msg_id, state=DELIVERY_UNCERTAIN,
                          detail="test setup uncertain head")
        self.audit.record({"event": "STATE_TRANSITION", "msg_id": message.msg_id,
                           "dst": message.dst, "state": DELIVERY_UNCERTAIN,
                           "detail": "test setup uncertain head"})
        self.assertFalse(self.spool.slot_released(message.dst, message.queue_seq))

        normal_bin = Path(self.cwd.name) / "abandon-normal-bin"
        normal_bin.mkdir()
        normal_a2a = normal_bin / "a2a"
        normal_a2a.write_text(
            "#!%s\nfrom a2a_codex.cli import main\nraise SystemExit(main())\n" % sys.executable,
            encoding="utf-8",
        )
        normal_a2a.chmod(0o755)
        normal_env = dict(self.env)
        normal_env["PATH"] = str(normal_bin) + os.pathsep + os.environ.get("PATH", "")

        injection_bin = Path(self.cwd.name) / "abandon-injection-bin"
        injection_bin.mkdir()
        marker = Path(self.cwd.name) / "abandon-failed-state-persisted.json"
        injection_a2a = injection_bin / "a2a"
        injection_source = '''import json, os, signal, sys
from a2a_codex.spool import Spool

MARKER = @MARKER@
MSG_ID = @MSG_ID@
original_update = Spool.update

def update_then_kill(self, msg_id, **kwargs):
    persisted = original_update(self, msg_id, **kwargs)
    if msg_id == MSG_ID and kwargs.get("state") == "FAILED" and kwargs.get("ruling_id"):
        marker_tmp = MARKER + ".tmp"
        with open(marker_tmp, "w", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "msg_id": msg_id,
                       "state": persisted.state, "ruling_id": kwargs["ruling_id"]}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(marker_tmp, MARKER)
        os.kill(os.getpid(), signal.SIGKILL)
    return persisted

Spool.update = update_then_kill
from a2a_codex.cli import main
raise SystemExit(main())
'''.replace("@MARKER@", repr(str(marker))).replace("@MSG_ID@", repr(message.msg_id))
        injection_a2a.write_text("#!%s\n" % sys.executable + injection_source,
                                 encoding="utf-8")
        injection_a2a.chmod(0o755)
        injection_env = dict(self.env)
        injection_env["PATH"] = str(injection_bin) + os.pathsep + os.environ.get("PATH", "")

        resolve_log = Path(self.cwd.name) / "abandon-resolve-process.log"
        resolve_output = resolve_log.open("w", encoding="utf-8")
        resolve_process = subprocess.Popen(
            ["a2a", "resolve", message.msg_id, "abandon", "--actor", "test-operator",
             "--reason", "crash after FAILED Spool update"],
            env=injection_env, cwd=str(Path(__file__).resolve().parents[3]),
            stdout=resolve_output, stderr=subprocess.STDOUT, text=True,
        )
        self.processes.append(("resolve-abandon", resolve_process, resolve_output))
        self.addCleanup(self._cleanup_processes)
        self.assertTrue(self._wait_for_path(marker, timeout_s=10),
                        "未在 FAILED 状态持久化后观察到崩溃 marker: %s" %
                        resolve_log.read_text(encoding="utf-8"))
        self.assertEqual(resolve_process.wait(timeout=10), -signal.SIGKILL,
                         "resolve 未在 FAILED Spool 更新后收到 SIGKILL")
        if not resolve_output.closed:
            resolve_output.close()

        marker_record = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(marker_record["pid"], resolve_process.pid)
        self.assertEqual(marker_record["msg_id"], message.msg_id)
        self.assertEqual(marker_record["state"], "FAILED")
        ruling_id = marker_record["ruling_id"]
        events = self.audit.read(strict=True)
        self.assertEqual(self.spool.get(message.msg_id).state, "FAILED")
        self.assertFalse(self.spool.slot_released(message.dst, message.queue_seq),
                         "该崩溃窗口应发生在 FAILED 持久化后、槽位放行前")
        self.assertFalse(any(event.get("event") in {"QUEUE_RELEASED", "RULING_APPLIED"}
                             and event.get("ruling_id") == ruling_id for event in events))

        def assert_abandon_effects_once():
            current_events = self.audit.read(strict=True)
            self.assertEqual(sum(event.get("event") == "STATE_TRANSITION"
                                 and event.get("ruling_id") == ruling_id
                                 and event.get("msg_id") == message.msg_id
                                 and event.get("state") == "FAILED"
                                 for event in current_events), 1)
            self.assertEqual(sum(event.get("event") == "QUEUE_RELEASED"
                                 and event.get("ruling_id") == ruling_id
                                 and event.get("msg_id") == message.msg_id
                                 for event in current_events), 1)
            self.assertEqual(sum(event.get("event") == "RULING_APPLIED"
                                 and event.get("ruling_id") == ruling_id
                                 for event in current_events), 1)

        recovery_log = Path(self.cwd.name) / "abandon-broker-recovery-1.log"
        first_broker = self._start_broker(normal_env, recovery_log)
        runtime_lock = self.state_dir / "spool" / "broker-runtime.lock"
        self.assertTrue(self._wait_for_lock(runtime_lock, first_broker, timeout_s=10),
                        "恢复 Broker 未持有锁: %s" % recovery_log.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if any(event.get("event") == "RULING_APPLIED"
                   and event.get("ruling_id") == ruling_id
                   for event in self.audit.read(strict=True)):
                break
            time.sleep(0.05)
        assert_abandon_effects_once()
        self.assertEqual(self.spool.get(message.msg_id).state, "FAILED")
        self.assertTrue(self.spool.slot_released(message.dst, message.queue_seq))
        self._stop_broker(first_broker)

        recovery_log_2 = Path(self.cwd.name) / "abandon-broker-recovery-2.log"
        second_broker = self._start_broker(normal_env, recovery_log_2)
        self.assertTrue(self._wait_for_lock(runtime_lock, second_broker, timeout_s=10),
                        "第二次恢复 Broker 未持有锁: %s" %
                        recovery_log_2.read_text(encoding="utf-8"))
        time.sleep(0.75)
        assert_abandon_effects_once()
        self.assertEqual(self.spool.get(message.msg_id).state, "FAILED")
        self.assertTrue(self.spool.slot_released(message.dst, message.queue_seq))
        self._stop_broker(second_broker)

    def test_uncertain_retry_crash_recovers_retrying_without_releasing_slot(self) -> None:
        now = now_iso()
        message = self.spool.enqueue(Message(
            msg_id=new_msg_id(), created_at=now, edge_id="dv_done", src="dv_uart",
            dst="sw_uart", project_id="soc_a", ip_id="uart", session=SESSION,
            text="不确定态裁定重试崩溃恢复测试消息", state=QUEUED,
            topology_revision="test-revision", updated_at=now,
        ))
        self.spool.update(message.msg_id, state=DISPATCHING, detail="test setup before prompt")
        self.audit.record({"event": "STATE_TRANSITION", "msg_id": message.msg_id,
                           "dst": message.dst, "state": DISPATCHING,
                           "detail": "test setup before prompt"})
        self.spool.update(message.msg_id, state=DELIVERY_UNCERTAIN,
                          detail="test setup uncertain head")
        self.audit.record({"event": "STATE_TRANSITION", "msg_id": message.msg_id,
                           "dst": message.dst, "state": DELIVERY_UNCERTAIN,
                           "detail": "test setup uncertain head"})

        normal_bin = Path(self.cwd.name) / "uncertain-retry-normal-bin"
        normal_bin.mkdir()
        normal_a2a = normal_bin / "a2a"
        normal_a2a.write_text(
            "#!%s\nfrom a2a_codex.cli import main\nraise SystemExit(main())\n" % sys.executable,
            encoding="utf-8",
        )
        normal_a2a.chmod(0o755)
        normal_env = dict(self.env)
        normal_env["PATH"] = str(normal_bin) + os.pathsep + os.environ.get("PATH", "")

        resolve_bin = Path(self.cwd.name) / "uncertain-retry-resolve-bin"
        resolve_bin.mkdir()
        resolve_marker = Path(self.cwd.name) / "retrying-state-persisted.json"
        resolve_a2a = resolve_bin / "a2a"
        resolve_source = '''import json, os, signal, sys
from a2a_codex.spool import Spool

MARKER = @MARKER@
MSG_ID = @MSG_ID@
original_update = Spool.update

def update_then_kill(self, msg_id, **kwargs):
    persisted = original_update(self, msg_id, **kwargs)
    if msg_id == MSG_ID and kwargs.get("state") == "RETRYING" and kwargs.get("ruling_id"):
        marker_tmp = MARKER + ".tmp"
        with open(marker_tmp, "w", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "msg_id": msg_id,
                       "state": persisted.state, "ruling_id": kwargs["ruling_id"]}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(marker_tmp, MARKER)
        os.kill(os.getpid(), signal.SIGKILL)
    return persisted

Spool.update = update_then_kill
from a2a_codex.cli import main
raise SystemExit(main())
'''.replace("@MARKER@", repr(str(resolve_marker))).replace("@MSG_ID@", repr(message.msg_id))
        resolve_a2a.write_text("#!%s\n" % sys.executable + resolve_source, encoding="utf-8")
        resolve_a2a.chmod(0o755)
        resolve_env = dict(self.env)
        resolve_env["PATH"] = str(resolve_bin) + os.pathsep + os.environ.get("PATH", "")

        resolve_log = Path(self.cwd.name) / "uncertain-retry-resolve.log"
        resolve_output = resolve_log.open("w", encoding="utf-8")
        resolve_process = subprocess.Popen(
            ["a2a", "resolve", message.msg_id, "retry", "--actor", "test-operator",
             "--reason", "manual confirmation that prompt was not delivered"],
            env=resolve_env, cwd=str(Path(__file__).resolve().parents[3]),
            stdout=resolve_output, stderr=subprocess.STDOUT, text=True,
        )
        self.processes.append(("resolve-uncertain-retry", resolve_process, resolve_output))
        self.addCleanup(self._cleanup_processes)
        self.assertTrue(self._wait_for_path(resolve_marker, timeout_s=10),
                        "未在 RETRYING 持久化后观察到崩溃 marker: %s" %
                        resolve_log.read_text(encoding="utf-8"))
        self.assertEqual(resolve_process.wait(timeout=10), -signal.SIGKILL)
        if not resolve_output.closed:
            resolve_output.close()
        resolve_record = json.loads(resolve_marker.read_text(encoding="utf-8"))
        self.assertEqual(resolve_record["pid"], resolve_process.pid)
        self.assertEqual(resolve_record["state"], "RETRYING")
        ruling_id = resolve_record["ruling_id"]
        self.assertEqual(self.spool.get(message.msg_id).state, "RETRYING")
        self.assertFalse(self.spool.slot_released(message.dst, message.queue_seq),
                         "不确定态重试不得放行当前槽位")
        self.assertFalse(any(event.get("event") == "RULING_APPLIED"
                             and event.get("ruling_id") == ruling_id
                             for event in self.audit.read(strict=True)))

        recovery_bin = Path(self.cwd.name) / "uncertain-retry-recovery-bin"
        recovery_bin.mkdir()
        applied_marker = Path(self.cwd.name) / "retry-ruling-applied-fsynced.json"
        recovery_a2a = recovery_bin / "a2a"
        recovery_source = '''import json, os, signal, sys
from a2a_codex.audit import AuditLog

MARKER = @MARKER@
RULING_ID = @RULING_ID@
original_record = AuditLog.record

def record_then_kill(self, event):
    persisted = original_record(self, event)
    if event.get("event") == "RULING_APPLIED" and event.get("ruling_id") == RULING_ID:
        marker_tmp = MARKER + ".tmp"
        with open(marker_tmp, "w", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "event": persisted}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(marker_tmp, MARKER)
        os.kill(os.getpid(), signal.SIGKILL)
    return persisted

AuditLog.record = record_then_kill
from a2a_codex.cli import main
raise SystemExit(main())
'''.replace("@MARKER@", repr(str(applied_marker))).replace("@RULING_ID@", repr(ruling_id))
        recovery_a2a.write_text("#!%s\n" % sys.executable + recovery_source,
                                encoding="utf-8")
        recovery_a2a.chmod(0o755)
        recovery_env = dict(self.env)
        recovery_env["PATH"] = str(recovery_bin) + os.pathsep + os.environ.get("PATH", "")
        recovery_log = Path(self.cwd.name) / "uncertain-retry-recovery-crash.log"
        recovery_output = recovery_log.open("w", encoding="utf-8")
        recovery_process = subprocess.Popen(
            ["a2a", "broker", "run"], env=recovery_env,
            cwd=str(Path(__file__).resolve().parents[3]),
            stdout=recovery_output, stderr=subprocess.STDOUT, text=True,
        )
        self.processes.append(("broker-recover-retry-kill-after-applied", recovery_process,
                               recovery_output))
        self.assertTrue(self._wait_for_path(applied_marker, timeout_s=10),
                        "未在 RULING_APPLIED fsync 后观察到恢复 Broker 崩溃 marker: %s" %
                        recovery_log.read_text(encoding="utf-8"))
        self.assertEqual(recovery_process.wait(timeout=10), -signal.SIGKILL)
        if not recovery_output.closed:
            recovery_output.close()
        applied_record = json.loads(applied_marker.read_text(encoding="utf-8"))
        self.assertEqual(applied_record["pid"], recovery_process.pid)
        self.assertEqual(applied_record["event"]["ruling_id"], ruling_id)
        self.assertEqual(self.spool.get(message.msg_id).state, "RETRYING")
        self.assertFalse(self.spool.slot_released(message.dst, message.queue_seq))
        self.assertEqual(sum(event.get("event") == "RULING_APPLIED"
                             and event.get("ruling_id") == ruling_id
                             for event in self.audit.read(strict=True)), 1)

        recovery_log_2 = Path(self.cwd.name) / "uncertain-retry-broker-recovery-2.log"
        first_broker = self._start_broker(normal_env, recovery_log_2)
        runtime_lock = self.state_dir / "spool" / "broker-runtime.lock"
        self.assertTrue(self._wait_for_lock(runtime_lock, first_broker, timeout_s=10),
                        "第二阶段恢复 Broker 未持有锁: %s" %
                        recovery_log_2.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current = self.spool.get(message.msg_id)
            if current.state == "TARGET_MISSING":
                break
            time.sleep(0.05)
        self.assertEqual(sum(event.get("event") == "STATE_TRANSITION"
                             and event.get("ruling_id") == ruling_id
                             and event.get("state") == "RETRYING"
                             for event in self.audit.read(strict=True)), 1)
        self.assertEqual(sum(event.get("event") == "RULING_APPLIED"
                             and event.get("ruling_id") == ruling_id
                             for event in self.audit.read(strict=True)), 1)
        self.assertFalse(self.spool.slot_released(message.dst, message.queue_seq))
        self.assertEqual(self.spool.queue_head(message.dst).msg_id, message.msg_id)
        self._stop_broker(first_broker)

        second_log = Path(self.cwd.name) / "uncertain-retry-broker-recovery-3.log"
        second_broker = self._start_broker(normal_env, second_log)
        self.assertTrue(self._wait_for_lock(runtime_lock, second_broker, timeout_s=10),
                        "重复恢复 Broker 未持有锁: %s" % second_log.read_text(encoding="utf-8"))
        time.sleep(0.75)
        self.assertEqual(sum(event.get("event") == "STATE_TRANSITION"
                             and event.get("ruling_id") == ruling_id
                             and event.get("state") == "RETRYING"
                             for event in self.audit.read(strict=True)), 1)
        self.assertEqual(sum(event.get("event") == "RULING_APPLIED"
                             and event.get("ruling_id") == ruling_id
                             for event in self.audit.read(strict=True)), 1)
        self.assertFalse(self.spool.slot_released(message.dst, message.queue_seq))
        self._stop_broker(second_broker)

    def test_abandon_and_continue_crash_releases_only_current_queue_slot(self) -> None:
        now = now_iso()
        first = self.spool.enqueue(Message(
            msg_id=new_msg_id(), created_at=now, edge_id="dv_done", src="dv_uart",
            dst="sw_uart", project_id="soc_a", ip_id="uart", session=SESSION,
            text="abandon_and_continue 队列头崩溃恢复测试消息", state=QUEUED,
            topology_revision="test-revision", updated_at=now,
        ))
        second = self.spool.enqueue(Message(
            msg_id=new_msg_id(), created_at=now, edge_id="dv_done", src="dv_uart",
            dst="sw_uart", project_id="soc_a", ip_id="uart", session=SESSION,
            text="同目标后继队列消息", state=QUEUED,
            topology_revision="test-revision", updated_at=now,
        ))
        failed = self.spool.update(first.msg_id, state="TARGET_BLOCKED",
                                   detail="test setup deterministic terminal failure")
        self.audit.record({"event": "STATE_TRANSITION", "msg_id": first.msg_id,
                           "dst": first.dst, "state": failed.state,
                           "detail": "test setup deterministic terminal failure"})
        self.assertEqual(self.spool.queue_head(first.dst).msg_id, first.msg_id)
        self.assertFalse(self.spool.slot_released(first.dst, first.queue_seq))

        normal_bin = Path(self.cwd.name) / "abandon-continue-normal-bin"
        normal_bin.mkdir()
        normal_a2a = normal_bin / "a2a"
        normal_a2a.write_text(
            "#!%s\nfrom a2a_codex.cli import main\nraise SystemExit(main())\n" % sys.executable,
            encoding="utf-8",
        )
        normal_a2a.chmod(0o755)
        normal_env = dict(self.env)
        normal_env["PATH"] = str(normal_bin) + os.pathsep + os.environ.get("PATH", "")

        injection_bin = Path(self.cwd.name) / "abandon-continue-injection-bin"
        injection_bin.mkdir()
        marker = Path(self.cwd.name) / "abandon-continue-slot-released.json"
        injection_a2a = injection_bin / "a2a"
        injection_source = '''import json, os, signal, sys
from a2a_codex.spool import Spool

MARKER = @MARKER@
DST = @DST@
QUEUE_SEQ = @QUEUE_SEQ@
original_release = Spool.release_slot

def release_then_kill(self, dst, queue_seq, **kwargs):
    released = original_release(self, dst, queue_seq, **kwargs)
    if (dst == DST and queue_seq == QUEUE_SEQ
            and kwargs.get("reason") == "OPERATOR_ABANDON_AND_CONTINUE"):
        marker_tmp = MARKER + ".tmp"
        with open(marker_tmp, "w", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "dst": dst, "queue_seq": queue_seq,
                       "ruling_id": kwargs.get("ruling_id"), "released": released}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(marker_tmp, MARKER)
        os.kill(os.getpid(), signal.SIGKILL)
    return released

Spool.release_slot = release_then_kill
from a2a_codex.cli import main
raise SystemExit(main())
'''.replace("@MARKER@", repr(str(marker))).replace("@DST@", repr(first.dst))
        injection_source = injection_source.replace("@QUEUE_SEQ@", repr(first.queue_seq))
        injection_a2a.write_text("#!%s\n" % sys.executable + injection_source,
                                 encoding="utf-8")
        injection_a2a.chmod(0o755)
        injection_env = dict(self.env)
        injection_env["PATH"] = str(injection_bin) + os.pathsep + os.environ.get("PATH", "")

        resolve_log = Path(self.cwd.name) / "abandon-continue-resolve.log"
        resolve_output = resolve_log.open("w", encoding="utf-8")
        resolve_process = subprocess.Popen(
            ["a2a", "resolve", first.msg_id, "abandon-and-continue", "--actor",
             "test-operator", "--reason", "skip this failed queue slot"],
            env=injection_env, cwd=str(Path(__file__).resolve().parents[3]),
            stdout=resolve_output, stderr=subprocess.STDOUT, text=True,
        )
        self.processes.append(("resolve-abandon-continue", resolve_process, resolve_output))
        self.addCleanup(self._cleanup_processes)
        self.assertTrue(self._wait_for_path(marker, timeout_s=10),
                        "未在 abandon_and_continue 槽位放行后观察到 marker: %s" %
                        resolve_log.read_text(encoding="utf-8"))
        self.assertEqual(resolve_process.wait(timeout=10), -signal.SIGKILL)
        if not resolve_output.closed:
            resolve_output.close()
        marker_record = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(marker_record["pid"], resolve_process.pid)
        self.assertEqual(marker_record["queue_seq"], first.queue_seq)
        self.assertTrue(marker_record["released"])
        ruling_id = marker_record["ruling_id"]
        self.assertEqual(self.spool.get(first.msg_id).state, "TARGET_BLOCKED")
        self.assertTrue(self.spool.slot_released(first.dst, first.queue_seq))
        self.assertFalse(self.spool.slot_released(second.dst, second.queue_seq))
        self.assertFalse(any(event.get("event") in {"QUEUE_RELEASED", "RULING_APPLIED"}
                             and event.get("ruling_id") == ruling_id
                             for event in self.audit.read(strict=True)))

        recovery_log = Path(self.cwd.name) / "abandon-continue-broker-recovery-1.log"
        first_broker = self._start_broker(normal_env, recovery_log)
        runtime_lock = self.state_dir / "spool" / "broker-runtime.lock"
        self.assertTrue(self._wait_for_lock(runtime_lock, first_broker, timeout_s=10),
                        "恢复 Broker 未持有锁: %s" % recovery_log.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if any(event.get("event") == "RULING_APPLIED"
                   and event.get("ruling_id") == ruling_id
                   for event in self.audit.read(strict=True)):
                break
            time.sleep(0.05)
        events = self.audit.read(strict=True)
        self.assertEqual(sum(event.get("event") == "QUEUE_RELEASED"
                             and event.get("ruling_id") == ruling_id
                             and event.get("msg_id") == first.msg_id for event in events), 1)
        self.assertEqual(sum(event.get("event") == "RULING_APPLIED"
                             and event.get("ruling_id") == ruling_id for event in events), 1)
        self.assertTrue(self.spool.slot_released(first.dst, first.queue_seq))
        self.assertFalse(self.spool.slot_released(second.dst, second.queue_seq),
                         "裁定只能放行当前 queue_seq，不能放行后继槽位")
        self.assertEqual(self.spool.queue_head(first.dst).msg_id, second.msg_id,
                         "恢复后队列头必须转到紧邻的后继消息")
        self._stop_broker(first_broker)

        recovery_log_2 = Path(self.cwd.name) / "abandon-continue-broker-recovery-2.log"
        second_broker = self._start_broker(normal_env, recovery_log_2)
        self.assertTrue(self._wait_for_lock(runtime_lock, second_broker, timeout_s=10),
                        "第二次恢复 Broker 未持有锁: %s" %
                        recovery_log_2.read_text(encoding="utf-8"))
        time.sleep(0.75)
        events = self.audit.read(strict=True)
        self.assertEqual(sum(event.get("event") == "QUEUE_RELEASED"
                             and event.get("ruling_id") == ruling_id
                             and event.get("msg_id") == first.msg_id for event in events), 1)
        self.assertEqual(sum(event.get("event") == "RULING_APPLIED"
                             and event.get("ruling_id") == ruling_id for event in events), 1)
        self.assertTrue(self.spool.slot_released(first.dst, first.queue_seq))
        self.assertFalse(self.spool.slot_released(second.dst, second.queue_seq))
        self.assertEqual(self.spool.queue_head(first.dst).msg_id, second.msg_id)
        self._stop_broker(second_broker)


if __name__ == "__main__":
    unittest.main()
