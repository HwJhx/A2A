"""阶段 8 §3.1–3.5:规模压测(真实 herdr、假 agent、不调用模型)。方案见 herdr/claude/13-stage8-scale-plan.md。

一档规模 = 5 个角色 × N 个 IP。步骤:
  spawn    逐个 `a2a agent spawn`,记录耗时与资源
  idle     broker 运行、没有消息,观察 --idle-s 秒
  fanout   每个 IP 的 dv 等在同一屏障前,放开后各发一条 dv_done;跑 --rounds 轮
  burst    ip00 的 dv 串行连发 --burst 条到 sw_ip00;跑 --rounds 轮
  backlog  broker 停止时每个 IP 的 dv 各发 k 条(--backlog 可给多个 k),再启动 broker
延迟用单调时钟(CLOCK_MONOTONIC 全系统共享):
  入队到送达   假 agent 在 `a2a send` 成功返回时记 t;脚本每 50 毫秒轮询 spool,首次看到 DELIVERED 记终点
  屏障到送达   起点是脚本创建屏障文件的时刻
broker 通过透明包装脚本调用 herdr(只记一行日志后 exec 真正的 herdr),采样线程每 20 毫秒检查这些 pid,
得到 herdr 调用次数(精确)、并发峰值与耗时(约 20 毫秒精度);另一个线程每 0.5 秒采样内存与 CPU。

只有设置 A2A_STRESS=1 才会运行:
  A2A_STRESS=1 python3 tests/stress/stress_scale.py --ips 10 [--rounds 3] [--steps spawn,idle,fanout,burst,backlog]
结果写在 --out(默认 ~/a2a_stage8_run/n<N>/),状态目录保留作证据;会话在结束或出错时清理。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

HERE = Path(__file__).resolve().parent
SRC = HERE.parent.parent / "src"
sys.path.insert(0, str(SRC))

from a2a.audit import AuditLog  # noqa: E402
from a2a.herdr_client import HerdrClient, session_delete, session_list, session_stop  # noqa: E402
from a2a.messages import DELIVERED, QUEUED  # noqa: E402
from a2a.registry import Registry  # noqa: E402
from a2a.spool import MessageNotFoundError, Spool  # noqa: E402
from a2a.topology import TopologyStore  # noqa: E402

FAKE = HERE.parent / "fake_agent.py"
ROLES = ("spec", "arch", "rtl", "dv", "sw")
TEMPLATE = "{ip} ip 压测消息,只记录不处理"
MEM_STOP_RATIO = 0.20


class Abort(Exception):
    pass


# ---- /proc 读取 -------------------------------------------------------------
def meminfo() -> Dict[str, float]:
    values = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemTotal", "MemAvailable"):
            values[key] = int(rest.split()[0]) / 1024
    return values


def pss_mb(pid: int) -> float:
    try:
        for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
            if line.startswith("Pss:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    return 0.0


def cpu_s(pid: int) -> float:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
    except (OSError, IndexError):
        return 0.0


def pids_matching(pattern: str) -> List[int]:
    out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout
    return [int(p) for p in out.split()]


def herdr_servers() -> set:
    return set(pids_matching("herdr server"))


def pct(values: List[float], q: float) -> Optional[float]:
    """最近秩法分位数。"""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-int(q * 100) * len(ordered) // 100))  # ceil(q * n)
    return ordered[min(rank, len(ordered)) - 1]


def summary(values: List[float]) -> Dict[str, object]:
    return {"n": len(values), "p50": _r(pct(values, 0.50)), "p95": _r(pct(values, 0.95)),
            "max": _r(max(values) if values else None)}


def _r(v):
    return None if v is None else round(v, 3)


# ---- 采样 -------------------------------------------------------------------
class Sampler:
    def __init__(self, run: "Run"):
        self.run = run
        self.stop = threading.Event()
        self.rows: List[dict] = []
        self.calls: Dict[int, dict] = {}       # herdr 调用:pid -> {start, end, cmd}
        self.call_log_pos = 0
        self.mem_total = meminfo()["MemTotal"]
        self.min_avail_ratio = 1.0
        self.threads = [threading.Thread(target=self._resources, daemon=True),
                        threading.Thread(target=self._herdr_calls, daemon=True)]

    def start(self):
        for t in self.threads:
            t.start()

    def close(self):
        self.stop.set()
        for t in self.threads:
            if t.ident is not None:  # 会话建立前就中止时,采样线程还没启动
                t.join(timeout=5)

    def _resources(self):
        while not self.stop.wait(0.5):
            mem = meminfo()
            ratio = mem["MemAvailable"] / self.mem_total
            self.min_avail_ratio = min(self.min_avail_ratio, ratio)
            if ratio < MEM_STOP_RATIO:
                self.run.abort_reason = f"MemAvailable/MemTotal = {ratio:.2f} < {MEM_STOP_RATIO}"
            fakes = pids_matching(f"{FAKE} {self.run.out}")
            broker = self.run.broker.pid if self.run.broker and self.run.broker.poll() is None else 0
            self.rows.append({
                "t": round(time.monotonic(), 3), "step": self.run.step, "mem_available_mb": round(mem["MemAvailable"]),
                "server_pss_mb": round(pss_mb(self.run.server_pid), 1), "server_cpu_s": round(cpu_s(self.run.server_pid), 2),
                "broker_pss_mb": round(pss_mb(broker), 1) if broker else 0, "broker_cpu_s": round(cpu_s(broker), 2) if broker else 0,
                "broker_threads": _threads(broker), "fake_count": len(fakes),
                "fake_pss_mb": round(sum(pss_mb(p) for p in fakes), 1),
                "sampler_cpu_s": round(time.process_time(), 2),
            })

    def _herdr_calls(self):
        log = self.run.out / "herdr_calls.log"
        while not self.stop.wait(0.02):
            now = time.monotonic()
            if log.exists():
                with log.open() as f:
                    f.seek(self.call_log_pos)
                    chunk = f.read()
                    self.call_log_pos = f.tell()
                for line in chunk.splitlines():
                    pid, _, cmd = line.partition(" ")
                    if pid.isdigit():
                        self.calls[int(pid)] = {"start": now, "end": None, "cmd": cmd, "step": self.run.step}
            for pid, call in self.calls.items():
                if call["end"] is None and not Path(f"/proc/{pid}").exists():
                    call["end"] = now

    def herdr_stats(self, step: str) -> dict:
        calls = [c for c in self.calls.values() if c["step"] == step]
        ended = [c for c in calls if c["end"] is not None]
        events = sorted([(c["start"], 1) for c in ended] + [(c["end"], -1) for c in ended])
        peak = cur = 0
        for _, d in events:
            cur += d
            peak = max(peak, cur)
        by_cmd: Dict[str, List[float]] = {}
        for c in ended:
            key = " ".join(c["cmd"].split()[2:4])  # 去掉 --session <名>
            by_cmd.setdefault(key, []).append(c["end"] - c["start"])
        # 起止都由 20 毫秒采样得到:调用次数精确,耗时与并发是估计值;短于采样间隔的调用耗时会记成 0
        return {"calls": len(calls), "concurrency_peak_sampled": peak, "sample_resolution_s": 0.02,
                "by_command_sampled_s": {k: summary(v) for k, v in sorted(by_cmd.items())}}

    def resource_stats(self, step: str) -> dict:
        rows = [r for r in self.rows if r["step"] == step]
        if not rows:
            return {}
        def peak(k):
            return max(r[k] for r in rows)
        def delta(k):
            return round(rows[-1][k] - rows[0][k], 2)
        return {"samples": len(rows), "mem_available_mb_min": min(r["mem_available_mb"] for r in rows),
                "server_pss_mb_max": peak("server_pss_mb"), "broker_pss_mb_max": peak("broker_pss_mb"),
                "broker_threads_max": peak("broker_threads"), "fake_pss_mb_max": peak("fake_pss_mb"),
                "server_cpu_s": delta("server_cpu_s"), "broker_cpu_s": delta("broker_cpu_s"),
                "sampler_cpu_s": delta("sampler_cpu_s"), "wall_s": round(rows[-1]["t"] - rows[0]["t"], 2)}


def _threads(pid: int) -> int:
    if not pid:
        return 0
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("Threads:"):
                return int(line.split()[1])
    except OSError:
        pass
    return 0


# ---- 一档压测 ---------------------------------------------------------------
class Run:
    def __init__(self, args):
        self.n = args.ips
        self.args = args
        self.session = f"a2a_s8_n{self.n}_{os.getpid()}"  # 唯一会话名;清理只碰本次创建、确认归属的资源
        self.owns_tmux = False
        self.owns_session = False
        self.out = Path(args.out or Path.home() / "a2a_stage8_run" / f"n{self.n}").resolve()
        if self.out.exists():
            raise SystemExit(f"{self.out} 已存在,先人工确认")
        self.state = self.out / "state"
        self.state.mkdir(parents=True)
        self.ips = [f"ip{i:02d}" for i in range(self.n)]
        self.env = dict(os.environ, A2A_STATE_DIR=str(self.state), PYTHONPATH=str(SRC), PYTHONDONTWRITEBYTECODE="1")
        self.step = "setup"
        self.abort_reason: Optional[str] = None
        self.broker: Optional[subprocess.Popen] = None
        self.server_pid = 0
        self.report: Dict[str, object] = {"ips": self.n, "agents": 5 * self.n, "rounds": args.rounds}
        self.expected_queued: List[tuple] = []
        self._write_files()
        self.store = TopologyStore.create(self.state / "topology.yaml", {
            "version": 1, "project_id": "soc_a", "workspace_label": f"stress-n{self.n}",
            "roles": {r: {"label": f"{r}智能体", "launcher": str(self.out / "launch_fake.sh")} for r in ROLES},
            "ips": self.ips, "edges": [{"id": "dv_done", "from": "dv", "to": "sw", "template": TEMPLATE}]})
        self.registry = Registry(self.state / "registry.json")
        self.spool = Spool(self.state / "spool")
        self.audit = AuditLog(self.state / "audit.jsonl")
        self.client = HerdrClient(self.session)
        self.sampler = Sampler(self)

    def _write_files(self):
        launcher = self.out / "launch_fake.sh"
        launcher.write_text(
            "#!/bin/bash\n"
            f'export FAKE_BARRIER="{self.out}/barrier.go"\n'
            f'exec {sys.executable} {FAKE} {self.out}/logs/"${{A2A_ROLE}}_${{A2A_IP}}.log" '
            '"$( [ "$A2A_ROLE" = dv ] && echo burst:dv_done || echo work )"\n')
        launcher.chmod(0o755)
        (self.out / "logs").mkdir()
        wrapper = self.out / "herdr_wrapper.sh"   # 透明:记一行后 exec 真正的 herdr(pid、参数、输出、退出码、信号都不变)
        wrapper.write_text("#!/bin/bash\n"
                           f'printf "%s %s\\n" "$$" "$*" >> {self.out}/herdr_calls.log\n'
                           f'exec {shutil.which("herdr")} "$@"\n')
        wrapper.chmod(0o755)

    # ---- 工具 ---------------------------------------------------------------
    def check(self):
        if self.abort_reason:
            raise Abort(self.abort_reason)

    def a2a(self, *argv, timeout=180):
        """运行 a2a 命令行;每 0.2 秒检查一次停止条件,触发时结束整个进程组后中止(不必等命令自己结束)。

        命令在独立的进程组里运行,它调用 herdr 起的子进程也在组里,一起结束。被中断的 spawn 可能已经
        建了 pane 但还没登记;这类 pane 随清理时删除本次会话一起关闭。"""
        proc = subprocess.Popen([sys.executable, "-m", "a2a.cli", *argv], env=self.env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True)

        def kill_group():
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    stdout, stderr = proc.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    if self.abort_reason or time.monotonic() > deadline:
                        kill_group()
                        self.check()
                        raise Abort(f"a2a {' '.join(argv)} 超过 {timeout} 秒没有结束")
        finally:
            if proc.poll() is None:
                kill_group()
        if proc.returncode != 0:
            raise Abort(f"a2a {' '.join(argv)} 失败:{stderr.strip()[-300:]}")
        return stdout

    def events(self, agent_id: str) -> List[dict]:
        path = self.out / "logs" / f"{agent_id}.log.events"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def sends(self, agent_id: str, n: int) -> List[dict]:
        return [e for e in self.events(agent_id) if e.get("n") == n and "rc" in e]

    def wait(self, predicate, what: str, timeout: float):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.check()
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        raise Abort(f"{timeout} 秒内没有等到:{what}")

    def sleep_checked(self, seconds: float):
        """分段等待,每 0.5 秒检查一次停止条件。"""
        deadline = time.monotonic() + seconds
        while True:
            self.check()
            left = deadline - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(0.5, left))

    def start_broker(self):
        before = sum(1 for e in self.audit.read() if e.get("state") == "BROKER_STARTED")
        self.broker = subprocess.Popen([sys.executable, "-m", "a2a.cli", "broker", "run", "--session", self.session,
                                        "--herdr-bin", str(self.out / "herdr_wrapper.sh")], env=self.env,
                                       stdout=subprocess.DEVNULL, stderr=(self.out / "broker.log").open("a"))
        started = time.monotonic()
        self.wait(lambda: sum(1 for e in self.audit.read() if e.get("state") == "BROKER_STARTED") > before,
                  "broker 启动", 60)
        return started

    def stop_broker(self):
        if self.broker and self.broker.poll() is None:
            self.broker.send_signal(signal.SIGTERM)
            try:
                self.broker.wait(timeout=90)
            except subprocess.TimeoutExpired:
                self.broker.kill()
                self.broker.wait(timeout=30)
        self.broker = None

    def trigger(self, ip: str, count: int) -> int:
        """给 dv_<ip> 一次输入,返回这是它的第几次输入(假 agent 按自己收到的行数计数)。"""
        self.check()
        self.inputs[ip] = self.inputs.get(ip, 0) + 1
        self.client.agent_prompt(f"dv_{ip}", str(count))
        return self.inputs[ip]

    def track(self, sends: Dict[str, float], t0: Optional[float], timeout: float) -> Dict[str, List[float]]:
        """sends: msg_id -> 入队时刻。轮询 spool 到全部送达,返回两种延迟。"""
        done: Dict[str, float] = {}
        deadline = time.monotonic() + timeout
        while len(done) < len(sends):
            self.check()
            if time.monotonic() > deadline:
                raise Abort(f"{timeout} 秒内只送达 {len(done)}/{len(sends)}")
            now = time.monotonic()
            for msg_id in sends:
                if msg_id not in done:
                    try:
                        if self.spool.get(msg_id).state == DELIVERED:
                            done[msg_id] = now
                    except MessageNotFoundError:
                        pass
            time.sleep(0.05)
        result = {"enqueue_to_delivered_s": [done[m] - sends[m] for m in sends]}
        if t0 is not None:
            result["barrier_to_delivered_s"] = [done[m] - t0 for m in sends]
        return result

    def collect_sends(self, ns: Dict[str, int], per_ip: int, timeout: float = 300) -> Dict[str, float]:
        """ns: ip -> 该 dv 的第几次输入。等每个 dv 在这次输入里发完 per_ip 条。"""
        def ready():
            got = {ip: self.sends(f"dv_{ip}", n) for ip, n in ns.items()}
            return got if all(len(v) >= per_ip for v in got.values()) else None
        got = self.wait(ready, f"{len(ns)} 个 dv 各发完 {per_ip} 条", timeout)
        sends = {}
        for ip, events in got.items():
            for e in events:
                if e["rc"] != 0 or not e.get("msg_id"):
                    raise Abort(f"dv_{ip} 发送失败:{e}")
                sends[e["msg_id"]] = e["t"]
                self.expected_queued.append(("dv_done", f"dv_{ip}", f"sw_{ip}"))
        return sends

    def verify_traffic(self, delivered_per_ip: Dict[str, int]):
        """队列侧与接收侧核对:入队记录恰好如预期、无待处理、每条只送达一次、每个 sw 只收到本 IP 的文字。"""
        self.wait(lambda: self.spool.pending() == [], "队列清空", 120)
        events = self.audit.read()
        queued = sorted((e["edge_id"], e["src"], e["dst"]) for e in events if e.get("state") == QUEUED)
        if queued != sorted(self.expected_queued):
            raise Abort(f"入队记录与预期不符:多 {len(queued) - len(self.expected_queued)} 条")
        delivered = [e["msg_id"] for e in events if e.get("state") == DELIVERED]
        if len(delivered) != len(set(delivered)) or len(delivered) != len(self.expected_queued):
            raise Abort(f"送达记录异常:{len(delivered)} 条,去重后 {len(set(delivered))},预期 {len(self.expected_queued)}")
        time.sleep(1.0)
        for ip in self.ips:
            raw = (self.out / "logs" / f"sw_{ip}.log").read_bytes().decode("utf-8", "replace")
            own = raw.count(TEMPLATE.format(ip=ip))
            others = sum(raw.count(TEMPLATE.format(ip=o)) for o in self.ips if o != ip)
            if own != delivered_per_ip.get(ip, 0) or others:
                raise Abort(f"sw_{ip} 收到本 IP {own} 条(预期 {delivered_per_ip.get(ip, 0)})、其他 IP {others} 条")

    # ---- 步骤 ---------------------------------------------------------------
    def setup_session(self):
        if any(row.get("name") == self.session for row in session_list()):
            raise Abort(f"herdr 会话 {self.session} 已存在,不使用、也不清理它")
        if subprocess.run(["tmux", "has-session", "-t", self.session + "_tty"], capture_output=True).returncode == 0:
            raise Abort(f"tmux 会话 {self.session}_tty 已存在,不使用、也不清理它")
        before = herdr_servers()
        subprocess.run(["tmux", "new-session", "-d", "-x", "200", "-y", "50", "-s", self.session + "_tty",
                        f"cd {Path.home()} && herdr session attach {self.session}"], check=True)
        self.owns_tmux = True
        self.wait(self.client.is_server_running, "会话启动", 30)
        if not any(row.get("name") == self.session for row in session_list()):
            raise Abort(f"会话 {self.session} 没有出现在 herdr session list 里")
        self.owns_session = True
        new = herdr_servers() - before
        if len(new) != 1:
            # 找不准本会话的服务进程就无法测它的资源;不输出可能误导的 0
            raise Abort(f"会话启动后新增的 herdr server 进程有 {len(new)} 个(预期 1 个),无法确定被测进程")
        self.server_pid = new.pop()
        self.sampler.start()

    def step_spawn(self):
        self.step = "spawn"
        times = []
        for role in ROLES:
            for ip in self.ips:
                started = time.monotonic()
                self.check()
                self.a2a("agent", "spawn", role, ip, "--cwd", str(self.out), "--session", self.session)
                times.append(time.monotonic() - started)
                self.check()
        started = time.monotonic()
        self.a2a("agent", "list", "--session", self.session)
        self.report["spawn"] = {"per_agent_s": summary(times), "total_s": _r(sum(times)),
                                "agent_list_s": _r(time.monotonic() - started),
                                "resources": self.sampler.resource_stats("spawn")}

    def step_idle(self):
        self.start_broker()
        self.sleep_checked(5)
        self.step = "idle"
        self.sleep_checked(self.args.idle_s)
        self.report["idle"] = {"seconds": self.args.idle_s, "herdr": self.sampler.herdr_stats("idle"),
                               "resources": self.sampler.resource_stats("idle")}

    def step_fanout(self):
        self.step = "fanout"
        if not self.broker:
            self.start_broker()
        barrier = self.out / "barrier.go"
        rounds = []
        lat: Dict[str, List[float]] = {"enqueue_to_delivered_s": [], "barrier_to_delivered_s": []}
        for r in range(self.args.rounds):
            barrier.unlink(missing_ok=True)
            ns = {ip: self.trigger(ip, 1) for ip in self.ips}
            self.wait(lambda: all(any(e.get("n") == ns[ip] and "barrier_wait" in e for e in self.events(f"dv_{ip}"))
                                  for ip in self.ips), "所有 dv 到达屏障", 120)
            t0 = time.monotonic()
            barrier.touch()
            sends = self.collect_sends(ns, 1)
            got = self.track(sends, t0, 300)
            for k in lat:
                lat[k] += got[k]
            rounds.append({k: summary(v) for k, v in got.items()})
            self.wait_dv_idle(self.ips)
        self.delivered_per_ip = {ip: self.delivered_per_ip.get(ip, 0) + self.args.rounds for ip in self.ips}
        self.verify_traffic(self.delivered_per_ip)
        self.report["fanout"] = {"rounds": rounds, "all": {k: summary(v) for k, v in lat.items()},
                                 "herdr": self.sampler.herdr_stats("fanout"),
                                 "resources": self.sampler.resource_stats("fanout")}

    def step_burst(self):
        self.step = "burst"
        if not self.broker:
            self.start_broker()
        (self.out / "barrier.go").touch()
        ip = self.ips[0]
        rounds = []
        for r in range(self.args.rounds):
            started = time.monotonic()
            n = self.trigger(ip, self.args.burst)
            sends = self.collect_sends({ip: n}, self.args.burst)
            got = self.track(sends, None, 300)
            total = time.monotonic() - started
            order = [e["msg_id"] for e in self.audit.read() if e.get("state") == DELIVERED and e["msg_id"] in sends]
            seqs = [self.spool.get(m).queue_seq for m in order]
            rounds.append({"total_s": _r(total), "enqueue_to_delivered_s": summary(got["enqueue_to_delivered_s"]),
                           "in_queue_seq_order": seqs == sorted(seqs)})
            self.wait_dv_idle([ip])
            self.delivered_per_ip[ip] = self.delivered_per_ip.get(ip, 0) + self.args.burst
        self.verify_traffic(self.delivered_per_ip)
        self.report["burst"] = {"count": self.args.burst, "rounds": rounds,
                                "herdr": self.sampler.herdr_stats("burst"),
                                "resources": self.sampler.resource_stats("burst")}

    def step_backlog(self):
        (self.out / "barrier.go").touch()
        results = []
        for k in self.args.backlog:
            self.step = f"backlog{k}"
            self.stop_broker()
            ns = {ip: self.trigger(ip, k) for ip in self.ips}
            sends = self.collect_sends(ns, k, timeout=600)
            sizes_before = self.sizes()
            started = self.start_broker()
            recovered = time.monotonic() - started
            got = self.track(sends, started, 900)
            self.delivered_per_ip = {ip: self.delivered_per_ip.get(ip, 0) + k for ip in self.ips}
            self.verify_traffic(self.delivered_per_ip)
            order_ok = True
            for ip in self.ips:
                seqs = [self.spool.get(e["msg_id"]).queue_seq for e in self.audit.read()
                        if e.get("state") == DELIVERED and e.get("dst") == f"sw_{ip}"]
                order_ok &= seqs == sorted(seqs)
            results.append({"per_ip": k, "messages": len(sends), "broker_start_s": _r(recovered),
                            "start_to_all_delivered_s": _r(max(got["barrier_to_delivered_s"])),
                            "start_to_delivered_s": summary(got["barrier_to_delivered_s"]),
                            "in_queue_seq_order": order_ok, "sizes_before": sizes_before, "sizes_after": self.sizes(),
                            "herdr": self.sampler.herdr_stats(self.step),
                            "resources": self.sampler.resource_stats(self.step)})
        self.report["backlog"] = results

    def sizes(self) -> dict:
        def tree(p: Path):
            files = [f for f in p.rglob("*") if f.is_file()] if p.exists() else []
            return {"files": len(files), "kb": round(sum(f.stat().st_size for f in files) / 1024, 1)}
        return {"spool": tree(self.state / "spool"), "done": tree(self.state / "spool" / "done"),
                "audit_kb": round((self.state / "audit.jsonl").stat().st_size / 1024, 1)}

    delivered_per_ip: Dict[str, int] = {}
    inputs: Dict[str, int] = {}

    def wait_dv_idle(self, ips: List[str], timeout: float = 60):
        for ip in ips:
            self.wait(lambda: self.client.agent_get(f"dv_{ip}").get("agent_status") in ("idle", "done"),
                      f"dv_{ip} 回到 idle", timeout)

    # ---- 收尾 ---------------------------------------------------------------
    def cleanup(self) -> List[str]:
        """只清理本次创建、确认归属的资源;返回清理失败项(非空即本次运行失败)。"""
        self.step = "cleanup"
        failures: List[str] = []

        def attempt(what: str, fn) -> None:
            """执行一步清理;任何异常(含超时)都记为失败并继续后面的步骤。"""
            try:
                problem = fn()
                if problem:
                    failures.append(f"{what}:{problem}")
            except Exception as exc:
                failures.append(f"{what}:{type(exc).__name__}: {exc}")

        def purge(record):
            proc = subprocess.run([sys.executable, "-m", "a2a.cli", "agent", "purge", record.role, record.ip_id,
                                   "--session", self.session], env=self.env, capture_output=True, text=True,
                                  timeout=120)
            return proc.stderr.strip()[-200:] if proc.returncode != 0 else None

        def tmux_gone():
            subprocess.run(["tmux", "kill-session", "-t", self.session + "_tty"], capture_output=True, timeout=30)
            alive = subprocess.run(["tmux", "has-session", "-t", self.session + "_tty"],
                                   capture_output=True, timeout=30).returncode == 0
            return "仍在" if alive else None

        attempt("停止 broker", self.stop_broker)
        if self.owns_session:
            records = []
            attempt("读注册表", lambda: records.extend(self.registry.list()))
            for record in records:
                attempt(f"purge {record.agent_id}", lambda r=record: purge(r))
            attempt("session_stop", lambda: session_stop(self.session) and None)
            attempt("session_delete", lambda: session_delete(self.session) and None)
        if self.owns_tmux:
            attempt(f"tmux 会话 {self.session}_tty", tmux_gone)
        attempt("停止采样", self.sampler.close)
        time.sleep(2)
        leftover: List[int] = []
        attempt("检查残留进程", lambda: leftover.extend(pids_matching(f"{FAKE} {self.out}")))
        if leftover:
            failures.append(f"残留假 agent 进程:{leftover}")
        if self.owns_session:
            attempt(f"会话 {self.session}",
                    lambda: "仍在" if any(row.get("name") == self.session for row in session_list()) else None)
        self.report["leftover_fake_pids"] = leftover
        self.report["cleanup_failures"] = failures
        self.report["min_mem_available_ratio"] = round(self.sampler.min_avail_ratio, 3)
        return failures


def main() -> int:
    if os.environ.get("A2A_STRESS") != "1":
        print("需要设置 A2A_STRESS=1")
        return 2
    parser = argparse.ArgumentParser()
    parser.add_argument("--ips", type=int, required=True)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--steps", default="spawn,idle,fanout,burst,backlog")
    parser.add_argument("--idle-s", type=float, default=60)
    parser.add_argument("--burst", type=int, default=20)
    parser.add_argument("--backlog", default="3,20", help="每个 IP 的积压条数,逗号分隔")
    parser.add_argument("--out")
    parser.add_argument("--abort-after", type=float, help="测试清理路径:运行 N 秒后人为触发停止条件")
    args = parser.parse_args()
    args.backlog = [int(k) for k in args.backlog.split(",") if k]
    run = Run(args)
    run.delivered_per_ip = {}
    run.inputs = {}
    status = 0
    if args.abort_after:
        threading.Timer(args.abort_after, lambda: setattr(run, "abort_reason", "人为触发(--abort-after)")).start()
    try:
        run.setup_session()
        for step in args.steps.split(","):
            getattr(run, f"step_{step}")()
            print(f"[{time.strftime('%H:%M:%S')}] {step} 完成", flush=True)
    except Abort as exc:
        run.report["aborted"] = {"step": run.step, "reason": str(exc)}
        print(f"中止于 {run.step}:{exc}", flush=True)
        status = 1
    finally:
        failures = run.cleanup()
        if failures:
            print("清理失败:", failures, flush=True)
            status = 1
        (run.out / "report.json").write_text(json.dumps(run.report, ensure_ascii=False, indent=2))
        with (run.out / "samples.csv").open("w") as f:
            if run.sampler.rows:
                f.write(",".join(run.sampler.rows[0]) + "\n")
                for row in run.sampler.rows:
                    f.write(",".join(str(v) for v in row.values()) + "\n")
        print(json.dumps(run.report, ensure_ascii=False, indent=1))
    return status


if __name__ == "__main__":
    sys.exit(main())
