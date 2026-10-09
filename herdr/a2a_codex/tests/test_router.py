from __future__ import annotations

import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path

import yaml

from a2a_codex import (
    AgentIdentity,
    AuditLog,
    MessageNotFoundError,
    Registry,
    Router,
    SendRejected,
    Spool,
    Topology,
    TopologyStore,
)
from a2a_codex.messages import QUEUED
from a2a_codex.messages import Message, now_iso, new_msg_id
from a2a_codex.spool import SpoolError


def _spool_enqueue_worker(root, payloads):
    spool = Spool(root)
    for payload in payloads:
        spool.enqueue(Message.from_dict(payload))


def _audit_append_worker(path, worker_id, count):
    audit = AuditLog(path)
    for index in range(count):
        audit.record({"worker": worker_id, "index": index, "state": "TEST"})


def _crash_during_enqueue(root, payload, exit_code):
    import a2a_codex.storage as storage

    spool = Spool(root)
    storage.os.replace = lambda *_args, **_kwargs: os._exit(exit_code)
    spool.enqueue(Message.from_dict(payload))


def _crash_after_done_archive(root, msg_id, exit_code):
    original_unlink = Path.unlink
    spool = Spool(root)

    def crash_before_pending_unlink(path, *args, **kwargs):
        if "pending" in path.parts:
            os._exit(exit_code)
        return original_unlink(path, *args, **kwargs)

    Path.unlink = crash_before_pending_unlink
    spool.update(msg_id, state="DELIVERED")


def _crash_during_audit_append(path, event, exit_code):
    audit = AuditLog(path)
    original_write = os.write

    def write_half_then_exit(descriptor, data):
        raw = bytes(data)
        original_write(descriptor, raw[:max(1, len(raw) // 2)])
        os._exit(exit_code)

    os.write = write_half_then_exit
    audit.record(event)

SESSION = "router-test"
CONFIG = {
    "version": 1,
    "project_id": "soc_a",
    "roles": {"dv": {}, "sw": {}, "rtl": {}},
    "ips": ["uart", "gpio"],
    "edges": [
        {"id": "dv_done", "from": "dv", "to": "sw", "template": "{ip}验证完成，请开发驱动。"},
        {"id": "dv_rtl", "from": "dv", "to": "rtl", "template": "检查 {ip}"},
    ],
}


class RouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.topology_path = root / "topology.yaml"
        self.topology_path.write_text(yaml.safe_dump(CONFIG, allow_unicode=True), encoding="utf-8")
        self.topology = TopologyStore(self.topology_path)
        self.registry = Registry(root / "registry.json")
        self.spool = Spool(root / "spool")
        self.audit = AuditLog(root / "audit.jsonl")
        for role in CONFIG["roles"]:
            for ip_id in CONFIG["ips"]:
                identity = AgentIdentity.create(self.topology.current, role, ip_id)
                self.registry.register(identity, session=SESSION, workspace_id="w1", tab_id="w1:t1",
                                       pane_id=f"pane_{role}_{ip_id}", status="idle")
        self.router = Router(self.topology, self.registry, self.spool, self.audit, session=SESSION)

    @staticmethod
    def env(role="dv", ip_id="uart", **changes):
        values = {"A2A_PROJECT_ID": "soc_a", "A2A_ROLE": role, "A2A_IP": ip_id,
                  "HERDR_PANE_ID": f"pane_{role}_{ip_id}"}
        values.update(changes)
        return values

    def reject(self, expected_code, edge, env, *, router=None):
        with self.assertRaises(SendRejected) as caught:
            (router or self.router).send(edge, env)
        error = caught.exception
        self.assertEqual(error.code, expected_code)
        matching = [event for event in self.audit.read() if event.get("msg_id") == error.msg_id]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["state"], "REJECTED")
        self.assertEqual(matching[0]["reject_code"], expected_code)
        return error, matching[0]

    def test_happy_path_renders_same_ip_and_enqueues(self):
        receipt = self.router.send("dv_done", self.env())
        self.assertEqual((receipt.src, receipt.dst, receipt.state), ("dv_uart", "sw_uart", QUEUED))
        self.assertEqual(receipt.text, "uart验证完成，请开发驱动。")
        queued = self.spool.pending("sw_uart")
        self.assertEqual([item.msg_id for item in queued], [receipt.msg_id])
        self.assertEqual(self.router.status(receipt.msg_id), queued[0])
        self.assertEqual(receipt.topology_revision, self.topology.revision)
        event = self.audit.read()[-1]
        self.assertEqual((event["src"], event["dst"], event["state"]),
                         ("dv_uart", "sw_uart", QUEUED))

    def test_target_ip_is_derived_from_sender_and_edge(self):
        uart = self.router.send("dv_done", self.env(ip_id="uart"))
        gpio = self.router.send("dv_done", self.env(ip_id="gpio"))
        self.assertEqual((uart.dst, gpio.dst), ("sw_uart", "sw_gpio"))
        self.assertEqual([item.dst for item in self.spool.pending()], ["sw_uart", "sw_gpio"])

    def test_identity_and_pane_spoofing_are_rejected_and_audited(self):
        cases = [
            {"HERDR_PANE_ID": None},
            {"A2A_PROJECT_ID": "other"},
            {"A2A_ROLE": "sw"},
            {"A2A_IP": "gpio"},
            {"HERDR_PANE_ID": "pane_dv_gpio"},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                error, _ = self.reject("identity", "dv_done", self.env(**changes))
                self.assertTrue(error.msg_id)
        self.assertEqual(self.spool.pending(), [])

    def test_unregistered_or_stopped_sender_is_rejected(self):
        self.registry.unregister("dv_uart")
        self.reject("identity", "dv_done", self.env())
        identity = AgentIdentity.create(self.topology.current, "dv", "uart")
        self.registry.register(identity, session=SESSION, workspace_id="w1", tab_id="w1:t1",
                               pane_id="pane_dv_uart", status="idle", lifecycle="stopped")
        self.reject("identity", "dv_done", self.env())

    def test_unknown_edge_and_wrong_direction_are_rejected(self):
        self.reject("unknown_edge", "not-configured", self.env())
        self.reject("wrong_direction", "dv_done", self.env(role="sw"))
        self.assertEqual(self.spool.pending(), [])

    def test_reverse_direction_requires_an_explicit_edge(self):
        self.reject("unknown_edge", "sw_ack", self.env(role="sw"))
        self.topology.add_edge("sw_ack", "sw", "dv", "{ip}驱动已完成")
        receipt = self.router.send("sw_ack", self.env(role="sw"))
        self.assertEqual((receipt.src, receipt.dst), ("sw_uart", "dv_uart"))

    def test_missing_or_nonrunning_target_is_rejected(self):
        self.registry.unregister("sw_uart")
        self.reject("target_missing", "dv_done", self.env())
        identity = AgentIdentity.create(self.topology.current, "sw", "uart")
        self.registry.register(identity, session=SESSION, workspace_id="w1", tab_id="w1:t1",
                               pane_id="pane_sw_uart", status="idle", lifecycle="stopped")
        self.reject("target_not_running", "dv_done", self.env())
        self.assertEqual(self.spool.pending(), [])

    def test_corrupt_registry_cannot_redirect_to_another_project_or_ip(self):
        records = json.loads(self.registry.path.read_text(encoding="utf-8"))
        records["agents"]["sw_uart"]["project_id"] = "other_project"
        self.registry.path.write_text(json.dumps(records), encoding="utf-8")
        self.reject("target_missing", "dv_done", self.env())
        self.assertEqual(self.spool.pending(), [])

    def test_target_from_another_session_is_not_routable(self):
        self.registry.update_runtime("sw_uart", session="other-session")
        self.reject("target_missing", "dv_done", self.env())
        self.assertEqual(self.spool.pending(), [])

    def test_invalid_message_template_output_is_rejected(self):
        self.topology.set_template("dv_done", "完成\x07{ip}")
        self.reject("bad_message", "dv_done", self.env())
        self.topology.set_template("dv_done", "{ip}" * 20)
        tiny = Router(self.topology, self.registry, self.spool, self.audit,
                      session=SESSION, max_chars=10)
        self.reject("bad_message", "dv_done", self.env(), router=tiny)
        self.assertEqual(self.spool.pending(), [])

    def test_sender_cannot_choose_message_or_target(self):
        with self.assertRaises(TypeError):
            self.router.send("dv_done", self.env(), text="free text")  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            self.router.send("dv_done", self.env(), target_ip="gpio")  # type: ignore[call-arg]

    def test_invalid_external_topology_keeps_last_good_snapshot(self):
        self.topology_path.write_text("version: invalid\n", encoding="utf-8")
        receipt = self.router.send("dv_done", self.env())
        self.assertEqual(receipt.dst, "sw_uart")
        self.assertEqual(len(self.spool.pending("sw_uart")), 1)
        self.assertIsNotNone(self.topology.last_error)

    def test_static_topology_is_supported(self):
        router = Router(Topology.from_dict(CONFIG), self.registry, self.spool,
                        self.audit, session=SESSION)
        self.assertEqual(router.send("dv_done", self.env()).topology_revision, "static")


class SpoolAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.spool = Spool(root / "spool")
        self.audit = AuditLog(root / "audit.jsonl")

    def message(self, msg_id=None, *, dst="sw_uart", state=QUEUED):
        now = now_iso()
        return Message(msg_id or new_msg_id(), now, "dv_done", "dv_uart", dst, "soc_a", "uart",
                       SESSION, "uart done", state, "test-revision", updated_at=now)

    def test_fifo_update_archive_and_lookup(self):
        first = self.spool.enqueue(self.message())
        second = self.spool.enqueue(self.message())
        self.assertEqual([msg.msg_id for msg in self.spool.pending("sw_uart")],
                         sorted([first.msg_id, second.msg_id]))
        self.spool.update(first.msg_id, state="DISPATCHING", attempts=1)
        archived = self.spool.update(first.msg_id, state="DELIVERED")
        self.assertEqual(archived.state, "DELIVERED")
        self.assertEqual(self.spool.done(), [archived])
        self.assertEqual([msg.msg_id for msg in self.spool.pending()], [second.msg_id])
        self.assertEqual(self.spool.get(first.msg_id).state, "DELIVERED")

    def test_spool_rejects_path_traversal_and_unknown_message(self):
        with self.assertRaises(SpoolError):
            self.spool.enqueue(self.message(dst="../other"))
        with self.assertRaises(SpoolError):
            self.spool.enqueue(self.message(state="not-a-state"))
        with self.assertRaises(MessageNotFoundError):
            self.spool.get("0" * 16 + "-" + "0" * 6)

    def test_audit_appends_jsonl_with_private_permissions(self):
        self.audit.record({"state": "QUEUED", "msg_id": "m1"})
        self.audit.record({"state": "REJECTED", "msg_id": "m2"})
        self.assertEqual([event["msg_id"] for event in self.audit.read()], ["m1", "m2"])
        self.assertEqual(self.audit.path.stat().st_mode & 0o777, 0o600)

    def test_concurrent_spool_enqueues_are_not_lost(self):
        context = multiprocessing.get_context("fork")
        batches = [[self.message().to_dict() for _ in range(8)] for _ in range(6)]
        processes = [context.Process(target=_spool_enqueue_worker,
                                     args=(str(self.spool.root), batch)) for batch in batches]
        for process in processes:
            process.start()
        for process in processes:
            process.join(60)
        for process in processes:
            self.assertFalse(process.is_alive(), "Spool 并发入队进程超时")
            self.assertEqual(process.exitcode, 0)
        messages = self.spool.pending("sw_uart")
        self.assertEqual(len(messages), 48)
        self.assertEqual(len({message.msg_id for message in messages}), 48)
        self.assertEqual([message.msg_id for message in messages],
                         sorted(message.msg_id for message in messages))

    def test_concurrent_audit_appends_remain_complete_jsonl_records(self):
        context = multiprocessing.get_context("fork")
        workers, per_worker = 6, 20
        processes = [context.Process(target=_audit_append_worker,
                                     args=(str(self.audit.path), worker, per_worker))
                     for worker in range(workers)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(60)
        for process in processes:
            self.assertFalse(process.is_alive(), "AuditLog 并发追加进程超时")
            self.assertEqual(process.exitcode, 0)
        records = self.audit.read()
        self.assertEqual(len(records), workers * per_worker)
        self.assertEqual(len({(record["worker"], record["index"]) for record in records}),
                         workers * per_worker)

    def test_enqueue_crash_before_atomic_replace_keeps_queue_readable(self):
        existing = self.spool.enqueue(self.message())
        interrupted = self.message()
        context = multiprocessing.get_context("fork")
        process = context.Process(target=_crash_during_enqueue,
                                  args=(str(self.spool.root), interrupted.to_dict(), 71))
        process.start()
        process.join(60)
        self.assertFalse(process.is_alive())
        self.assertEqual(process.exitcode, 71)

        recovered = Spool(self.spool.root)
        self.assertEqual([message.msg_id for message in recovered.pending("sw_uart")], [existing.msg_id])
        self.assertEqual(list(recovered.pending_dir.rglob("*.tmp")), [])

    def test_crash_after_terminal_archive_recovers_without_duplicate_pending(self):
        queued = self.spool.enqueue(self.message())
        context = multiprocessing.get_context("fork")
        process = context.Process(target=_crash_after_done_archive,
                                  args=(str(self.spool.root), queued.msg_id, 72))
        process.start()
        process.join(60)
        self.assertFalse(process.is_alive())
        self.assertEqual(process.exitcode, 72)

        recovered = Spool(self.spool.root)
        self.assertEqual(recovered.pending("sw_uart"), [])
        self.assertEqual([(message.msg_id, message.state) for message in recovered.done()],
                         [(queued.msg_id, "DELIVERED")])

    def test_audit_repairs_incomplete_tail_after_process_crash(self):
        self.audit.record({"event_id": "before", "state": "TEST"})
        context = multiprocessing.get_context("fork")
        process = context.Process(target=_crash_during_audit_append,
                                  args=(str(self.audit.path), {"event_id": "partial"}, 73))
        process.start()
        process.join(60)
        self.assertFalse(process.is_alive())
        self.assertEqual(process.exitcode, 73)

        self.assertEqual([event["event_id"] for event in self.audit.read()], ["before"])
        self.assertTrue(self.audit.path.read_bytes().endswith(b"\n"))
        self.audit.record({"event_id": "after", "state": "TEST"})
        self.assertEqual([event["event_id"] for event in self.audit.read()], ["before", "after"])

    def test_audit_keeps_valid_final_record_without_newline(self):
        self.audit.record({"event_id": "first"})
        self.audit.record({"event_id": "second"})
        self.audit.path.write_bytes(self.audit.path.read_bytes().rstrip(b"\n"))

        records = self.audit.read()

        self.assertEqual([event["event_id"] for event in records], ["first", "second"])
        self.assertTrue(self.audit.path.read_bytes().endswith(b"\n"))


if __name__ == "__main__":
    unittest.main()
