"""阶段 5d:操作员裁定(先落盘、再生效、幂等补做、作废)与命令行。用有状态的假 herdr(见 test_broker)。"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from a2a import cli, rulings
from a2a.audit import AuditLog
from a2a.broker import Broker
from a2a.errors import HerdrPromptFailed
from a2a.identity import AgentIdentity
from a2a.messages import DELIVERED, DELIVERY_UNCERTAIN, FAILED, QUEUED, RETRYING, TARGET_BLOCKED
from a2a.policy import BrokerConfig
from a2a.registry import Registry
from a2a.spool import Spool
from a2a.topology import TopologyStore
from test_broker import SESSION, StatefulHerdr

TOPOLOGY = {
    "version": 1, "project_id": "soc_a", "roles": {"dv": {}, "sw": {}}, "ips": ["uart", "gpio"],
    "edges": [{"id": "dv_done", "from": "dv", "to": "sw", "template": "{ip}已完成UVM验证,请开发驱动。"}],
}
SRC = Path(__file__).resolve().parents[1] / "src"
PANES = {"dv_uart": "w1:p2", "sw_uart": "w1:p3", "dv_gpio": "w1:p4", "sw_gpio": "w1:p5"}


class Env(unittest.TestCase):
    """真实的状态目录(A2A_STATE_DIR)+ 拓扑 + 注册表;命令行在进程内调用 cli.main。"""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        patcher = mock.patch.dict(os.environ, {"A2A_STATE_DIR": str(self.state)}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("A2A_TOPOLOGY", None)
        self.store = TopologyStore.create(self.state / "topology.yaml", TOPOLOGY)
        self.registry = Registry(self.state / "registry.json")
        self.spool = Spool(self.state / "spool")
        self.audit = AuditLog(self.state / "audit.jsonl")
        self.herdr = StatefulHerdr()
        for agent_id, pane in PANES.items():
            role, ip = agent_id.split("_")
            self.registry.register(AgentIdentity.create(self.store.current(), role, ip), session=SESSION,
                                   workspace_id="w1", tab_id="w1:t2", pane_id=pane)
            self.herdr.add(agent_id, pane)
        self.broker = Broker(spool=self.spool, registry=self.registry, audit=self.audit, client=self.herdr,
                             session=SESSION, state_dir=self.state, semantics_verified=True,
                             config=BrokerConfig(), sleep=lambda s: None)

    def cli(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env or {}), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        text = out.getvalue().strip()
        return code, (json.loads(text) if text else None), err.getvalue()

    def send(self, ip="uart"):
        env = {"A2A_PROJECT_ID": "soc_a", "A2A_ROLE": "dv", "A2A_IP": ip, "HERDR_PANE_ID": PANES["dv_" + ip],
               "HERDR_SESSION": SESSION}
        code, out, err = self.cli("send", "dv_done", env=env)
        self.assertEqual(code, 0, err)
        return out["msg_id"]

    def events(self, state):
        return [e for e in self.audit.read() if e.get("state") == state]

    def scan(self):
        return self.broker.scan(threaded=False)

    def uncertain_head(self):
        self.herdr.on_prompt["sw_uart"] = HerdrPromptFailed("x", code="agent_prompt_failed")
        first, second = self.send(), self.send()
        self.scan()
        self.herdr.on_prompt.pop("sw_uart")
        self.assertEqual(self.spool.get(first).state, DELIVERY_UNCERTAIN)
        return first, second

    def failed_head(self):
        self.herdr.status["sw_uart"] = "blocked"
        first, second = self.send(), self.send()
        self.scan()
        self.herdr.status["sw_uart"] = "idle"
        self.assertEqual(self.spool.get(first).state, TARGET_BLOCKED)
        return first, second


class ResolveUncertain(Env):
    def test_delivered_releases_and_never_resends(self):
        first, second = self.uncertain_head()
        code, out, err = self.cli("resolve", first, "delivered", "--reason", "看到目标在处理")
        self.assertEqual(code, 0, err)
        self.assertEqual(out["ruling"], "delivered")
        self.assertEqual(self.spool.get(first).state, DELIVERED)
        ruling = self.events("OPERATOR_RULING")[0]
        self.assertEqual((ruling["previous_state"], ruling["new_state"], ruling["evidence"]),
                         (DELIVERY_UNCERTAIN, DELIVERED, "operator_confirmed"))
        self.assertEqual(ruling["actor"], cli._actor())
        order = [e["state"] for e in self.audit.read() if e.get("ruling_id") == ruling["ruling_id"]]
        self.assertEqual(order, ["OPERATOR_RULING", DELIVERED, "QUEUE_RELEASED", "RULING_APPLIED"])  # 先落盘
        self.scan()
        self.assertEqual(self.spool.get(second).state, DELIVERED)
        self.assertEqual(len(self.herdr.prompts), 2)  # 第一条只发过一次(第一次 prompt 返回失败但已写入)

    def test_retry_keeps_the_slot_then_broker_resends(self):
        first, second = self.uncertain_head()
        self.assertEqual(self.cli("resolve", first, "retry", "--reason", "确认目标没收到")[0], 0)
        self.assertEqual(self.spool.get(first).state, RETRYING)
        self.assertEqual(self.spool.head("sw_uart").active.msg_id, first)  # 仍占队列头
        self.scan()
        self.assertEqual([self.spool.get(m).state for m in (first, second)], [DELIVERED, DELIVERED])
        self.assertEqual(len(self.herdr.prompts), 3)  # 第一条重发了一次

    def test_abandon_fails_and_releases(self):
        first, second = self.uncertain_head()
        self.assertEqual(self.cli("resolve", first, "abandon", "--reason", "不要了")[0], 0)
        self.assertEqual(self.spool.get(first).state, FAILED)
        self.scan()
        self.assertEqual(self.spool.get(second).state, DELIVERED)


class ResolveDefiniteFailure(Env):
    def test_retry_creates_a_new_message_in_the_same_slot(self):
        first, second = self.failed_head()
        code, out, _ = self.cli("resolve", first, "retry", "--reason", "目标已处理完审批")
        self.assertEqual(code, 0)
        retry = self.spool.get(out["retry_msg_id"])
        self.assertEqual((retry.retry_of, retry.queue_seq, retry.state), (first, self.spool.get(first).queue_seq, QUEUED))
        ruling = self.events("OPERATOR_RULING")[0]
        self.assertEqual((ruling["ruling"], ruling["previous_state"], ruling["new_state"]),
                         ("retry_terminal", TARGET_BLOCKED, TARGET_BLOCKED))  # 原消息状态不变
        self.scan()
        self.assertEqual([self.spool.get(m).state for m in (out["retry_msg_id"], second)], [DELIVERED, DELIVERED])

    def test_abandon_and_continue(self):
        first, second = self.failed_head()
        self.assertEqual(self.cli("resolve", first, "abandon", "--reason", "跳过")[0], 0)
        self.assertEqual(self.events("OPERATOR_RULING")[0]["ruling"], "abandon_and_continue")
        self.assertEqual(self.spool.get(first).state, TARGET_BLOCKED)
        self.scan()
        self.assertEqual(self.spool.get(second).state, DELIVERED)

    def test_invalid_rulings_change_nothing(self):
        first, second = self.failed_head()
        for argv in (("resolve", first, "delivered", "--reason", "x"),     # 终态不可改判已送达
                     ("resolve", second, "abandon", "--reason", "x"),      # 不是队列头
                     ("resolve", first, "abandon", "--reason", "  ")):     # 没有理由
            code, _, err = self.cli(*argv)
            self.assertEqual(code, cli.EXIT_INVALID, argv)
        self.assertEqual(self.events("OPERATOR_RULING"), [])
        self.assertEqual(self.cli("resolve", "0000000000000001-abcdef", "retry", "--reason", "x")[0],
                         cli.EXIT_NOT_FOUND)


class CrashAndReconcile(Env):
    def recorded_but_not_applied(self, msg_id, action):
        event = rulings.plan(self.spool, msg_id, action, actor="op", reason="r")
        self.audit.record(event)  # 落盘后、生效前崩溃
        return event

    def test_unapplied_ruling_is_completed_by_broker_restart_exactly_once(self):
        first, second = self.uncertain_head()
        event = self.recorded_but_not_applied(first, "delivered")
        summary = self.broker.recover()
        self.assertEqual(summary["rulings_reconciled"], 1)
        self.assertEqual(self.spool.get(first).state, DELIVERED)
        applied = self.events("RULING_APPLIED")
        self.assertEqual((len(applied), applied[0]["detail"]), (1, "恢复时补做"))
        self.assertEqual(self.broker.recover()["rulings_reconciled"], 0)  # 再次启动不重复补做
        self.assertEqual(len([e for e in self.audit.read() if e.get("state") == DELIVERED
                              and e.get("ruling_id") == event["ruling_id"]]), 1)

    def test_crash_after_transition_before_release_is_finished_without_repeating(self):
        first, second = self.uncertain_head()
        event = self.recorded_but_not_applied(first, "abandon")
        # 第一个效果做了(状态与 ruling_id 同一次落盘),放行没做
        self.spool.update(first, state=FAILED, detail="已迁移,随后崩溃", ruling_id=event["ruling_id"])
        self.broker.recover()
        self.assertTrue(self.spool.head("sw_uart").active.msg_id == second)  # 已放行
        self.assertEqual(len(self.events("RULING_APPLIED")), 1)
        # Codex 审核应修 4:迁移的审计在崩溃前没写,补做时要补记(且只记一次)
        self.assertEqual(len(self.ruled(FAILED, event)), 1)
        self.assertEqual(len(self.ruled("QUEUE_RELEASED", event)), 1)

    def ruled(self, state, event):
        return [e for e in self.events(state) if e.get("ruling_id") == event["ruling_id"]]

    def test_crash_after_release_before_its_audit_backfills_the_release_event(self):
        first, second = self.uncertain_head()
        event = self.recorded_but_not_applied(first, "abandon")
        self.spool.update(first, state=FAILED, detail="已迁移", ruling_id=event["ruling_id"])
        self.audit.record({"state": FAILED, "msg_id": first, "ruling_id": event["ruling_id"], "dst": "sw_uart"})
        self.spool.release("sw_uart", event["queue_seq"], reason="abandon", ruling_id=event["ruling_id"])
        self.broker.recover()  # 放行已落盘,QUEUE_RELEASED 没写
        self.assertEqual(len(self.ruled("QUEUE_RELEASED", event)), 1)
        self.assertEqual(len(self.ruled(FAILED, event)), 1)  # 已有的不重复记
        self.assertEqual(len(self.events("RULING_APPLIED")), 1)

    def test_retry_enqueue_audit_is_not_duplicated_on_reconcile(self):
        first, _ = self.failed_head()
        event = self.recorded_but_not_applied(first, "retry")
        rulings.apply(self.spool, self.audit, event)
        # 模拟 RULING_APPLIED 写之前崩溃:删掉它再恢复
        lines = [line for line in self.audit.path.read_text().splitlines() if "RULING_APPLIED" not in line]
        self.audit.path.write_text("\n".join(lines) + "\n")
        self.broker.recover()
        self.assertEqual(len([e for e in self.ruled(QUEUED, event) if e["msg_id"] == event["retry_msg_id"]]), 1)
        self.assertEqual(len(self.events("RULING_APPLIED")), 1)

    def test_broker_auto_release_is_not_audited_twice(self):
        # 裁定为已送达、迁移已落盘;broker 随后自动放行;再补做裁定时不另记一次放行
        first, second = self.uncertain_head()
        event = self.recorded_but_not_applied(first, "delivered")
        self.spool.update(first, state=DELIVERED, detail="已迁移", ruling_id=event["ruling_id"])
        self.scan()
        self.broker.recover()
        released = [e for e in self.events("QUEUE_RELEASED") if e.get("queue_seq") == event["queue_seq"]]
        self.assertEqual(len(released), 1)
        self.assertEqual(released[0].get("ruling_id"), event["ruling_id"])  # 自动放行与裁定关联
        self.assertEqual(self.spool.get(second).state, DELIVERED)

    def test_auto_release_after_a_delivered_ruling_is_linked_to_it(self):
        # Codex 复核应修 1:裁定"已送达"后 broker 先自动放行,放行审计要带 ruling_id
        first, second = self.uncertain_head()
        event = self.recorded_but_not_applied(first, "delivered")
        self.spool.update(first, state=DELIVERED, detail="已迁移", ruling_id=event["ruling_id"])
        self.scan()
        released = [e for e in self.events("QUEUE_RELEASED") if e.get("queue_seq") == event["queue_seq"]]
        self.assertEqual([e.get("ruling_id") for e in released], [event["ruling_id"]])
        self.assertEqual(self.spool.head("sw_uart"), None)

    def test_ruling_releases_first_and_broker_sees_already_released(self):
        # Codex 第二次复核应修:broker 读到 DELIVERED 队列头之后、放行之前,裁定线程先放行并写了审计;
        # broker 的 release 得到 already_released,不能再写一条不带 ruling_id 的放行记录
        first, second = self.uncertain_head()
        event = self.recorded_but_not_applied(first, "delivered")
        self.spool.update(first, state=DELIVERED, detail="已迁移", ruling_id=event["ruling_id"])
        real_release, interleaved = self.spool.release, []

        def ruling_first_then_release(*args, **kwargs):
            if not interleaved:  # broker 的第一次放行调用:此时它已读到 DELIVERED 队列头
                interleaved.append(True)
                with mock.patch.object(self.spool, "release", side_effect=real_release):
                    rulings.apply(self.spool, self.audit, event)  # 裁定线程恰好先放行并写审计
            return real_release(*args, **kwargs)

        with mock.patch.object(self.spool, "release", side_effect=ruling_first_then_release):
            self.scan()
        self.assertEqual(interleaved, [True])
        released = [e for e in self.events("QUEUE_RELEASED") if e.get("queue_seq") == event["queue_seq"]]
        self.assertEqual([e.get("ruling_id") for e in released], [event["ruling_id"]])
        self.assertEqual(self.spool.get(second).state, DELIVERED)  # broker 照常继续处理后续消息

    def test_auto_release_after_an_earlier_retry_ruling_is_not_attributed_to_it(self):
        # 消息上的 ruling_id 来自"未送达,重试",之后由 broker 正常送达:放行不归到那条裁定
        first, _ = self.uncertain_head()
        code, out, _ = self.cli("resolve", first, "retry", "--reason", "确认没写入")
        self.assertEqual(code, 0)
        self.scan()
        self.assertEqual(self.spool.get(first).state, DELIVERED)
        released = [e for e in self.events("QUEUE_RELEASED") if e.get("msg_id") == first]
        self.assertEqual([e.get("ruling_id") for e in released], [None])

    def test_concurrent_resolves_of_the_same_head_record_only_one_ruling(self):
        # Codex 复核应修 2:两个 resolve 进程同时裁定同一个队列头,只能有一个落盘生效,另一个被拒绝
        first, _ = self.uncertain_head()
        env = dict(os.environ, PYTHONPATH=str(SRC), A2A_STATE_DIR=str(self.state))
        with rulings.lock(self.state):  # 先占住锁,让两个进程都卡在检查之前,再同时放开
            procs = [subprocess.Popen([sys.executable, "-m", "a2a.cli", "resolve", first, action, "--reason", "并发"],
                                      env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for action in ("delivered", "abandon")]
            time.sleep(1.5)
            self.assertEqual(self.events("OPERATOR_RULING"), [])
        codes = sorted(p.wait(timeout=30) for p in procs)
        for p in procs:
            p.stdout.close()
            p.stderr.close()
        self.assertEqual(codes, [0, cli.EXIT_INVALID])
        self.assertEqual(len(self.events("OPERATOR_RULING")), 1)
        self.assertEqual(len(self.events("RULING_APPLIED")), 1)
        self.broker.recover()
        self.assertIsNone(self.broker.halted())  # 没有留下补做不了的裁定

    def test_applied_retry_is_not_redone_after_the_message_became_uncertain_again(self):
        # "重试"已生效、broker 重发后消息又不确定;此时补做不能再重试一次(否则重复投递)
        first, _ = self.uncertain_head()
        event = self.recorded_but_not_applied(first, "retry")
        self.spool.update(first, state=RETRYING, ruling_id=event["ruling_id"])  # 已生效,RULING_APPLIED 没写
        self.spool.update(first, state="DISPATCHING")
        self.spool.update(first, state=DELIVERY_UNCERTAIN)
        self.broker.recover()
        self.assertIsNone(self.broker.halted())
        self.assertEqual(self.spool.get(first).state, DELIVERY_UNCERTAIN)  # 没有再次迁到 RETRYING
        self.assertEqual(len(self.events("RULING_APPLIED")), 1)

    def test_transition_without_ruling_id_is_a_contradiction(self):
        first, _ = self.uncertain_head()
        self.recorded_but_not_applied(first, "abandon")
        self.spool.update(first, state=FAILED, detail="不知谁改的")  # 不是这条裁定改的
        self.broker.recover()
        self.assertIn("矛盾", self.broker.halted())

    def test_crash_after_retry_enqueue_does_not_create_a_second_retry(self):
        first, _ = self.failed_head()
        event = self.recorded_but_not_applied(first, "retry")
        self.spool.enqueue_retry(first, event["retry_msg_id"])  # 重试已入队,随后崩溃
        self.broker.recover()
        same_slot = [m for m in self.spool.pending("sw_uart") if m.queue_seq == self.spool.get(first).queue_seq]
        self.assertEqual([m.msg_id for m in same_slot], [event["retry_msg_id"]])

    def test_unreadable_ruling_halts_dispatch_until_voided_and_resumed(self):
        first, second = self.uncertain_head()
        with self.audit.path.open("a", encoding="utf-8") as f:   # 中间一行损坏的裁定
            f.write('{"state": "OPERATOR_RULING", "ruling_id": "r1", "msg_id": "坏\n')
        self.audit.record({"state": "NOTE", "detail": "后续正常记录"})
        self.broker.recover()
        self.assertIsNotNone(self.broker.halted())
        self.assertEqual(self.scan(), {})
        code, out, _ = self.cli("queue")
        fp = out["unreadable_rulings"][0]["fingerprint"]
        self.assertIsNotNone(out["dispatch_halted"])
        self.assertEqual(self.cli("dispatch", "resume", "--reason", "x")[0], cli.EXIT_INVALID)  # 未作废不能恢复
        self.assertEqual(self.cli("ruling", "void", "corrupt:" + fp, "--reason", "核实过",
                                  "--verified", "消息仍是不确定态,没有任何效果生效")[0], 0)
        self.assertEqual(self.cli("dispatch", "resume", "--reason", "已作废损坏裁定")[0], 0)
        self.assertIsNone(self.broker.halted())
        self.assertEqual(len(self.events("DISPATCH_RESUMED")), 1)
        # 作废后以实际状态为准:队列头仍是不确定态,操作员重新裁定
        self.assertEqual(self.cli("resolve", first, "delivered", "--reason", "重新裁定")[0], 0)
        self.scan()
        self.assertEqual(self.spool.get(second).state, DELIVERED)

    def test_quarantined_ruling_fragment_also_fails_closed(self):
        self.uncertain_head()
        self.audit.corrupt_path.write_text('# 2026 隔离 40 字节\n{"state": "OPERATOR_RULING", "ruli\n')
        self.broker.recover()
        self.assertIn("corrupt:", self.broker.halted())

    def test_contradicting_ruling_halts_and_can_be_voided(self):
        first, second = self.failed_head()
        event = self.recorded_but_not_applied(first, "abandon")
        self.spool.release("sw_uart", self.spool.get(first).queue_seq, reason="别人已经放行")
        self.spool.update(second, state="DISPATCHING")           # 制造矛盾:队列头已变
        self.spool.update(second, state=DELIVERY_UNCERTAIN)
        # abandon_and_continue 的放行是幂等的,所以这里不矛盾;构造一个真正矛盾的:未生效的 delivered 指向已失败的消息
        bad = dict(event, ruling_id=rulings.new_ruling_id(), ruling="delivered", previous_state="QUEUED",
                   new_state=DELIVERED, msg_id=second)
        self.audit.record(bad)
        self.broker.recover()
        self.assertIn(bad["ruling_id"], self.broker.halted())
        self.assertEqual(self.cli("ruling", "void", bad["ruling_id"], "--reason", "矛盾",
                                  "--verified", "无效果")[0], 0)
        pending, _ = rulings.unapplied(self.audit)
        self.assertNotIn(bad["ruling_id"], [p["ruling_id"] for p in pending])


class CommandLine(Env):
    def test_send_status_queue(self):
        msg_id = self.send()
        code, status, _ = self.cli("status", msg_id)
        self.assertEqual((code, status["state"], status["dst"], status["queue_seq"]), (0, QUEUED, "sw_uart", 1))
        code, queue, _ = self.cli("queue")
        self.assertEqual(queue["targets"][0]["msg_id"], msg_id)
        self.assertFalse(queue["targets"][0]["paused"])

    def test_rejected_send_and_its_status(self):
        env = {"A2A_PROJECT_ID": "soc_a", "A2A_ROLE": "sw", "A2A_IP": "uart", "HERDR_PANE_ID": PANES["sw_uart"],
               "HERDR_SESSION": SESSION}
        code, _, err = self.cli("send", "dv_done", env=env)  # 方向不对
        self.assertEqual(code, cli.EXIT_REJECTED)
        rejected = json.loads(err)
        self.assertEqual(rejected["rejected"], "wrong_direction")
        code, status, _ = self.cli("status", rejected["msg_id"])
        self.assertEqual((code, status["state"]), (0, "REJECTED"))

    def test_queue_shows_pause_reason_and_how_to_resolve(self):
        first, _ = self.uncertain_head()
        target = self.cli("queue", "sw_uart")[1]["targets"][0]
        self.assertTrue(target["paused"])
        self.assertIn(f"a2a resolve {first}", target["resolve"])
        self.assertEqual(target["backlog"], 1)

    def test_topology_commands_are_audited(self):
        self.assertEqual(self.cli("topology", "add-ip", "spi")[0], 0)
        self.assertIn("spi", self.cli("topology", "show")[1]["topology"]["ips"])
        self.assertEqual(self.cli("topology", "add-edge", "sw_ask", "sw", "dv", "{ip}驱动有疑问。")[0], 0)
        self.assertEqual(self.cli("topology", "remove-edge", "sw_ask")[0], 0)
        self.assertEqual(self.cli("topology", "remove-edge", "no_such")[0], cli.EXIT_INVALID)
        self.assertEqual(len(self.events("TOPOLOGY_CHANGED")), 3)

    def test_removing_an_edge_does_not_rewrite_queued_messages(self):
        # 回归:消息入队后删边,旧消息照常按队列规则投递;新消息被 Router 拒绝
        old = self.send()
        self.assertEqual(self.cli("topology", "remove-edge", "dv_done")[0], 0)
        env = {"A2A_PROJECT_ID": "soc_a", "A2A_ROLE": "dv", "A2A_IP": "uart", "HERDR_PANE_ID": PANES["dv_uart"],
               "HERDR_SESSION": SESSION}
        code, _, err = self.cli("send", "dv_done", env=env)
        self.assertEqual((code, json.loads(err)["rejected"]), (cli.EXIT_REJECTED, "unknown_edge"))
        self.scan()
        self.assertEqual(self.spool.get(old).state, DELIVERED)


if __name__ == "__main__":
    unittest.main()
