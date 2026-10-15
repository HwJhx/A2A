"""路线图 336:发送防重复(幂等键)。Router 单元测试 + 多进程并发 + CLI。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from a2a import Router, SendRejected
from a2a.messages import QUEUED
from a2a.router import IDEMPOTENCY_TTL_S, SEND_DEDUPLICATED

from test_router import RouterBase, SESSION, env_of

SRC = Path(__file__).resolve().parent.parent / "src"


def key(age_s: int = 0) -> str:
    return f"{int(time.time()) - age_s}-{uuid.uuid4()}"


class Crash(Exception):
    pass


class Idempotency(RouterBase):
    def setUp(self):
        super().setUp()
        self.dv = env_of("dv", "uart")

    def send(self, edge="dv_done", k=None, env=None, router=None):
        return (router or self.router).send(edge, env or self.dv, idempotency_key=k)

    def events(self, state, msg_id=None):
        return [e for e in self.audit.read() if e.get("state") == state and (msg_id is None or e.get("msg_id") == msg_id)]

    def crash_at(self, point):
        def hook(name):
            if name == point:
                raise Crash(name)
        self.router.crash_hook = hook

    def assert_exactly_one(self, msg_id):
        self.assertEqual([m.msg_id for m in self.queued()], [msg_id])
        self.assertEqual(len(self.events(QUEUED, msg_id)), 1)

    # ---- 基本行为 ------------------------------------------------------
    def test_same_key_is_enqueued_once_and_returns_the_first_msg_id(self):
        k = key()
        first = self.send(k=k)
        second = self.send(k=k)
        self.assertEqual((second.msg_id, second.duplicate, first.duplicate), (first.msg_id, True, False))
        self.assertEqual((second.dst, second.text, second.queue_seq), (first.dst, first.text, first.queue_seq))
        self.assert_exactly_one(first.msg_id)
        self.assertEqual(len(self.events(SEND_DEDUPLICATED, first.msg_id)), 1)

    def test_different_key_edge_or_sender_are_separate_messages(self):
        k = key()
        a = self.send(k=k)
        b = self.send(k=key())                                   # 新的一次处理
        c = self.send("dv_rtl_fix", k=k)                         # 同一次处理、另一条边
        d = self.send(k=k, env=env_of("dv", "gpio"))             # 另一个发送方
        self.assertEqual(len({a.msg_id, b.msg_id, c.msg_id, d.msg_id}), 4)
        self.assertEqual(len(self.queued()), 4)

    def test_without_a_key_nothing_changes(self):
        a, b = self.send(), self.send()
        self.assertNotEqual(a.msg_id, b.msg_id)
        self.assertEqual(len(self.queued()), 2)
        self.assertFalse((Path(self.temp.name) / "idempotency").exists())

    def test_bad_expired_and_future_keys_are_rejected(self):
        for bad, code in (("abc", "bad_idempotency_key"), (f"{int(time.time())}-not-a-uuid", "bad_idempotency_key"),
                          (key(IDEMPOTENCY_TTL_S + 60), "idempotency_expired"), (key(-3600), "bad_idempotency_key")):
            with self.subTest(bad=bad):
                with self.assertRaises(SendRejected) as ctx:
                    self.send(k=bad)
                self.assertEqual(ctx.exception.code, code)
        self.assertEqual(self.queued(), [])

    def test_a_rejected_first_attempt_records_nothing_and_can_be_retried(self):
        self.registry.update_runtime("sw_uart", lifecycle="stopped")
        k = key()
        with self.assertRaises(SendRejected):
            self.send(k=k)
        self.registry.update_runtime("sw_uart", lifecycle="running")
        self.assertFalse(self.send(k=k).duplicate)                # 第一次没入队:不算重复
        self.assertEqual(len(self.queued()), 1)

    def test_duplicate_is_detected_even_after_the_message_left_the_spool(self):
        k = key()
        first = self.send(k=k)
        for p in Path(self.temp.name, "spool").rglob(f"{first.msg_id}.json"):
            p.unlink()                                            # 模拟已归档
        self.assertTrue(self.send(k=k).duplicate)
        self.assertEqual(self.queued(), [])

    # ---- 崩溃后重试 ----------------------------------------------------
    def test_crash_at_each_step_then_retry_yields_exactly_one_message(self):
        for point in ("before_intent", "after_intent", "after_enqueue", "after_mark"):
            with self.subTest(point=point):
                self.setUp()
                k = key()
                self.crash_at(point)
                with self.assertRaises(Crash):
                    self.send(k=k)
                self.router.crash_hook = None
                retry = self.send(k=k)
                (msg,) = self.queued()
                self.assertEqual(retry.msg_id, msg.msg_id)
                self.assert_exactly_one(msg.msg_id)
                self.assertEqual(retry.duplicate, point == "after_mark")

    def test_crash_between_enqueue_and_audit_is_repaired(self):
        k = key()
        original = self.router._record_queued
        self.router._record_queued = lambda ctx, stored: (_ for _ in ()).throw(Crash("audit"))
        with self.assertRaises(Crash):
            self.send(k=k)
        self.router._record_queued = original
        (msg,) = self.queued()
        self.assertEqual(self.events(QUEUED, msg.msg_id), [])
        retry = self.send(k=k)
        self.assertEqual(retry.msg_id, msg.msg_id)
        self.assert_exactly_one(msg.msg_id)                       # 补记了入队审计,恰好一条

    def test_changed_template_after_intent_is_a_conflict(self):
        k = key()
        self.crash_at("after_intent")
        with self.assertRaises(Crash):
            self.send(k=k)
        self.router.crash_hook = None
        self.store.update(lambda d: d["edges"][0].update(template="{ip} 模板改了。"))
        with self.assertRaises(SendRejected) as ctx:
            self.send(k=k)
        self.assertEqual(ctx.exception.code, "idempotency_conflict")
        self.assertEqual(self.queued(), [])

    def test_removed_target_after_intent_is_rejected_as_usual(self):
        k = key()
        self.crash_at("after_intent")
        with self.assertRaises(Crash):
            self.send(k=k)
        self.router.crash_hook = None
        self.registry.update_runtime("sw_uart", lifecycle="closed")
        with self.assertRaises(SendRejected) as ctx:
            self.send(k=k)
        self.assertEqual(ctx.exception.code, "target_not_running")
        self.registry.update_runtime("sw_uart", lifecycle="running")
        retry = self.send(k=k)                                     # 记录仍是 intent:恢复后补齐,msg_id 不变
        self.assert_exactly_one(retry.msg_id)

    # ---- 22 号方案 §10:不安全模板与幂等路径 ----------------------------------
    def test_unsafe_template_with_a_key_is_rejected_and_leaves_no_intent(self):
        from test_router import write_legacy_template
        write_legacy_template(self.topo_path, "dv_done", "!ls")
        k = key()
        with self.assertRaises(SendRejected) as ctx:
            self.send(k=k)
        self.assertEqual(ctx.exception.code, "unsafe_template")
        self.assertEqual(self.queued(), [])
        self.assertFalse(list((self.router.state_dir / "idempotency").rglob("*.json")))

    def test_intent_recovery_rechecks_after_the_template_became_unsafe(self):
        from test_router import write_legacy_template
        k = key()
        self.crash_at("after_intent")
        with self.assertRaises(Crash):
            self.send(k=k)
        self.router.crash_hook = None
        write_legacy_template(self.topo_path, "dv_done", "/a2a x")
        with self.assertRaises(SendRejected) as ctx:            # 补做时重新鉴权
            self.send(k=k)
        self.assertEqual(ctx.exception.code, "unsafe_template")
        self.assertEqual(self.queued(), [])

    def test_unsafe_snapshot_in_an_old_intent_is_not_enqueued(self):
        # 旧版本留下的 intent:快照文本本身不安全(当前拓扑是安全的)。用快照入队前也要检查
        k = key()
        self.crash_at("after_intent")
        with self.assertRaises(Crash):
            self.send(k=k)
        self.router.crash_hook = None
        [path] = list((self.router.state_dir / "idempotency").rglob("*.json"))
        record = json.loads(path.read_text())
        record["message"]["text"] = "\ufeff!ls"
        path.write_text(json.dumps(record, ensure_ascii=False))
        with self.assertRaises(SendRejected) as ctx:
            self.send(k=k)
        self.assertEqual(ctx.exception.code, "unsafe_template")
        self.assertEqual(self.queued(), [])

    def test_replay_of_an_enqueued_message_still_returns_the_receipt(self):
        from test_router import write_legacy_template
        k = key()
        first = self.send(k=k)
        write_legacy_template(self.topo_path, "dv_done", "/a2a x")       # 之后模板变成不安全的
        again = self.send(k=k)
        self.assertEqual((again.msg_id, again.duplicate), (first.msg_id, True))
        self.assert_exactly_one(first.msg_id)

    def test_concurrent_threads_with_the_same_key_enqueue_once(self):
        k = key()
        routers = [Router(self.store, self.registry, self.spool, self.audit, session=SESSION) for _ in range(8)]
        with ThreadPoolExecutor(8) as pool:
            receipts = list(pool.map(lambda r: self.send(k=k, router=r), routers))
        self.assertEqual(len({r.msg_id for r in receipts}), 1)
        self.assertEqual(sum(not r.duplicate for r in receipts), 1)
        self.assert_exactly_one(receipts[0].msg_id)


class IdempotencyProcesses(RouterBase):
    """多进程(真正的 a2a send 命令行)同键并发,与 CLI 的输出格式。"""

    def cli(self, k):
        env = dict(os.environ, **env_of("dv", "uart"), HERDR_SESSION=SESSION, A2A_STATE_DIR=self.temp.name,
                   A2A_TOPOLOGY=str(self.topo_path), PYTHONPATH=str(SRC), PYTHONDONTWRITEBYTECODE="1")
        return subprocess.Popen([sys.executable, "-m", "a2a.cli", "send", "dv_done", "--idempotency-key", k],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_concurrent_processes_with_the_same_key_enqueue_once(self):
        k = key()
        procs = [self.cli(k) for _ in range(6)]
        outs = []
        for p in procs:
            out, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, err)
            outs.append(json.loads(out))
        self.assertEqual(len({o["msg_id"] for o in outs}), 1)
        self.assertEqual(sorted(o["duplicate"] for o in outs), [False] + [True] * 5)
        self.assertTrue(all(o["state"] == QUEUED for o in outs))   # 回执格式不变,插件照常识别
        self.assertEqual(len(self.queued()), 1)


if __name__ == "__main__":
    unittest.main()
