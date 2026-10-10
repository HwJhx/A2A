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
        self.agent_dir = self.state / "agent"
        self.agent_dir.mkdir()
        self.cwd = self.state / "work"
        self.cwd.mkdir()

    def env(self, role="dv", ip="uart", **changes):
        """spawn 注入 pane 的环境变量(见 Lifecycle._agent_env),加上 herdr 注入的 pane / 会话。"""
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
               "A2A_PROJECT_ID": "soc_a", "A2A_ROLE": role, "A2A_IP": ip,
               "A2A_STATE_DIR": str(self.state), "A2A_TOPOLOGY": str(self.state / "topology.yaml"),
               "A2A_PYTHON": sys.executable, "A2A_SRC": str(SRC),
               "HERDR_PANE_ID": PANES[f"{role}_{ip}"], "HERDR_SESSION": SESSION,
               # fnx 启动脚本导出的 agent 目录(pi 从这里读全局 settings.json);测试里是一个空目录
               "FORENYX_CODING_AGENT_DIR": str(self.agent_dir)}
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
        self.assertEqual(sorted(out["events"]), sorted(["tool_call", "agent_start", "agent_end", "session_before_compact"]))

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
        self.assertEqual((out["tools"], sorted(out["events"])), ([], sorted(["tool_call", "agent_start", "agent_end", "session_before_compact"])))
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

    # ---- 状态上报(阶段 8,方案 16 号 §2)-------------------------------------
    def fake_herdr(self, mode="ok"):
        """记录 `pane report-agent` 调用的假 herdr。mode:ok / fail(退出码 1)/ hang(卡住 10 秒)。"""
        path = self.state / f"fake_herdr_{mode}.sh"
        log = self.state / f"herdr_{mode}.log"
        body = {"ok": "exit 0", "fail": "exit 1", "hang": "exec sleep 10"}[mode]
        path.write_text(f'#!/bin/sh\necho "$*" >> {log}\n{body}\n')
        path.chmod(0o755)
        return str(path), log

    def run_events(self, events, mode="ok", **env):
        herdr, log = self.fake_herdr(mode)
        log.unlink(missing_ok=True)   # 每次运行单独记录
        out, err = self.harness(self.env(A2A_HERDR_BIN=herdr, **env), "events", json.dumps(events))
        calls = [line.split() for line in log.read_text().splitlines()] if log.exists() else []
        return out, err, calls

    @staticmethod
    def reported(calls):
        """[(state, seq)];并核对每次上报的固定参数。"""
        result = []
        for c in calls:
            assert c[:3] == ["pane", "report-agent", PANES["dv_uart"]], c
            args = dict(zip(c[3::2], c[4::2]))
            assert (args["--source"], args["--agent"]) == ("a2a", "pi"), c
            result.append((args["--state"], int(args["--seq"])))
        return result

    @staticmethod
    def end(stop="stop", tokens=10_000, window=1_000_000, usage_style="total"):
        """agent_end:最后一条 assistant 带 usage(与 pi 判断压缩用的数据相同);ctx 只提供窗口大小。"""
        assistant = {"role": "assistant"}
        if stop is not None:
            assistant["stopReason"] = stop
        if tokens is not None:
            assistant["usage"] = ({"totalTokens": tokens} if usage_style == "total" else
                                  {"input": tokens - 30, "output": 10, "cacheRead": 10, "cacheWrite": 10})
        return {"name": "agent_end", "event": {"messages": [{"role": "user"}, assistant]},
                "usage": {"tokens": None, "contextWindow": window}, "cwd": None}

    def run_end(self, end, **env):
        end = dict(end, cwd=str(self.cwd))
        _, _, calls = self.run_events([{"name": "agent_start"}, end], **env)
        return [s for s, _ in self.reported(calls)]

    def test_reports_working_on_start_and_idle_on_a_clean_end(self):
        _, _, calls = self.run_events([{"name": "agent_start"}, self.end("stop")])
        self.assertEqual(self.reported(calls), [("working", 1), ("idle", 2)])

    def test_aborted_end_reports_idle(self):
        _, _, calls = self.run_events([{"name": "agent_start"}, self.end("aborted")])
        self.assertEqual(self.reported(calls), [("working", 1), ("idle", 2)])

    def test_error_end_keeps_working_until_a_retry_finishes_cleanly(self):
        # 出错后 pi 可能自动重试:不报 idle;重试再次 agent_start,成功结束后才报 idle
        _, _, calls = self.run_events([{"name": "agent_start"}, self.end("error"),
                                       {"name": "agent_start"}, self.end("stop")])
        self.assertEqual(self.reported(calls), [("working", 1), ("working", 2), ("idle", 3)])

    def test_idle_threshold_uses_the_same_token_formula_as_pi_compaction(self):
        # 阈值 = (窗口 − 16384) × 90%;1M 窗口时为 885254.4
        limit = (1_000_000 - 16_384) * 0.9
        cases = {"低于阈值(totalTokens)": (self.end("stop", tokens=int(limit) - 1), True),
                 "低于阈值(input+output+cacheRead+cacheWrite)": (self.end("stop", tokens=500_000, usage_style="parts"), True),
                 "达到阈值": (self.end("stop", tokens=int(limit) + 1), False),
                 "没有 usage": (self.end("stop", tokens=None), False),
                 "窗口未知": (self.end("stop", window=0), False)}
        for name, (end, idle) in cases.items():
            with self.subTest(name):
                _, _, calls = self.run_events([{"name": "agent_start"}, end])
                expected = [("working", 1), ("idle", 2)] if idle else [("working", 1)]
                self.assertEqual(self.reported(calls), expected)

    def test_idle_threshold_follows_the_configured_compaction_reserve(self):
        # 预留可配置:预留 200000 时阈值 (1M − 200000) × 90% = 720000;850000 在默认阈值内,在自定义阈值外
        end = self.end("stop", tokens=850_000)
        self.assertEqual(self.run_end(end), ["working", "idle"])                       # 默认预留
        (self.agent_dir / "settings.json").write_text(json.dumps({"compaction": {"reserveTokens": 200_000}}))
        self.assertEqual(self.run_end(end), ["working"])                               # 全局设置
        (self.agent_dir / "settings.json").unlink()
        (self.cwd / ".forenyx").mkdir()
        (self.cwd / ".forenyx" / "settings.json").write_text(json.dumps({"compaction": {"reserveTokens": 200_000}}))
        self.assertEqual(self.run_end(end), ["working"])                               # 项目设置
        self.assertEqual(self.run_end(self.end("stop", tokens=700_000)), ["working", "idle"])

    def test_unreadable_settings_or_unknown_agent_dir_keeps_working(self):
        end = self.end("stop", tokens=10_000)
        (self.agent_dir / "settings.json").write_text("{坏")
        self.assertEqual(self.run_end(end), ["working"])
        (self.agent_dir / "settings.json").write_text(json.dumps({"compaction": {"reserveTokens": "很多"}}))
        self.assertEqual(self.run_end(end), ["working"])
        (self.agent_dir / "settings.json").unlink()
        self.assertEqual(self.run_end(end, FORENYX_CODING_AGENT_DIR=None), ["working"])  # 找不到 agent 目录

    def test_only_whitelisted_stop_reasons_report_idle(self):
        # 只有 stop(低用量)和 aborted 能确定已结束;其他或缺失一律保持 working
        for stop in ("length", "toolUse", "something_new", None):
            with self.subTest(stop=stop):
                _, _, calls = self.run_events([{"name": "agent_start"}, self.end(stop)])
                self.assertEqual(self.reported(calls), [("working", 1)])
        _, _, calls = self.run_events([{"name": "agent_start"}, self.end("aborted", tokens=None)])
        self.assertEqual(self.reported(calls), [("working", 1), ("idle", 2)])

    def test_compaction_reports_working_and_its_end_does_not_report_idle(self):
        _, _, calls = self.run_events([{"name": "session_before_compact"}, {"name": "session_compact"}])
        self.assertEqual(self.reported(calls), [("working", 1)])

    def test_hanging_herdr_delays_agent_start_by_about_two_seconds_at_most(self):
        out, err, calls = self.run_events([{"name": "agent_start"}], mode="hang")
        (start,) = out
        self.assertIsNone(start["error"])
        self.assertGreaterEqual(start["ms"], 1_900)         # 单次 1 秒超时 × 2 次
        self.assertLess(start["ms"], 3_500)                  # 明显短于 broker 的 5 秒观察窗口
        self.assertEqual(len(calls), 2)
        self.assertIn("上报 working 失败", err)

    def test_failed_idle_report_logs_how_to_recover(self):
        out, err, calls = self.run_events([self.end("stop")], mode="fail")
        self.assertIsNone(out[0]["error"])
        self.assertEqual([s for s, _ in self.reported(calls)], ["idle", "idle", "idle"])
        self.assertIn("上报 idle 失败", err)
        self.assertIn("a2a agent stop", err)

    def test_no_state_reporting_without_a_herdr_pane(self):
        out, _, calls = self.run_events([{"name": "agent_start"}, self.end("stop")], HERDR_PANE_ID=None)
        self.assertEqual(calls, [])

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
