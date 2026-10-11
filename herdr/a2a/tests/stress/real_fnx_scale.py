"""阶段 8:真实 fnx 规模测试(**会调用模型**)。方案见 herdr/claude/15-stage8-real-fnx-scale-plan.md。

N 个 IP,每个 IP 一个真实 fnx_dv、一个真实 fnx_sw(--no-builtin-tools,只有 a2a_send),跑阶段 6 的双向链路:
  操作员几乎同时给每个 dv 发开头提示 -> dv 发 dv_done -> sw 回 sw_test_pass / sw_test_fail -> dv 回复"收到"。

判定分两部分(方案 §6):
  框架正确性  每条已入队消息的发送方 / 目标 / 文字等于本 IP 模板,只送达一次,无异常状态,不串 IP;
              工具调用都是 a2a_send;工作目录为空。违例即框架失败,按 §7 停止。
  模型完成度  按 IP 记录链路是否走完、没走完的原因(模型侧),分开汇报。
调用上限:每 IP 入队不超过 3 条;模型请求(含出错)合计默认不超过 5×N×2。超过按 §7 停止,归为模型行为异常。
  注意这些上限是**软阈值**:每 2 秒从会话记录统计一次已写入的 assistant 消息,在途的请求、provider SDK
  内部的重试都不计入,所以实际请求数可能超过配置值(超出量取决于 2 秒内的并发);不能当作硬上限。

必须同时设置 A2A_REAL_FNX=1 并给出 --ips N 才会运行:
  A2A_REAL_FNX=1 python3 tests/stress/real_fnx_scale.py --ips 5
  A2A_REAL_FNX=1 python3 tests/stress/real_fnx_scale.py --ips 5 --no-kickoff   # 不发提示词、不调用模型,只验证准备与清理
结果在 --out(默认 ~/a2a_stage8_real/n<N>/)。成功时清理会话;触发停止条件时保留现场(会话、agent),只停 broker、删插件文件。
一旦开始发提示词,任何停止或未预料的异常都只保留现场,不再清理(模型可能还在运行)。

退出码:0 = 框架正确且模型完成度达到门槛(≥ 4/5 个 IP 走完);1 = 停止 / 失败 / 清理失败;
        2 = 未设置 A2A_REAL_FNX;3 = 框架正确,但模型完成度没达到门槛。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

HERE = Path(__file__).resolve().parent
SRC = HERE.parent.parent / "src"
sys.path.insert(0, str(SRC))

from a2a._fsutil import atomic_write_text  # noqa: E402
from a2a.audit import AuditLog  # noqa: E402
from a2a.herdr_client import HerdrClient, session_delete, session_list, session_stop  # noqa: E402
from a2a.messages import (DELIVERED, DELIVERY_UNCERTAIN, FAILED, REJECTED, TARGET_BLOCKED,  # noqa: E402
                          TARGET_MISSING, TIMEOUT)
from a2a.registry import Registry  # noqa: E402
from a2a.spool import Spool  # noqa: E402
from a2a.topology import TopologyStore  # noqa: E402

_spec = importlib.util.spec_from_file_location("stress_scale", HERE / "stress_scale.py")
stress = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stress)  # 复用采样器与 /proc 工具

PLUGIN = HERE.parent.parent / "pi-extension" / "a2a.ts"
HOME = Path.home()
EDGES = [
    {"id": "dv_done", "from": "dv", "to": "sw",
     "template": "{ip} ip 我已经完成了uvm验证，你需要对这个{ip} ip进行 驱动程序开发和HAL框架开发。"
                 "不要真的去做，直接用 a2a_send 回复测试成功或测试失败（二选一）"},
    {"id": "sw_test_pass", "from": "sw", "to": "dv",
     "template": "我已经完成了{ip} ip的驱动程序开发，测试成功。你只回复收到即可，不要真的去做"},
    {"id": "sw_test_fail", "from": "sw", "to": "dv",
     "template": "我已经完成了{ip} ip的驱动程序开发，测试失败。你只回复收到即可，不要真的去做"},
]
TEMPLATES = {e["id"]: e for e in EDGES}
KICKOFF = "假设你已经完成了 {ip} ip 的 UVM 验证，请用 a2a_send 工具通知软件智能体。"
ABNORMAL = {DELIVERY_UNCERTAIN, TIMEOUT, TARGET_BLOCKED, TARGET_MISSING, FAILED, REJECTED}
CALLS_PER_IP = 5            # 阶段 6 实测:dv 3 次、sw 2 次
MAX_MSGS_PER_IP = 3
CHAIN_TIMEOUT_S = 600
SETTLE_S = 30               # 一个 IP 的两个 agent 都空闲、无待投递,且这么久没有新动静,才算这个 IP 结束
API_ERRORS_PER_MIN = 5


class Stop(Exception):
    """触发方案 §7 的停止条件。kind: framework / model / resource。"""

    def __init__(self, kind: str, reason: str):
        super().__init__(reason)
        self.kind = kind


_RATE_LIMITED = re.compile(r"^\s*429\b")


def is_rate_limited(error_message: str) -> bool:
    """模型接口限流。pi 的错误记录没有单独的状态码字段,errorMessage 来自 OpenAI SDK,格式为
    "<状态码> status code (no body)" 或 "<状态码> <说明>"(B1 实测 128 条全是 "429 status code (no body)")。
    只认以状态码 429 开头的,避免说明文字里偶然出现 429 被误判。"""
    return bool(_RATE_LIMITED.match(error_message or ""))


def iso_to_epoch(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def session_dir_for(agent: str, cwd: Path) -> Path:
    """fnx 按工作目录建会话目录:去掉首尾 /,/ 换成 -,前后加 --。"""
    return HOME / ".forenyx" / agent / "agent" / "sessions" / ("--" + str(cwd).strip("/").replace("/", "-") + "--")


PARSE_ERRORS: Dict[str, int] = {}   # 会话记录文件 -> 解析失败的完整行数(不含正在写入的最后半行)


def read_transcript(directory: Path) -> List[dict]:
    records = []
    for path in sorted(directory.glob("*.jsonl")) if directory.is_dir() else []:
        raw = path.read_text(encoding="utf-8", errors="replace")
        lines = raw.split("\n")
        partial = lines.pop()   # 没有换行结尾的最后一段:fnx 可能正在写,这次不算
        bad = 0
        for line in lines:
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                bad += 1
        PARSE_ERRORS[str(path)] = bad
        if partial.strip():
            PARSE_ERRORS[str(path) + " (未写完的最后一行)"] = 1
        else:
            PARSE_ERRORS.pop(str(path) + " (未写完的最后一行)", None)
    return records


def assistant_calls(records: List[dict]) -> List[dict]:
    """每条 assistant 记录 = 一次模型调用。耗时 = 外层 timestamp(写入)- message.timestamp(请求开始)。"""
    calls = []
    for r in records:
        m = r.get("message") if r.get("type") == "message" else None
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        content = m.get("content") or []
        tools = [c.get("name") for c in content if isinstance(c, dict) and c.get("type") == "toolCall"]
        text = "".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
        start = m.get("timestamp") / 1000 if isinstance(m.get("timestamp"), (int, float)) else None
        end = iso_to_epoch(r["timestamp"]) if r.get("timestamp") else None
        usage = m.get("usage") or {}
        calls.append({"provider": m.get("provider"), "model": m.get("model"), "stop": m.get("stopReason"),
                      "error": (m.get("errorMessage") or "")[:200] if m.get("stopReason") == "error" else "",
                      "tools": tools, "text": text, "start": start, "end": end,
                      "latency_s": (end - start) if start and end else None,
                      "input": usage.get("input"), "output": usage.get("output")})
    return calls


def extensions_listed(screen: str) -> List[str]:
    """fnx 启动画面里 [Extensions] 下面列出的扩展文件名(缩进行,直到空行或下一个 [段落])。"""
    names, inside = [], False
    for line in screen.splitlines():
        if line.strip() == "[Extensions]":
            inside = True
            continue
        if inside:
            if not line.strip() or line.strip().startswith("[") or not line.startswith(" "):
                break
            names += [n.strip() for n in line.split(",") if n.strip()]
    return names


class Run:
    def __init__(self, args):
        self.args = args
        self.n = args.ips
        self.ips = [f"ip{i:02d}" for i in range(self.n)]
        self.out = Path(args.out or HOME / "a2a_stage8_real" / f"n{self.n}").resolve()
        if self.out.exists():
            raise SystemExit(f"{self.out} 已存在,先人工确认")
        self.state = self.out / "state"
        self.state.mkdir(parents=True)
        (self.out / "logs").mkdir()
        self.session = f"a2a_real_n{self.n}_{os.getpid()}"
        self.owns_tmux = self.owns_session = False
        self.env = dict(os.environ, A2A_STATE_DIR=str(self.state), PYTHONPATH=str(SRC), PYTHONDONTWRITEBYTECODE="1")
        self.step = "setup"
        self.abort_reason: Optional[str] = None    # 采样器在内存低于 20% 时设置
        self.broker: Optional[subprocess.Popen] = None
        self.server_pid = 0
        self.report: Dict[str, object] = {"ips": self.n, "session": self.session, "kickoff": not args.no_kickoff}
        self.plugin_files: List[tuple] = []        # (文件, 目录是否本次新建)
        self.roots: Dict[str, int] = {}            # agent_id -> fnx 进程 pid
        self.cwds: Dict[str, Path] = {}
        self.call_cap = CALLS_PER_IP * self.n * 2
        # 默认:所有调用(含出错)合计 ≤ call_cap、任何出错都计入"连续报错"。验证重试时可放宽(见 main 的参数)
        self.ignore_429 = args.ignore_429
        self.success_cap = args.success_cap
        self.request_cap = args.request_cap or self.call_cap
        wrapper = self.out / "herdr_wrapper.sh"    # 透明:记一行后 exec 真正的 herdr
        wrapper.write_text("#!/bin/bash\n"
                           f'printf "%s %s\\n" "$$" "$*" >> {self.out}/herdr_calls.log\n'
                           f'exec {shutil.which("herdr")} "$@"\n')
        wrapper.chmod(0o755)
        TopologyStore.create(self.state / "topology.yaml", {
            "version": 1, "project_id": "soc_a", "workspace_label": f"real-n{self.n}",
            "roles": {r: {"label": f"{r}智能体", "launcher": str(HOME / f".forenyx/fnx_{r}/bin/fnx_{r}"),
                          "launch_args": ["--no-builtin-tools"]} for r in ("dv", "sw")},
            "ips": self.ips, "edges": EDGES})
        self.registry = Registry(self.state / "registry.json")
        self.spool = Spool(self.state / "spool")
        self.audit = AuditLog(self.state / "audit.jsonl")
        self.client = HerdrClient(self.session)
        self.sampler = stress.Sampler(self)          # 用到 out / step / broker / server_pid / abort_reason
        self.fnx_pss: List[dict] = []
        self.kicked_off = False                    # 一旦开始发提示词,任何异常都只能保留现场,不能清理

    # ---- 准备 -----------------------------------------------------------
    def install_plugin(self):
        for agent in ("fnx_dv", "fnx_sw"):
            ext = HOME / ".forenyx" / agent / "agent" / "extensions"
            created = not ext.exists()
            if not created:
                for f in ext.glob("*.ts"):
                    if "a2a_send" in f.read_text(errors="replace"):
                        raise Stop("setup", f"{f} 也注册 a2a_send,不安装")
            ext.mkdir(exist_ok=True)
            dest = ext / f"a2a_scale_{os.getpid()}.ts"
            shutil.copyfile(PLUGIN, dest)
            self.plugin_files.append((dest, created))

    def remove_plugin(self) -> List[str]:
        problems = []
        for dest, created in self.plugin_files:
            try:
                dest.unlink(missing_ok=True)
                if created:
                    dest.parent.rmdir()
            except OSError as exc:
                problems.append(f"{dest}: {exc}")
        self.plugin_files = []
        return problems

    def setup_session(self):
        if any(r.get("name") == self.session for r in session_list()):
            raise Stop("setup", f"会话 {self.session} 已存在")
        before = stress.herdr_servers()
        subprocess.run(["tmux", "new-session", "-d", "-x", "200", "-y", "50", "-s", self.session + "_tty",
                        f"cd {HOME} && herdr session attach {self.session}"], check=True)
        self.owns_tmux = True
        deadline = time.monotonic() + 30
        while not self.client.is_server_running():
            if time.monotonic() > deadline:
                raise Stop("setup", "会话没有启动")
            time.sleep(0.2)
        if not any(r.get("name") == self.session for r in session_list()):
            raise Stop("setup", "会话不在 session list 里")
        self.owns_session = True
        new = stress.herdr_servers() - before
        if len(new) != 1:
            raise Stop("setup", f"新增 herdr server 进程 {len(new)} 个,无法确定被测进程")
        self.server_pid = new.pop()
        self.sampler.start()

    def spawn_all(self):
        self.step = "spawn"
        times = []
        for ip in self.ips:
            for role in ("dv", "sw"):
                agent_id = f"{role}_{ip}"
                cwd = self.out / "work" / agent_id
                cwd.mkdir(parents=True)
                self.cwds[agent_id] = cwd
                sdir = session_dir_for(f"fnx_{role}", cwd)
                if sdir.exists():
                    raise Stop("setup", f"{sdir} 已存在,无法确认会话记录归属")
                t0 = time.monotonic()
                proc = subprocess.run([sys.executable, "-m", "a2a.cli", "agent", "spawn", role, ip, "--cwd", str(cwd),
                                       "--session", self.session], env=self.env, capture_output=True, text=True,
                                      timeout=180)
                if proc.returncode != 0:
                    raise Stop("setup", f"spawn {agent_id} 失败:{proc.stderr[-300:]}")
                times.append(time.monotonic() - t0)
                info = self.client.pane_process_info(self.client.agent_get(agent_id)["pane_id"])
                self.roots[agent_id] = info["foreground_processes"][0]["pid"]
                if self.abort_reason:
                    raise Stop("resource", self.abort_reason)
        self.report["spawn_per_agent_s"] = stress.summary(times)
        # 发提示词之前确认 fnx 确实加载了本次的插件(TUI 的 [Extensions] 里列出文件名);否则不调用模型
        # 部署约束(方案 16 号 §2.1):只加载本次的 a2a 插件,没有其他可能在 agent_end 里排消息的扩展
        name = self.plugin_files[0][0].name
        wrong = {}
        for a in self.roots:
            listed = extensions_listed(self.client.agent_read(a, source="recent-unwrapped", lines=60))
            if listed != [name]:
                wrong[a] = listed
        if wrong:
            raise Stop("setup", f"这些 agent 加载的扩展不是只有 {name},不发提示词:{wrong}")
        self.report["plugin_loaded_check"] = f"全部 {len(self.roots)} 个 agent 的扩展列表都只有 {name}"

    def start_broker(self):
        self.broker = subprocess.Popen([sys.executable, "-m", "a2a.cli", "broker", "run", "--session", self.session,
                                        "--herdr-bin", str(self.out / "herdr_wrapper.sh")], env=self.env,
                                       stdout=subprocess.DEVNULL, stderr=(self.out / "broker.log").open("a"))
        deadline = time.monotonic() + 60
        while not any(e.get("state") == "BROKER_STARTED" for e in self.audit.read()):
            if time.monotonic() > deadline or self.broker.poll() is not None:
                raise Stop("setup", "broker 没有启动")
            time.sleep(0.2)

    def halt_dispatch(self):
        """与 broker 自己的全局停止相同:写 <状态目录>/dispatch.halted 并记审计 DISPATCH_HALTED。"""
        path = self.state / "dispatch.halted"
        if path.exists():
            return
        atomic_write_text(path, json.dumps({"reason": "真实 fnx 规模测试触发停止条件,保留现场",
                                            "at": datetime.now().astimezone().isoformat(), "pid": os.getpid()},
                                           ensure_ascii=False) + "\n")
        self.audit.record({"state": "DISPATCH_HALTED", "session": self.session, "detail": "压测脚本保留现场"})

    def stop_broker(self):
        if self.broker and self.broker.poll() is None:
            self.broker.send_signal(signal.SIGTERM)    # 优雅停机:在途投递收尾,不再开始新投递
            try:
                self.broker.wait(timeout=90)
            except subprocess.TimeoutExpired:
                self.broker.kill()
                self.broker.wait(timeout=30)

    def memory_snapshot(self, label: str):
        per = {a: round(stress.pss_mb(p), 1) for a, p in self.roots.items()}
        mem = stress.meminfo()
        self.fnx_pss.append({"label": label, "mem_available_mb": round(mem["MemAvailable"]),
                             "fnx_pss_sum_mb": round(sum(per.values())), "fnx_pss_max_mb": max(per.values() or [0]),
                             "server_pss_mb": round(stress.pss_mb(self.server_pid))})

    # ---- 运行 -----------------------------------------------------------
    def kickoff(self):
        self.step = "work"
        errors: Dict[str, str] = {}
        ok: List[str] = []

        def prompt(ip):
            try:
                self.client.agent_prompt(f"dv_{ip}", KICKOFF.format(ip=ip))
                ok.append(ip)
            except Exception as exc:
                errors[ip] = f"{type(exc).__name__}: {exc}"

        threads = {ip: threading.Thread(target=prompt, args=(ip,)) for ip in self.ips}
        self.kicked_off = True
        self.kickoff_wall = time.time()
        self.kickoff_mono = time.monotonic()
        for t in threads.values():
            t.start()
        for t in threads.values():
            t.join(timeout=60)
        alive = [ip for ip, t in threads.items() if t.is_alive()]
        self.report["kickoff"] = {"submitted": len(ok), "errors": errors, "still_running": alive}
        if errors or alive:
            # 有的提示词可能已经提交、模型可能在跑:按停止处理,保留现场
            raise Stop("kickoff", f"开头提示没有全部确认提交:出错 {errors},仍在进行 {alive}")

    def transcripts(self) -> Dict[str, List[dict]]:
        return {a: assistant_calls(read_transcript(session_dir_for("fnx_" + a.split("_")[0], cwd)))
                for a, cwd in self.cwds.items()}

    def check_framework(self, events: List[dict]):
        """框架正确性(方案 §6 一):已入队消息与本 IP 模板一致、无异常状态、不串 IP。"""
        for e in events:
            if e.get("state") in ABNORMAL:
                raise Stop("framework", f"出现异常状态 {e.get('state')}:{e}")
        for m in self.spool.pending() + self.spool.done():
            src_role, src_ip = m.src.split("_", 1)
            edge = TEMPLATES.get(m.edge_id)
            if (edge is None or edge["from"] != src_role or m.dst != f"{edge['to']}_{src_ip}"
                    or m.ip_id != src_ip or m.text != edge["template"].format(ip=src_ip)):
                raise Stop("framework", f"消息与本 IP 模板不符:{m.msg_id} {m.src}->{m.dst} {m.edge_id}")

    def monitor(self):
        """每 2 秒检查一次:框架正确性、调用上限、接口报错、链路超时;所有 IP 结束即返回。"""
        last_change: Dict[str, float] = {ip: time.monotonic() for ip in self.ips}
        last_sig: Dict[str, tuple] = {}
        settled: Dict[str, float] = {}
        while len(settled) < self.n:
            time.sleep(2)
            if self.abort_reason:
                raise Stop("resource", self.abort_reason)
            events = self.audit.read()
            self.check_framework(events)
            queued = [e for e in events if e.get("state") == "QUEUED"]
            per_ip = {ip: sum(1 for e in queued if (e.get("src") or "").endswith("_" + ip)) for ip in self.ips}
            over = {ip: c for ip, c in per_ip.items() if c > MAX_MSGS_PER_IP}
            if over:
                raise Stop("model", f"IP 入队超过 {MAX_MSGS_PER_IP} 条:{over}")
            calls = self.transcripts()
            total = sum(len(v) for v in calls.values())
            if total > self.request_cap:
                raise Stop("model", f"模型请求(含出错){total} 次,超过上限 {self.request_cap}")
            ok = sum(1 for v in calls.values() for c in v if c["stop"] in ("toolUse", "stop"))
            if self.success_cap and ok > self.success_cap:
                raise Stop("model", f"成功的模型调用 {ok} 次,超过上限 {self.success_cap}")
            now_wall = time.time()
            errors = [c for v in calls.values() for c in v if c["stop"] not in ("toolUse", "stop")
                      and c["end"] and now_wall - c["end"] < 60
                      and not (self.ignore_429 and is_rate_limited(c["error"]))]
            if len(errors) > API_ERRORS_PER_MIN:
                raise Stop("model", f"最近 1 分钟模型接口出错 {len(errors)} 次:{[e['stop'] for e in errors[:5]]}")
            pending = {m.dst for m in self.spool.pending()}
            for ip in self.ips:
                if ip in settled:
                    continue
                sig = (per_ip[ip], len(calls[f"dv_{ip}"]), len(calls[f"sw_{ip}"]),
                       sum(1 for e in events if (e.get("dst") or "").endswith("_" + ip)))
                if sig != last_sig.get(ip):
                    last_sig[ip] = sig
                    last_change[ip] = time.monotonic()
                idle = all(self.client.agent_get(f"{r}_{ip}").get("agent_status") in ("idle", "done")
                           for r in ("dv", "sw"))
                if idle and not ({f"dv_{ip}", f"sw_{ip}"} & pending) and time.monotonic() - last_change[ip] >= SETTLE_S:
                    settled[ip] = time.monotonic()
                elif time.monotonic() - self.kickoff_mono > CHAIN_TIMEOUT_S:
                    raise Stop("model", f"{ip} 的链路超过 {CHAIN_TIMEOUT_S} 秒没有结束")
            print(f"[{time.strftime('%H:%M:%S')}] 已结束 {len(settled)}/{self.n},入队 {len(queued)},"
                  f"模型请求 {total}(成功 {ok})", flush=True)

    # ---- 结果 -----------------------------------------------------------
    def final_checks(self) -> dict:
        events = self.audit.read()
        self.check_framework(events)
        delivered = [e["msg_id"] for e in events if e.get("state") == DELIVERED]
        queued = [e for e in events if e.get("state") == "QUEUED"]
        if len(delivered) != len(set(delivered)) or len(delivered) != len(queued) or self.spool.pending():
            raise Stop("framework", f"入队 {len(queued)} 条、送达 {len(delivered)} 条(去重 {len(set(delivered))})、"
                                    f"待投递 {len(self.spool.pending())} 条")
        calls = self.transcripts()
        unparsed = {k: v for k, v in PARSE_ERRORS.items() if v}
        if unparsed:
            # 会话记录不完整会低估模型调用、工具调用和报错次数,结论不可靠
            raise Stop("framework", f"会话记录有无法解析的行,统计不完整:{unparsed}")
        bad_tools = [(a, c["tools"]) for a, v in calls.items() for c in v if any(t != "a2a_send" for t in c["tools"])]
        if bad_tools:
            raise Stop("framework", f"出现 a2a_send 以外的工具调用:{bad_tools[:3]}")
        dirty = {a: sorted(p.name for p in cwd.iterdir()) for a, cwd in self.cwds.items() if any(cwd.iterdir())}
        if dirty:
            raise Stop("framework", f"工作目录不为空:{dirty}")
        return {"queued": len(queued), "delivered": len(delivered)}

    def completion(self) -> dict:
        """模型任务完成度(方案 §6 二)。"""
        events = self.audit.read()
        calls = self.transcripts()
        result = {}
        for ip in self.ips:
            counts: Dict[str, int] = {}
            for e in events:
                if e.get("state") == DELIVERED and (e.get("src") or "").endswith("_" + ip):
                    counts[e["edge_id"]] = counts.get(e["edge_id"], 0) + 1
            sent = set(counts)
            replies = sum(counts.get(k, 0) for k in ("sw_test_pass", "sw_test_fail"))
            dv_calls = calls[f"dv_{ip}"]
            last_text = dv_calls[-1]["text"] if dv_calls else ""
            done = counts.get("dv_done", 0) >= 1 and replies >= 1 and "收到" in last_text
            extra = {k: v for k, v in counts.items() if v > 1}
            if replies > 1:
                extra["sw 回复合计"] = replies
            if done:
                result[ip] = {"complete": True, "sw_choice": sorted(sent & {"sw_test_pass", "sw_test_fail"}),
                              "messages": counts}
            else:
                errors = [c["stop"] for a in (f"dv_{ip}", f"sw_{ip}") for c in calls[a] if c["stop"] not in ("toolUse", "stop")]
                reason = ("模型接口出错:" + ",".join(errors) if errors else
                          "dv 没有发 dv_done" if "dv_done" not in sent else
                          "sw 没有回复(没调用 a2a_send)" if not sent & {"sw_test_pass", "sw_test_fail"} else
                          "dv 最后没有回复收到")
                result[ip] = {"complete": False, "reason": reason, "messages": counts}
            if extra:
                result[ip]["model_anomaly"] = f"多发:{extra}"   # 模型行为异常,单列,不算框架失败(完成与否都记)
        return result

    def timeline(self) -> dict:
        events = self.audit.read()
        segs: Dict[str, List[float]] = {k: [] for k in ("kickoff_to_dv_done_queued", "dv_done_delivery",
                                                        "sw_reply_queued_after_dv_done_delivered", "sw_reply_delivery",
                                                        "dv_final_reply", "total")}
        waiting = []
        calls = self.transcripts()
        for ip in self.ips:
            def ts(state, edge_ids, src_role):
                for e in events:
                    if e.get("state") == state and e.get("edge_id") in edge_ids and e.get("src") == f"{src_role}_{ip}":
                        return iso_to_epoch(e["ts"])
                return None
            q1, d1 = ts("QUEUED", {"dv_done"}, "dv"), ts(DELIVERED, {"dv_done"}, "dv")
            q2, d2 = ts("QUEUED", {"sw_test_pass", "sw_test_fail"}, "sw"), ts(DELIVERED, {"sw_test_pass", "sw_test_fail"}, "sw")
            w = [e for e in events if e.get("state") == "WAITING_TARGET" and e.get("dst") == f"dv_{ip}"]
            if w:
                waiting.append(ip)
            dv_end = calls[f"dv_{ip}"][-1]["end"] if calls[f"dv_{ip}"] else None
            if None in (q1, d1, q2, d2, dv_end):
                continue
            segs["kickoff_to_dv_done_queued"].append(q1 - self.kickoff_wall)
            segs["dv_done_delivery"].append(d1 - q1)
            segs["sw_reply_queued_after_dv_done_delivered"].append(q2 - d1)
            segs["sw_reply_delivery"].append(d2 - q2)
            segs["dv_final_reply"].append(dv_end - d2)
            segs["total"].append(dv_end - self.kickoff_wall)
        out = {k: stress.summary(v) for k, v in segs.items()}
        out["ips_with_waiting_target"] = len(waiting)
        # 每条经历过 WAITING_TARGET 的消息:从第一次 WAITING_TARGET 到其 DISPATCHING 的时长
        waits = []
        for msg_id in {e["msg_id"] for e in events if e.get("state") == "WAITING_TARGET"}:
            mine = [e for e in events if e.get("msg_id") == msg_id]
            first = min(iso_to_epoch(e["ts"]) for e in mine if e.get("state") == "WAITING_TARGET")
            disp = [iso_to_epoch(e["ts"]) for e in mine if e.get("state") == "DISPATCHING" and iso_to_epoch(e["ts"]) >= first]
            if disp:
                waits.append(min(disp) - first)
        out["waiting_target"] = {"messages": len({e["msg_id"] for e in events if e.get("state") == "WAITING_TARGET"}),
                                 "events": sum(1 for e in events if e.get("state") == "WAITING_TARGET"),
                                 "wait_s": stress.summary(waits)}
        return out

    def model_stats(self) -> dict:
        calls = [c for v in self.transcripts().values() for c in v]
        lat = [c["latency_s"] for c in calls if c["latency_s"] is not None]
        intervals = sorted([(c["start"], 1) for c in calls if c["start"] and c["end"]] +
                           [(c["end"], -1) for c in calls if c["start"] and c["end"]])
        cur = peak = 0
        for _, d in intervals:
            cur += d
            peak = max(peak, cur)
        errs: Dict[str, int] = {}
        for c in calls:
            if c["stop"] == "error":
                errs[c["error"][:60]] = errs.get(c["error"][:60], 0) + 1
        rate_limited = sum(1 for c in calls if c["stop"] == "error" and is_rate_limited(c["error"]))
        ok = [c for c in calls if c["stop"] in ("toolUse", "stop")]
        ok_lat = [c["latency_s"] for c in ok if c["latency_s"] is not None]
        # 计数口径:会话记录里的 assistant 消息条数,不含 provider SDK 内部重试,不等于底层 HTTP 请求数
        return {"calls": len(calls), "successful": len(ok), "request_cap": self.request_cap,
                "success_cap": self.success_cap, "errors_by_message": errs, "rate_limited_429": rate_limited,
                "successful_latency_s": stress.summary(ok_lat),
                "providers": sorted({f"{c['provider']}/{c['model']}" for c in calls}),
                "stop_reasons": {s: sum(1 for c in calls if c["stop"] == s) for s in {c["stop"] for c in calls}},
                "latency_s": stress.summary(lat), "missing_timestamps": len(calls) - len(lat),
                "concurrency_peak": peak,
                "tokens_in": sum(c["input"] or 0 for c in calls), "tokens_out": sum(c["output"] or 0 for c in calls)}

    # ---- 停止与清理 -------------------------------------------------------
    def preserve(self, stop: Stop):
        """方案 §7:停 broker、快照证据、必要时停 agent、删插件文件;保留会话与 agent。

        每一步都尽力执行、失败只记录,本方法不抛异常,也绝不清理会话或 agent。"""
        failures: List[str] = []

        def attempt(what, fn):
            try:
                problem = fn()
                if problem:
                    failures.append(f"{what}:{problem}")
            except BaseException as exc:
                failures.append(f"{what}:{type(exc).__name__}: {exc}")

        snap = self.out / "snapshot"
        # 第一步:写全局停止标记(dispatch.halted)。broker 每处理下一条前都检查它,即使下面停不掉 broker,
        # 也不会再开始新的投递(手上那一条做完为止),从而不会再触发新的模型调用
        attempt("全局停止投递", self.halt_dispatch)
        attempt("停止 broker", self.stop_broker)

        def broker_gone():
            alive = []
            if self.broker is not None and self.broker.poll() is None:
                alive.append(self.broker.pid)
            alive += stress.pids_matching(f"a2a.cli broker run --session {self.session} ")
            return sorted(set(alive))
        try:
            alive = broker_gone()
        except BaseException as exc:
            alive = [f"无法确认:{type(exc).__name__}: {exc}"]
        self.report["broker_stopped"] = not alive
        if alive:
            failures.append(f"broker 没能停止 {alive};已写 dispatch.halted 阻止新的投递,需人工处理")
            self.report["DELIVERY_NOT_STOPPED"] = {"broker": alive, "halt_marker": str(self.state / "dispatch.halted")}
        attempt("建快照目录", lambda: snap.mkdir(exist_ok=True))
        for name in ("audit.jsonl", "registry.json"):
            attempt(f"快照 {name}", lambda name=name: (self.state / name).exists()
                    and shutil.copy(self.state / name, snap / name) and None)
        attempt("快照 spool", lambda: (self.state / "spool").exists()
                and shutil.copytree(self.state / "spool", snap / "spool", dirs_exist_ok=True) and None)
        attempt("快照 broker.log", lambda: (self.out / "broker.log").exists()
                and shutil.copy(self.out / "broker.log", snap / "broker.log") and None)

        def transcripts():
            self.report["transcripts"] = {a: [str(p) for p in session_dir_for("fnx_" + a.split("_")[0], c).glob("*.jsonl")]
                                          for a, c in self.cwds.items()}
        attempt("记录会话记录路径", transcripts)

        def protect_memory():
            mem = stress.meminfo()
            if stop.kind != "resource" or mem["MemAvailable"] / mem["MemTotal"] >= 0.10:
                return None
            stopped, failed = [], []
            for record in self.registry.list():
                try:
                    proc = subprocess.run([sys.executable, "-m", "a2a.cli", "agent", "stop", record.role, record.ip_id,
                                           "--session", self.session], env=self.env, capture_output=True, text=True,
                                          timeout=120)
                    if proc.returncode == 0:
                        stopped.append(record.agent_id)
                    else:
                        failed.append(f"{record.agent_id}(退出码 {proc.returncode})")
                except BaseException as exc:   # 一个失败不影响其余 agent
                    failed.append(f"{record.agent_id}({type(exc).__name__}: {exc})")
            self.report["agents_stopped_for_memory"] = {"stopped": stopped, "failed": failed}
            return f"这些 agent 没能停止:{failed}" if failed else None
        attempt("内存保护", protect_memory)
        attempt("删除插件文件", lambda: "; ".join(self.remove_plugin()) or None)
        self.report["preserve_failures"] = failures
        self.report["preserved"] = {"session": self.session, "tmux": self.session + "_tty", "out": str(self.out)}

    def cleanup(self) -> List[str]:
        failures: List[str] = []

        def attempt(what, fn):
            try:
                problem = fn()
                if problem:
                    failures.append(f"{what}:{problem}")
            except Exception as exc:
                failures.append(f"{what}:{type(exc).__name__}: {exc}")

        attempt("停止 broker", self.stop_broker)
        if self.owns_session:
            records = []
            attempt("读注册表", lambda: records.extend(self.registry.list()))
            def purge(r):
                proc = subprocess.run([sys.executable, "-m", "a2a.cli", "agent", "purge", r.role, r.ip_id,
                                       "--session", self.session], env=self.env, capture_output=True, text=True,
                                      timeout=120)
                return f"退出码 {proc.returncode}:{proc.stderr.strip()[-200:]}" if proc.returncode != 0 else None
            for r in records:
                attempt(f"purge {r.agent_id}", lambda r=r: purge(r))
            attempt("session_stop", lambda: session_stop(self.session) and None)
            attempt("session_delete", lambda: session_delete(self.session) and None)
            attempt(f"会话 {self.session}",
                    lambda: "仍在" if any(x.get("name") == self.session for x in session_list()) else None)
        if self.owns_tmux:
            def tmux_gone():
                subprocess.run(["tmux", "kill-session", "-t", self.session + "_tty"], capture_output=True, timeout=30)
                alive = subprocess.run(["tmux", "has-session", "-t", self.session + "_tty"],
                                       capture_output=True, timeout=30).returncode == 0
                return "仍在" if alive else None
            attempt(f"tmux 会话 {self.session}_tty", tmux_gone)
        attempt("插件文件", lambda: "; ".join(self.remove_plugin()) or None)
        for a, cwd in self.cwds.items():   # 没发提示词(或没收到输入)的 fnx 会话目录是空的,删掉;非空的保留作证据
            d = session_dir_for("fnx_" + a.split("_")[0], cwd)
            if d.is_dir() and not any(d.iterdir()):
                attempt(f"空会话目录 {d.name}", lambda d=d: d.rmdir())
        time.sleep(2)
        attempt("残留 fnx 进程", lambda: [a for a, pid in self.roots.items() if Path(f"/proc/{pid}").exists()] or None)
        return failures


def main() -> int:
    if os.environ.get("A2A_REAL_FNX") != "1":
        print("会调用模型:需要设置 A2A_REAL_FNX=1")
        return 2
    parser = argparse.ArgumentParser()
    parser.add_argument("--ips", type=int, required=True)
    parser.add_argument("--no-kickoff", action="store_true", help="不发开头提示、不调用模型,只验证准备与清理")
    parser.add_argument("--out")
    parser.add_argument("--ignore-429", action="store_true",
                        help="验证重试用:HTTP 429 不计入'最近 1 分钟出错次数'的停止条件(其他错误照常计入)")
    parser.add_argument("--success-cap", type=int, help="成功的模型调用上限(默认不单独限制)")
    parser.add_argument("--request-cap", type=int,
                        help="全部模型请求(含出错)上限(默认 5 × N × 2;软阈值,每 2 秒按会话记录统计,实际可能超出)")
    parser.add_argument("--simulate-stop", action="store_true",
                        help="测试保留现场的路径:spawn 并启动 broker 后模拟一次框架失败(不调用模型)")
    args = parser.parse_args()
    run = Run(args)
    status = 0
    preserve_for: Optional[Stop] = None    # 非 None 表示必须保留现场;一旦设置不会再走清理
    try:
        run.install_plugin()
        run.setup_session()
        run.spawn_all()
        run.start_broker()
        time.sleep(5)
        run.memory_snapshot("spawn 后空闲")
        if args.simulate_stop:
            raise Stop("framework", "模拟的框架失败(--simulate-stop)")
        if not args.no_kickoff:
            run.kickoff()
            run.monitor()
            run.memory_snapshot("全部结束后")
            run.report["framework"] = run.final_checks()
            run.report["completion"] = run.completion()
            run.report["timeline_s"] = run.timeline()
            run.report["model"] = run.model_stats()
            run.report["herdr"] = run.sampler.herdr_stats("work")
            run.report["resources_work"] = run.sampler.resource_stats("work")
            done = sum(1 for v in run.report["completion"].values() if v["complete"])
            gate = -(-run.n * 4 // 5)   # 方案 §6:至少 4/5 个 IP 走完(向上取整)
            run.report["completion_summary"] = f"{done}/{run.n} 个 IP 走完(门槛 {gate})"
            if done < gate:
                status = 3   # 框架正确,但模型完成度没达到门槛:不是成功
    except Stop as stop:
        run.report["stopped"] = {"kind": stop.kind, "step": run.step, "reason": str(stop)}
        print(f"停止({stop.kind},{run.step}):{stop}", flush=True)
        status = 1
        if stop.kind != "setup" or run.kicked_off:
            preserve_for = stop
    except BaseException as exc:   # 未预料的异常(含 Ctrl-C):发过提示词就保留现场
        run.report["stopped"] = {"kind": "unexpected", "step": run.step, "reason": f"{type(exc).__name__}: {exc}"}
        print(f"未预料的异常({run.step}):{type(exc).__name__}: {exc}", flush=True)
        status = 1
        if run.kicked_off:
            preserve_for = Stop("unexpected", str(exc))
    if run.kicked_off and "model" not in run.report:
        try:   # 停止路径也记录已发生的模型调用与错误分类,便于事后核对
            # 这是停止时的快照:agent 仍在运行,之后可能继续完成或重试模型调用,不一定是最终计数
            run.report["model"] = dict(run.model_stats(), snapshot_at=datetime.now().astimezone().isoformat(),
                                       note="停止时已记录的统计(会话记录条数),agent 之后可能继续调用,不是最终计数")
        except Exception as exc:
            run.report["model"] = {"unavailable": f"{type(exc).__name__}: {exc}"}
    run.report["memory"] = run.fnx_pss
    if run.sampler.rows:
        run.report["mem_available_mb_min"] = min(r["mem_available_mb"] for r in run.sampler.rows)
    if preserve_for is not None:
        run.preserve(preserve_for)      # 不抛异常,也不清理会话与 agent
    else:
        failures = run.cleanup()
        run.report["cleanup_failures"] = failures
        if failures:
            status = 1
    try:
        run.sampler.close()
    finally:
        run.report["exit_status"] = status
        (run.out / "report.json").write_text(json.dumps(run.report, ensure_ascii=False, indent=2))
        print(json.dumps(run.report, ensure_ascii=False, indent=1))
    return status


if __name__ == "__main__":
    sys.exit(main())
