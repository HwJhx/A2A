"""阶段 5c:Broker(单实例、启动恢复、队列头调度、暂停与放行、告警、全局停止)。用有状态的假 herdr。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

from a2a.audit import AuditLog
from a2a.broker import Broker, BrokerAlreadyRunning
from a2a.errors import HerdrAgentBlocked, HerdrNotFound, HerdrPromptFailed
from a2a.identity import AgentIdentity
from a2a.messages import (DELIVERED, DELIVERY_UNCERTAIN, DISPATCHING, QUEUED, TARGET_BLOCKED, Message,
                          new_msg_id, now_iso)
from a2a.policy import BrokerConfig
from a2a.registry import Registry
from a2a.spool import Spool

SESSION = "walk1"
SRC = Path(__file__).resolve().parents[1] / "src"


class StatefulHerdr:
    """每个 agent 名字一个状态;prompt 的结果可按名字指定(异常或状态)。记录 prompt 顺序。"""

    def __init__(self):
        self.session = SESSION
        self.status: Dict[str, str] = {}
        self.pane: Dict[str, str] = {}
        self.on_prompt: Dict[str, object] = {}
        self.prompts: List[tuple] = []
        self.server_up = True

    def add(self, name, pane, status="idle"):
        self.status[name], self.pane[name] = status, pane

    def is_server_running(self):
        return self.server_up

    def agent_get(self, target):
        if target not in self.status:
            raise HerdrNotFound("x", code="agent_not_found")
        return {"agent_status": self.status[target], "pane_id": self.pane[target]}

    def agent_wait(self, target, *, until=None, timeout_ms=None):
        return self.agent_get(target)

    def agent_prompt(self, target, text, *, wait=False, until=None, timeout_ms=None):
        if target not in self.status:
            raise HerdrNotFound("x", code="agent_not_found")
        if self.status[target] == "blocked":
            raise HerdrAgentBlocked("x", code="agent_blocked")
        self.prompts.append((target, text))
        outcome = self.on_prompt.get(target, "working")
        if isinstance(outcome, BaseException):
            raise outcome
        return {"agent_status": outcome, "pane_id": self.pane[target]}


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.spool = Spool(self.dir / "spool")
        self.registry = Registry(self.dir / "registry.json")
        self.audit = AuditLog(self.dir / "audit.jsonl")
        self.herdr = StatefulHerdr()
        for ip, pane in (("uart", "w1:p2"), ("gpio", "w1:p3")):
            self.registry.register(AgentIdentity("soc_a", ip, "sw", f"sw_{ip}"), session=SESSION,
                                   workspace_id="w1", tab_id="w1:t2", pane_id=pane)
            self.herdr.add(f"sw_{ip}", pane)
        self.now = [datetime(2026, 10, 9, tzinfo=timezone.utc).timestamp()]
        self.broker = self.make_broker()

    def make_broker(self, **kw):
        return Broker(spool=self.spool, registry=self.registry, audit=self.audit, client=self.herdr,
                      session=SESSION, state_dir=self.dir, semantics_verified=True, herdr_version="0.9.3",
                      config=BrokerConfig(), sleep=lambda s: None, wall_clock=lambda: self.now[0], **kw)

    def send(self, dst="sw_uart", text=None, session=SESSION):
        return self.spool.enqueue(Message(
            msg_id=new_msg_id(), created_at=now_iso(), edge_id="dv_done", src="dv_" + dst[3:], dst=dst,
            project_id="soc_a", ip_id=dst[3:], session=session, text=text or f"msg {new_msg_id()}", state=QUEUED,
            topology_revision="r"))

    def events(self, state):
        return [ev for ev in self.audit.read() if ev.get("state") == state]

    def scan(self):
        return self.broker.scan(threaded=False)


class Scheduling(Base):
    def test_delivers_in_queue_order_and_releases_each_slot(self):
        msgs = [self.send(text=f"第{i}条") for i in range(3)]
        self.assertEqual(self.scan(), {"sw_uart": "empty"})
        self.assertEqual([t for _, t in self.herdr.prompts], ["第0条", "第1条", "第2条"])
        self.assertTrue(all(self.spool.get(m.msg_id).state == DELIVERED for m in msgs))
        self.assertEqual(len(self.events("QUEUE_RELEASED")), 3)
        self.assertIsNone(self.spool.head("sw_uart"))

    def test_uncertain_head_pauses_only_its_target(self):
        self.herdr.on_prompt["sw_uart"] = HerdrPromptFailed("x", code="agent_prompt_failed")
        first, second = self.send(text="一"), self.send(text="二")
        other = self.send("sw_gpio", text="别的目标")
        result = self.scan()
        self.assertEqual(result, {"sw_uart": "paused", "sw_gpio": "empty"})
        self.assertEqual(self.spool.get(first.msg_id).state, DELIVERY_UNCERTAIN)
        self.assertEqual(self.spool.get(second.msg_id).state, QUEUED)       # 后续消息没发
        self.assertEqual(self.spool.get(other.msg_id).state, DELIVERED)     # 其他目标照常
        paused = self.events("QUEUE_PAUSED")
        self.assertEqual((len(paused), paused[0]["msg_id"]), (1, first.msg_id))
        # 再扫多次:不重复暂停事件,也不再调用 herdr
        calls = len(self.herdr.prompts)
        self.scan()
        self.scan()
        self.assertEqual((len(self.events("QUEUE_PAUSED")), len(self.herdr.prompts)), (1, calls))

    def test_paused_head_resumes_after_a_ruling(self):
        self.herdr.on_prompt["sw_uart"] = HerdrPromptFailed("x", code="agent_prompt_failed")
        first, second = self.send(text="一"), self.send(text="二")
        self.scan()
        self.herdr.on_prompt.pop("sw_uart")
        self.spool.update(first.msg_id, state=DELIVERED, detail="操作员裁定已送达")  # 模拟 5d 的裁定
        self.scan()
        self.assertEqual(self.spool.get(second.msg_id).state, DELIVERED)
        self.assertEqual([t for _, t in self.herdr.prompts], ["一", "二"])  # 第一条没有被重发

    def test_definite_failure_pauses_until_operator_releases(self):
        self.herdr.status["sw_uart"] = "blocked"
        first, second = self.send(text="一"), self.send(text="二")
        self.assertEqual(self.scan(), {"sw_uart": "paused"})
        self.assertEqual(self.spool.get(first.msg_id).state, TARGET_BLOCKED)
        self.assertEqual(self.spool.get(second.msg_id).state, QUEUED)
        self.assertEqual(self.events("QUEUE_PAUSED")[0]["head_state"], TARGET_BLOCKED)
        self.herdr.status["sw_uart"] = "idle"
        self.scan()
        self.assertEqual(self.spool.get(second.msg_id).state, QUEUED)  # 目标恢复也不自动放行
        self.spool.release("sw_uart", first.queue_seq, reason="abandon_and_continue", ruling_id="r1")  # 模拟放弃并继续
        self.scan()
        self.assertEqual(self.spool.get(second.msg_id).state, DELIVERED)

    def test_foreign_session_messages_are_left_alone(self):
        m = self.send(session="other")
        self.assertEqual(self.scan(), {"sw_uart": "foreign"})
        self.assertEqual(self.spool.get(m.msg_id).state, QUEUED)
        self.assertEqual(self.herdr.prompts, [])

    def test_server_down_pauses_everything_without_burning_retries(self):
        m = self.send()
        self.herdr.server_up = False
        self.assertEqual(self.scan(), {})
        self.assertEqual((self.spool.get(m.msg_id).state, self.spool.get(m.msg_id).detail), (QUEUED, ""))
        self.herdr.server_up = True
        self.scan()
        self.assertEqual(self.spool.get(m.msg_id).state, DELIVERED)


class Threaded(Base):
    def test_run_forever_with_real_worker_threads(self):
        import threading
        for i in range(3):
            self.send(text=f"u{i}")
            self.send("sw_gpio", text=f"g{i}")
        broker = Broker(spool=self.spool, registry=self.registry, audit=self.audit, client=self.herdr,
                        session=SESSION, state_dir=self.dir, semantics_verified=True,
                        config=BrokerConfig(scan_interval_s=0.05))
        thread = threading.Thread(target=broker.run_forever)
        thread.start()
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and len(self.herdr.prompts) < 6:
                time.sleep(0.05)
        finally:
            broker.stop_event.set()
            thread.join(timeout=10)
        self.assertEqual([t for d, t in self.herdr.prompts if d == "sw_uart"], ["u0", "u1", "u2"])
        self.assertEqual([t for d, t in self.herdr.prompts if d == "sw_gpio"], ["g0", "g1", "g2"])


class StartupRecovery(Base):
    def test_dispatching_becomes_uncertain_and_is_not_resent(self):
        m = self.send()
        self.spool.update(m.msg_id, state=DISPATCHING)  # 上次崩溃留下的写前标记
        summary = self.broker.recover()
        self.assertEqual(summary["dispatching_to_uncertain"], 1)
        self.assertEqual(self.spool.get(m.msg_id).state, DELIVERY_UNCERTAIN)
        self.assertEqual(self.scan(), {"sw_uart": "paused"})
        self.assertEqual(self.herdr.prompts, [])
        self.assertEqual(len(self.events("BROKER_STARTED")), 1)

    def test_corrupt_queue_file_fails_closed_for_that_target_only(self):
        self.send()
        other = self.send("sw_gpio")
        (self.dir / "spool" / "queues" / "sw_uart.json").write_text("{坏")
        summary = self.broker.recover()
        self.assertEqual(summary["failed_closed_targets"], ["sw_uart"])
        self.scan()
        self.assertEqual(self.spool.get(other.msg_id).state, DELIVERED)
        self.assertEqual([t for t, _ in self.herdr.prompts], ["sw_gpio"])

    def test_unreadable_pending_message_halts_all_dispatch(self):
        self.send()
        bad = self.dir / "spool" / "pending" / "sw_gpio" / f"{new_msg_id()}.json"
        bad.parent.mkdir(parents=True)
        bad.write_text("{坏")
        self.broker.recover()
        self.assertIsNotNone(self.broker.halted())
        self.assertEqual(len(self.events("DISPATCH_HALTED")), 1)
        self.assertEqual(self.scan(), {})
        self.assertEqual(self.herdr.prompts, [])

    def test_single_instance(self):
        self.broker.acquire()
        self.addCleanup(self.broker.release_lock)
        with self.assertRaises(BrokerAlreadyRunning):
            self.make_broker().acquire()

    def test_single_instance_across_processes(self):
        self.broker.acquire()
        self.addCleanup(self.broker.release_lock)
        script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(SRC)!r})
            from pathlib import Path
            from a2a.broker import Broker, BrokerAlreadyRunning
            from a2a.spool import Spool
            from a2a.registry import Registry
            from a2a.audit import AuditLog
            d = Path({str(self.dir)!r})
            b = Broker(spool=Spool(d / "spool"), registry=Registry(d / "registry.json"),
                       audit=AuditLog(d / "audit.jsonl"), client=None, session="walk1", state_dir=d,
                       semantics_verified=True)
            try:
                b.acquire()
            except BrokerAlreadyRunning:
                sys.exit(7)
            sys.exit(0)
        """)
        self.assertEqual(subprocess.run([sys.executable, "-c", script]).returncode, 7)


class Alerts(Base):
    def make_uncertain(self, hours_ago):
        self.herdr.on_prompt["sw_uart"] = HerdrPromptFailed("x", code="agent_prompt_failed")
        m = self.send()
        self.scan()
        self.now[0] = datetime.now(timezone.utc).timestamp() + hours_ago * 3600
        return m

    def test_remind_after_one_hour_escalate_after_a_day_without_changing_state(self):
        m = self.make_uncertain(0.5)
        self.broker.check_alerts()
        self.assertEqual(self.events("ALERT"), [])
        self.now[0] += 0.6 * 3600
        self.broker.check_alerts()
        self.broker.check_alerts()
        alerts = self.events("ALERT")
        self.assertEqual([a["level"] for a in alerts], ["remind"])
        self.assertIn(f"a2a resolve {m.msg_id}", alerts[0]["detail"])
        self.now[0] += 24 * 3600
        self.broker.check_alerts()
        self.assertEqual([a["level"] for a in self.events("ALERT")], ["remind", "escalate"])
        self.assertEqual(self.spool.get(m.msg_id).state, DELIVERY_UNCERTAIN)  # 只告警,不迁移
        self.assertEqual(self.scan(), {})  # 仍然暂停


@unittest.skipUnless(sys.platform != "win32", "需要 POSIX 信号")
class KillMinus9(unittest.TestCase):
    """真实子进程:broker 在调用 herdr 期间被 kill -9,重启后不重发、不跳过队列头。用假 herdr 可执行文件。"""

    def test_kill_during_prompt_then_restart(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        d = Path(temp.name)
        fake = d / "fake-herdr"
        prompts = d / "prompts.log"
        fake.write_text(textwrap.dedent(f"""\
            #!{sys.executable}
            import json, sys, time
            args = [a for a in sys.argv[1:]]
            if args and args[0] == "--version":
                print("herdr 0.9.3"); sys.exit(0)
            if "--session" in args:
                i = args.index("--session"); del args[i:i + 2]
            if args[:2] == ["workspace", "list"]:
                print(json.dumps({{"result": {{"workspaces": []}}}})); sys.exit(0)
            if args[:2] == ["agent", "get"]:
                print(json.dumps({{"result": {{"agent": {{"agent_status": "idle", "pane_id": "w1:p2"}}}}}})); sys.exit(0)
            if args[:2] == ["agent", "prompt"]:
                with open({str(prompts)!r}, "a") as f:
                    f.write(args[3] + "\\n")
                time.sleep(60)   # 在这里被 kill -9
            sys.exit(1)
        """))
        fake.chmod(0o755)
        env = dict(os.environ, A2A_STATE_DIR=str(d / "state"), PYTHONPATH=str(SRC), PYTHONDONTWRITEBYTECODE="1")
        state = d / "state"
        spool, registry = Spool(state / "spool"), Registry(state / "registry.json")
        registry.register(AgentIdentity("soc_a", "uart", "sw", "sw_uart"), session=SESSION, workspace_id="w1",
                          tab_id="w1:t2", pane_id="w1:p2")
        msgs = [spool.enqueue(Message(msg_id=new_msg_id(), created_at=now_iso(), edge_id="e", src="dv_uart",
                                      dst="sw_uart", project_id="soc_a", ip_id="uart", session=SESSION,
                                      text=f"第{i}条", state=QUEUED, topology_revision="r")) for i in range(2)]
        proc = subprocess.Popen([sys.executable, "-m", "a2a.cli", "broker", "run", "--session", SESSION,
                                 "--herdr-bin", str(fake)], env=env, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not (prompts.exists() and prompts.read_text()):
                time.sleep(0.1)
            self.assertTrue(prompts.exists(), "broker 没有调用 agent prompt")
            self.assertEqual(spool.get(msgs[0].msg_id).state, DISPATCHING)  # 写前标记已落盘
        finally:
            proc.kill()          # SIGKILL:不给 broker 任何清理机会
            proc.wait(timeout=10)
        # 重启(只跑一遍):第一条转为不确定、不重发;第二条不被投递
        result = subprocess.run([sys.executable, "-m", "a2a.cli", "broker", "run", "--session", SESSION, "--once",
                                 "--herdr-bin", str(fake)], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(spool.get(msgs[0].msg_id).state, DELIVERY_UNCERTAIN)
        self.assertEqual(spool.get(msgs[1].msg_id).state, QUEUED)
        self.assertEqual(prompts.read_text().splitlines(), ["第0条"])  # 只发过一次


if __name__ == "__main__":
    unittest.main()
