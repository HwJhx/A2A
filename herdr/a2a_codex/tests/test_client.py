from __future__ import annotations

import json
import io
import subprocess
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Optional, Sequence
from unittest import mock

from a2a_codex import HerdrClient
from a2a_codex import cli as cli_module
from a2a_codex.errors import (
    HerdrAgentPromptFailed,
    HerdrAgentNameTaken,
    HerdrAgentNotReady,
    HerdrInvalidAgentName,
    HerdrNotFound,
    HerdrPromptOutcomeUnknown,
    from_code,
)
from a2a_codex.launcher import build_launch_command
from a2a_codex.messages import Message
from a2a_codex.router import SendReceipt


def response(result: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, json.dumps({"id": "x", "result": result}), "")


class Runner:
    def __init__(self, *items: subprocess.CompletedProcess[str]) -> None:
        self.items = list(items)
        self.calls: list[list[str]] = []
        self.timeouts: list[Optional[float]] = []

    def __call__(self, argv: Sequence[str], timeout: Optional[float]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        self.timeouts.append(timeout)
        return self.items.pop(0) if self.items else response({})


class TestHerdrClient(unittest.TestCase):
    def test_version_is_read_without_session_and_unknown_output_fails_closed(self) -> None:
        runner = Runner(subprocess.CompletedProcess([], 0, "herdr 0.9.3\n", ""),
                        subprocess.CompletedProcess([], 0, "unexpected version output", ""))
        client = HerdrClient("test1", runner=runner)
        self.assertEqual(client.version(), "0.9.3")
        self.assertIsNone(client.version())
        self.assertEqual(runner.calls, [["herdr", "--version"], ["herdr", "--version"]])

    def test_agent_name_errors_are_mapped(self) -> None:
        self.assertIsInstance(from_code("agent_name_taken", "taken"), HerdrAgentNameTaken)
        self.assertIsInstance(from_code("invalid_agent_name", "invalid"), HerdrInvalidAgentName)

    def test_prompt_errors_follow_herdr_093_probe_classification(self) -> None:
        prompt_failed = from_code("agent_prompt_failed", "PTY actor closed during input submission")
        self.assertIsInstance(prompt_failed, HerdrAgentPromptFailed)
        self.assertIsInstance(prompt_failed, HerdrPromptOutcomeUnknown)
        self.assertIsInstance(from_code("agent_not_found", "missing"), HerdrNotFound)

    def test_create_tab_maps_to_cli_and_model(self) -> None:
        r = Runner(response({"tab": {"tab_id": "w1:t2", "workspace_id": "w1"},
                             "root_pane": {"pane_id": "w1:p2"}}))
        value = HerdrClient("test1", runner=r).create_tab(label="dv", cwd="/tmp", env={"A2A_IP": "uart"})
        self.assertEqual(value.tab_id, "w1:t2")
        self.assertEqual(r.calls[0], ["herdr", "--session", "test1", "tab", "create", "--label", "dv",
                                      "--cwd", "/tmp", "--env", "A2A_IP=uart", "--no-focus"])

    def test_start_agent_uses_pane_run(self) -> None:
        r = Runner(response({}))
        HerdrClient("test1", runner=r).start_agent("w1:p2", "bash -c 'exec -a pi fnx_dv'")
        self.assertEqual(r.calls[0][3:], ["pane", "run", "w1:p2", "bash -c 'exec -a pi fnx_dv'"])

    def test_read_is_plain_text(self) -> None:
        r = Runner(subprocess.CompletedProcess([], 0, "hello\n", ""))
        self.assertEqual(HerdrClient("test1", runner=r).read_agent("w1:p2", lines=20), "hello\n")

    def test_error_is_mapped(self) -> None:
        body = json.dumps({"error": {"code": "agent_not_ready", "message": "not ready"}})
        r = Runner(subprocess.CompletedProcess([], 1, body, ""))
        with self.assertRaises(HerdrAgentNotReady):
            HerdrClient("test1", runner=r).send_prompt("w1:p2", "hi")

    def test_delete_and_rename(self) -> None:
        r = Runner(response({}), response({}), response({}))
        c = HerdrClient("test1", runner=r)
        c.delete_workspace("w1")
        c.rename_tab("w1:t1", "verification")
        c.delete_pane("w1:p1")
        self.assertEqual(r.calls[0][3:], ["workspace", "close", "w1"])
        self.assertEqual(r.calls[1][3:], ["tab", "rename", "w1:t1", "verification"])
        self.assertEqual(r.calls[2][3:], ["pane", "close", "w1:p1"])

    def test_agent_rename_and_launch_command(self) -> None:
        r = Runner(response({"agents": [{"agent": "pi", "pane_id": "w1:p1"}]}),
                   response({"agent": {"agent": "pi", "pane_id": "w1:p1", "name": "dv_uart"}}))
        c = HerdrClient("test1", runner=r)
        self.assertEqual(c.find_agent("w1:p1").agent, "pi")
        self.assertEqual(c.rename_agent("w1:p1", "dv_uart").raw["name"], "dv_uart")
        self.assertIn("builtin exec -a pi", build_launch_command("/opt/fnx_dv"))

    def test_agent_rename_can_clear_name(self) -> None:
        r = Runner(response({"agent": {"pane_id": "w1:p1"}}))
        HerdrClient("test1", runner=r).rename_agent("w1:p1", None)
        self.assertEqual(r.calls[0][3:], ["agent", "rename", "w1:p1", "--clear"])

    def test_pane_process_info_maps_to_cli(self) -> None:
        r = Runner(response({"process_info": {"pane_id": "w1:p1", "foreground_processes": []}}))
        info = HerdrClient("test1", runner=r).pane_process_info("w1:p1")
        self.assertEqual(info["pane_id"], "w1:p1")
        self.assertEqual(r.calls[0][3:], ["pane", "process-info", "--pane", "w1:p1"])

    def test_pane_process_info_maps_to_cli_and_result(self) -> None:
        r = Runner(response({"process_info": {"pane_id": "w1:p1", "foreground_processes": []}}))
        value = HerdrClient("test1", runner=r).pane_process_info("w1:p1")
        self.assertEqual(value["pane_id"], "w1:p1")
        self.assertEqual(r.calls[0][3:], ["pane", "process-info", "--pane", "w1:p1"])

    def test_send_prompt_wait_requires_timeout(self) -> None:
        with self.assertRaises(ValueError):
            HerdrClient("test1", runner=Runner()).send_prompt("w1:p1", "hello", wait=True)

    def test_stalled_and_timed_out_prompt_are_outcome_unknown(self) -> None:
        stalled = json.dumps({"error": {"code": "agent_prompt_stalled", "message": "stalled"}})
        timed_out = json.dumps({"error": {"code": "timeout", "message": "timed out"}})
        for body in (stalled, timed_out):
            runner = Runner(subprocess.CompletedProcess([], 1, body, ""))
            with self.subTest(body=body), self.assertRaises(HerdrPromptOutcomeUnknown):
                HerdrClient("test1", runner=runner).send_prompt(
                    "w1:p1", "hello", wait=True, timeout_ms=120000
                )

    def test_client_process_timeout_during_prompt_is_outcome_unknown(self) -> None:
        def timeout_runner(argv, timeout):
            raise subprocess.TimeoutExpired(argv, timeout)

        with self.assertRaises(HerdrPromptOutcomeUnknown):
            HerdrClient("test1", runner=timeout_runner).send_prompt("w1:p1", "hello")


class TestCli(unittest.TestCase):
    def test_a2a_entry_point_keeps_a2a_herdr_compatibility_alias(self) -> None:
        project_config = Path(__file__).resolve().parents[1] / "pyproject.toml"
        scripts = project_config.read_text(encoding="utf-8")
        self.assertIn('a2a = "a2a_codex.cli:main"', scripts)
        self.assertIn('a2a-herdr = "a2a_codex.cli:main"', scripts)

    def test_wait_until_explicit_value_replaces_default(self) -> None:
        captured = {}

        class StubClient:
            def __init__(self, session):
                pass

            def wait_agent(self, target, *, until, timeout_ms):
                captured["target"] = target
                captured["until"] = list(until)
                return type("Result", (), {"raw": {"ok": True}})()

        with mock.patch.object(cli_module, "HerdrClient", StubClient), redirect_stdout(io.StringIO()):
            self.assertEqual(cli_module.main(["wait-agent", "w1:p1", "--until", "idle"]), 0)
        self.assertEqual(captured["until"], ["idle"])

    def test_free_text_send_prompt_is_not_an_agent_cli_command(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            cli_module.main(["send-prompt", "w1:p1", "arbitrary text"])
        self.assertEqual(caught.exception.code, 2)

    def test_send_rejects_free_text_and_only_passes_edge_id_to_router(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            cli_module.main(["send", "dv_done", "arbitrary text"])
        self.assertEqual(caught.exception.code, 2)

        receipt = SendReceipt(
            msg_id="0000000000000001-abcdef", edge_id="dv_done", src="dv_uart",
            dst="sw_uart", text="fixed template", state="QUEUED",
            topology_revision="r1",
        )
        router = mock.Mock()
        router.send.return_value = receipt
        output = io.StringIO()
        with mock.patch.object(cli_module, "_state_components", return_value=(object(), object())), \
                mock.patch.object(cli_module, "_router", return_value=router), \
                redirect_stdout(output):
            self.assertEqual(cli_module.main(["send", "dv_done"]), 0)

        router.send.assert_called_once_with("dv_done")
        self.assertEqual(json.loads(output.getvalue()), {
            "msg_id": receipt.msg_id, "edge_id": receipt.edge_id,
            "src": receipt.src, "dst": receipt.dst, "text": receipt.text,
            "state": receipt.state, "topology_revision": receipt.topology_revision,
        })

    def test_status_and_queue_print_message_and_queue_state(self) -> None:
        head = Message(
            msg_id="0000000000000001-abcdef", created_at="t0", edge_id="dv_done",
            src="dv_uart", dst="sw_uart", project_id="soc_a", ip_id="uart",
            session="test1", text="fixed template", state="DELIVERY_UNCERTAIN",
            topology_revision="r1", updated_at="t1", queue_seq=1,
        )
        later = Message(
            msg_id="0000000000000002-abcdef", created_at="t0", edge_id="dv_done",
            src="dv_uart", dst="sw_uart", project_id="soc_a", ip_id="uart",
            session="test1", text="fixed template", state="QUEUED",
            topology_revision="r1", updated_at="t1", queue_seq=2,
        )

        class StubSpool:
            def get(self, msg_id):
                self.requested_msg_id = msg_id
                return head

            def queue_targets(self):
                return ["sw_uart"]

            def queue_head(self, dst):
                self.requested_dst = dst
                return head if dst == "sw_uart" else None

            def pending(self, _dst):
                return [head, later]

            def done(self):
                return []

            def slot_released(self, _dst, _queue_seq):
                return False

        spool = StubSpool()
        patch_components = mock.patch.object(
            cli_module, "_state_components", return_value=(spool, object()))

        status_output = io.StringIO()
        with patch_components, redirect_stdout(status_output):
            self.assertEqual(cli_module.main(["status", head.msg_id]), 0)
        self.assertEqual(json.loads(status_output.getvalue())["msg_id"], head.msg_id)
        self.assertEqual(spool.requested_msg_id, head.msg_id)

        queue_output = io.StringIO()
        with mock.patch.object(cli_module, "_state_components", return_value=(spool, object())), \
                redirect_stdout(queue_output):
            self.assertEqual(cli_module.main(["queue", "sw_uart"]), 0)
        rows = json.loads(queue_output.getvalue())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["dst"], "sw_uart")
        self.assertEqual(rows[0]["head"]["msg_id"], head.msg_id)
        self.assertTrue(rows[0]["paused"])
        self.assertEqual(rows[0]["paused_message_count"], 1)
        self.assertFalse(rows[0]["slot_released"])

    def test_broker_run_constructs_runtime_and_starts_scheduler(self) -> None:
        runtime = mock.Mock()
        with mock.patch.object(cli_module, "_state_components", return_value=(object(), object())), \
                mock.patch.object(cli_module, "Registry") as registry_cls, \
                mock.patch.object(cli_module, "HerdrClient") as client_cls, \
                mock.patch.object(cli_module, "DeliveryBroker") as broker_cls, \
                mock.patch.object(cli_module, "BrokerRuntime", return_value=runtime) as runtime_cls:
            client_cls.return_value.version.return_value = "0.9.3"
            self.assertEqual(cli_module.main(["broker", "run"]), 0)

        registry_cls.assert_called_once_with()
        client_cls.assert_called_once()
        client_cls.return_value.version.assert_called_once_with()
        broker_cls.assert_called_once()
        self.assertEqual(broker_cls.call_args.kwargs["herdr_version"], "0.9.3")
        runtime_cls.assert_called_once_with(broker_cls.return_value)
        runtime.run.assert_called_once()
        self.assertIsInstance(runtime.run.call_args.kwargs["stop_event"], threading.Event)

    def test_broker_run_translates_sigterm_to_graceful_stop(self) -> None:
        runtime = mock.Mock()
        handlers = {}
        previous = object()

        def set_signal(signum, handler):
            handlers[signum] = handler
            return previous

        def run(*, stop_event):
            handlers[cli_module.signal.SIGTERM](cli_module.signal.SIGTERM, None)
            self.assertTrue(stop_event.is_set())

        runtime.run.side_effect = run
        with mock.patch.object(cli_module.signal, "getsignal", return_value=previous), \
                mock.patch.object(cli_module.signal, "signal", side_effect=set_signal):
            cli_module._run_broker(runtime)
        runtime.stop.assert_called_once_with()

    def test_read_agent_diagnostic_command_remains_available(self) -> None:
        captured = {}

        class StubClient:
            def __init__(self, session):
                captured["session"] = session

            def read_agent(self, target, *, source, lines):
                captured.update(target=target, source=source, lines=lines)
                return "diagnostic output\n"

        output = io.StringIO()
        with mock.patch.object(cli_module, "HerdrClient", StubClient), \
                redirect_stdout(output):
            self.assertEqual(cli_module.main([
                "--session", "test1", "read-agent", "w1:p1",
                "--source", "recent", "--lines", "12",
            ]), 0)
        self.assertEqual(output.getvalue(), "diagnostic output\n")
        self.assertEqual(captured, {
            "session": "test1", "target": "w1:p1", "source": "recent", "lines": 12,
        })

    def test_resolve_actions_map_to_broker_api_with_reason_and_actor(self) -> None:
        expected_actions = {
            "delivered": "delivered",
            "retry": "retry",
            "abandon": "abandon",
            "abandon-and-continue": "abandon_and_continue",
        }
        spool, audit = object(), object()
        with mock.patch.object(cli_module, "_state_components", return_value=(spool, audit)), \
                mock.patch.object(cli_module, "_runtime") as runtime_factory, \
                mock.patch.object(cli_module.getpass, "getuser", return_value="test-operator"):
            runtime = runtime_factory.return_value
            runtime.broker.resolve.return_value = {"ok": True}
            for cli_action, api_action in expected_actions.items():
                with self.subTest(action=cli_action), redirect_stdout(io.StringIO()):
                    self.assertEqual(cli_module.main([
                        "resolve", "msg-123", cli_action, "--reason", "verified",
                    ]), 0)
                runtime.broker.resolve.assert_called_with(
                    "msg-123", api_action, actor="test-operator", reason="verified")

    def test_actor_is_audit_label_without_authentication(self) -> None:
        runtime = mock.Mock()
        runtime.broker.resolve.return_value = {"ok": True}
        with mock.patch.object(cli_module, "_state_components", return_value=(object(), object())), \
                mock.patch.object(cli_module, "_runtime", return_value=runtime), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cli_module.main([
                "resolve", "msg-123", "abandon", "--reason", "reviewed",
                "--actor", "claimed-auditor-label",
            ]), 0)
        runtime.broker.resolve.assert_called_once_with(
            "msg-123", "abandon", actor="claimed-auditor-label", reason="reviewed")

    def test_ruling_void_requires_json_object_and_passes_effects_to_api(self) -> None:
        runtime = mock.Mock()
        runtime.broker.void_ruling.return_value = {"voided": True}
        base_args = ["ruling", "void", "ruling-123", "--reason", "superseded"]
        with mock.patch.object(cli_module, "_state_components", return_value=(object(), object())), \
                mock.patch.object(cli_module, "_runtime", return_value=runtime), \
                mock.patch.object(cli_module.getpass, "getuser", return_value="default-operator"):
            for non_object in ("null", "[]", '"text"', "17"):
                stderr = io.StringIO()
                with self.subTest(verified_effects=non_object), redirect_stderr(stderr):
                    self.assertEqual(cli_module.main([
                        *base_args, "--verified-effects", non_object,
                    ]), 2)
                self.assertIn("JSON object", stderr.getvalue())
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                self.assertEqual(cli_module.main([
                    *base_args, "--verified-effects", "not-json",
                ]), 2)
            self.assertIn("有效 JSON", stderr.getvalue())
            runtime.broker.void_ruling.assert_not_called()

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(cli_module.main([
                    *base_args, "--verified-effects", '{"slot_released": false}',
                ]), 0)
        runtime.broker.void_ruling.assert_called_once_with(
            "ruling-123", actor="default-operator", reason="superseded",
            verified_effects={"slot_released": False})
        self.assertEqual(json.loads(output.getvalue()), {"voided": True})

    def test_dispatch_resume_forwards_operator_inputs(self) -> None:
        runtime = mock.Mock()
        runtime.resume_dispatch.return_value = {"resumed": True}
        with mock.patch.object(cli_module, "_state_components", return_value=(object(), object())), \
                mock.patch.object(cli_module, "_runtime", return_value=runtime), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cli_module.main([
                "dispatch", "resume", "--reason", "incident verified",
                "--quarantine-location", "none", "--actor", "operator-label",
            ]), 0)
        runtime.resume_dispatch.assert_called_once_with(
            actor="operator-label", reason="incident verified", quarantine_location="none")

    def test_spool_quarantine_list_and_repair_call_their_apis(self) -> None:
        incidents = [{"incident_id": "incident-1"}]
        spool = mock.Mock()
        spool.quarantine_incidents.return_value = incidents
        output = io.StringIO()
        with mock.patch.object(cli_module, "_state_components", return_value=(spool, object())), \
                redirect_stdout(output):
            self.assertEqual(cli_module.main(["spool", "quarantine", "list"]), 0)
        spool.quarantine_incidents.assert_called_once_with()
        self.assertEqual(json.loads(output.getvalue()), incidents)

        runtime = mock.Mock()
        runtime.resolve_spool_corruption.return_value = {"resolved": True}
        output = io.StringIO()
        with mock.patch.object(cli_module, "_state_components", return_value=(spool, object())), \
                mock.patch.object(cli_module, "_runtime", return_value=runtime), \
                redirect_stdout(output):
            self.assertEqual(cli_module.main([
                "spool", "repair", "incident-1", "--reason", "restored from backup",
                "--verification", "hash and schema checked", "--actor", "ops",
            ]), 0)
        runtime.resolve_spool_corruption.assert_called_once_with(
            "incident-1", actor="ops", reason="restored from backup",
            verification="hash and schema checked")
        self.assertEqual(json.loads(output.getvalue()), {"resolved": True})


if __name__ == "__main__":
    unittest.main()
