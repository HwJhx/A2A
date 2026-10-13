"""真实 fnx 探测(**会调用模型**,约 3 次):路线图 338 的真实验证 + 探测 8。

  338  fnx_dv(--no-builtin-tools)运行中,`a2a topology add-edge` 加一条 dv 的出边;再给它一条只要求回复 OK
       的提示(触发一轮处理,1 次模型调用)。检查扩展 a2a-turn-inspect 在这一轮开始时记录的 a2a_send 可选边
       应包含新边(插件在 before_agent_start 刷新)。
  探测 8 fnx_dv 不加 --no-builtin-tools(有 bash),提示它用 bash 运行 `echo probe8`(约 2 次模型调用),
       每秒记录 herdr 状态与屏幕,看是否出现人工批准(blocked / 屏幕上的批准提示)。**不自动批准**:
       一旦 blocked,记录现场后 stop 这个 agent。

探测 8 发提示之前,先确认执行前限制 a2a-probe-guard 已在本次的 fnx_dv 里生效(它注册钩子后写出含进程号的标记),
否则不发提示、判失败。
判定(退出码 0 = 全部成立,1 = 有失败):338 这一轮按时结束且新边可见;探测 8 按时结束,并且从本轮的 fnx 会话记录
里核实到 bash 调用(命令含 echo probe8)及其输出 probe8,或者出现 blocked(那也是有效结论);清理无问题、工作目录为空。
只清理本次确认创建的会话、tmux、agent、插件链接与检查扩展。

不发 a2a 消息、不启动 broker。必须设置 A2A_REAL_FNX=1:
  A2A_REAL_FNX=1 python3 tests/stress/real_fnx_probes.py               # 调用模型
  A2A_REAL_FNX=1 python3 tests/stress/real_fnx_probes.py --no-kickoff  # 不调用模型,只验证准备与清理
结果写在 --out(默认 ~/a2a_probes/<时间>/report.json)。agent 的工作目录是空的临时目录,结束后检查仍为空。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
SRC = HERE.parent.parent / "src"
sys.path.insert(0, str(SRC))
from a2a import plugin  # noqa: E402
from a2a.herdr_client import HerdrClient, session_delete, session_list, session_stop  # noqa: E402
from a2a.topology import TopologyStore  # noqa: E402

HOME = Path.home()
TURN_INSPECT = HERE.parent / "pi-ext" / "a2a-turn-inspect.ts"
# 执行前硬限制:只放行 bash 执行 echo probe8,其他工具调用在执行前被拦下(探测 8 开放了全部内置工具)
PROBE_GUARD = HERE.parent / "pi-ext" / "a2a-probe-guard.ts"
EDGES = [{"id": "dv_done", "from": "dv", "to": "sw", "template": "{ip} 验证完成(探测用,不会真的发送)"}]
NEW_EDGE = ("dv_extra", "dv", "sw", "{ip} 补充说明(338 探测新增的边)")
PROMPT_338 = "这是一次连通性测试。不要调用任何工具,只回复两个字母 OK。"
PROMPT_P8 = "请用 bash 工具运行命令 echo probe8,然后把命令的输出原样告诉我。不要做别的事。"
APPROVAL_WORDS = ("approve", "Approve", "allow", "Allow", "批准", "允许", "确认执行", "y/n", "Y/n")


class Probe:
    def __init__(self, args):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.out = Path(args.out or HOME / "a2a_probes" / stamp).resolve()
        if self.out.exists():
            raise SystemExit(f"{self.out} 已存在")
        self.state = self.out / "state"
        self.work = self.out / "work"
        self.state.mkdir(parents=True)
        self.work.mkdir()
        (self.state / "inspect.enabled").touch()
        (self.state / "probe-guard.enabled").touch()
        self.session = f"a2a_probe_{os.getpid()}"
        self.kickoff = not args.no_kickoff
        self.env = dict(os.environ, A2A_STATE_DIR=str(self.state), PYTHONPATH=str(SRC), PYTHONDONTWRITEBYTECODE="1")
        self.store = TopologyStore.create(self.state / "topology.yaml", {
            "version": 1, "project_id": "probe", "workspace_label": "a2a-probe",
            "roles": {r: {"label": f"{r}智能体", "launcher": str(HOME / f".forenyx/fnx_{r}/bin/fnx_{r}"),
                          "launch_args": ["--no-builtin-tools"]} for r in ("dv", "sw")},
            "ips": ["uart"], "edges": EDGES})
        self.client = HerdrClient(self.session)
        self.report: Dict[str, Any] = {"session": self.session, "kickoff": self.kickoff, "out": str(self.out)}
        self.installed: List[str] = []
        self.ext_copies: List[Path] = []
        self.owns_tmux = self.owns_session = self.spawned = False   # 只清理本次确认创建的资源
        self.failures: List[str] = []

    # ---- 准备与清理 --------------------------------------------------
    def a2a(self, *argv, check=True):
        session = ["--session", self.session] if argv[0] == "agent" else []    # 只有 agent 命令需要会话
        proc = subprocess.run([sys.executable, "-m", "a2a.cli", *argv, *session], env=self.env,
                              capture_output=True, text=True, timeout=180)
        if check and proc.returncode != 0:
            raise RuntimeError(f"a2a {' '.join(argv)} 失败:{proc.stderr.strip()[-400:]}")
        return json.loads(proc.stdout) if proc.stdout.strip() else None

    def setup(self):
        # 有任何副作用之前先确认名字没被占用(herdr 会话与 tmux 会话都查)
        if any(r.get("name") == self.session for r in session_list()):
            raise RuntimeError(f"herdr 会话 {self.session} 已存在,不动它")
        if subprocess.run(["tmux", "has-session", "-t", self.session + "_tty"], capture_output=True).returncode == 0:
            raise RuntimeError(f"tmux 会话 {self.session}_tty 已存在,不动它")
        fnx_dv = str(HOME / ".forenyx" / "fnx_dv")
        sources = (TURN_INSPECT, PROBE_GUARD)
        dests = [Path(fnx_dv) / "agent" / "extensions" / src.name for src in sources]
        for dest in dests:
            if dest.exists():
                raise RuntimeError(f"{dest} 已存在,不覆盖")
        result = plugin.install(fnx_dv)
        if result["changed"]:
            self.installed.append(fnx_dv)
        for src, dest in zip(sources, dests):
            self.ext_copies.append(dest)        # 先登记:复制中途失败,残留的部分文件也会被清理
            shutil.copy(src, dest)
        subprocess.run(["tmux", "new-session", "-d", "-x", "200", "-y", "50", "-s", self.session + "_tty",
                        f"cd {HOME} && herdr session attach {self.session}"], check=True)
        self.owns_tmux = True
        deadline = time.monotonic() + 30
        while True:
            # 一出现在 session list 就记归属(之前已确认这个名字不存在,所以它是本次建的);
            # 服务探测超时也要能清理掉它
            if not self.owns_session:
                try:
                    self.owns_session = any(r.get("name") == self.session for r in session_list())
                except Exception as exc:        # 暂时查不到也继续等;清理时按 owns_tmux 仍会处理该会话
                    self.report.setdefault("session_list_errors", []).append(str(exc)[:200])
            if self.owns_session and self.client.is_server_running():
                break
            if time.monotonic() > deadline:
                raise RuntimeError("会话没有启动")
            time.sleep(0.2)

    def cleanup(self):
        problems = []
        steps = []
        if self.spawned:
            steps.append(lambda: self.a2a("agent", "purge", "dv", "uart"))
        # 会话:本次确认创建的(owns_session),或本次启动了 attach 而名字事先不存在的(owns_tmux)都归本次
        if self.owns_session or self.owns_tmux:
            steps.append(self._remove_session)
        if self.owns_tmux:
            steps.append(self._kill_tmux)
        steps += [lambda d=d: d.unlink(missing_ok=True) for d in self.ext_copies]
        steps += [lambda f=f: plugin.uninstall(f) for f in self.installed]
        for step in steps:                      # 每一步单独捕获:一步失败不跳过后面的清理
            try:
                step()
            except Exception as exc:
                problems.append(f"{type(exc).__name__}: {exc}")
        if (self.owns_session or self.owns_tmux) and any(r.get("name") == self.session
                                                         for r in self._sessions_safely(problems)):
            problems.append(f"herdr 会话 {self.session} 清理后仍在")
        try:
            self.report["work_dir_empty"] = not any(self.work.iterdir())
        except OSError as exc:
            self.report["work_dir_empty"] = False
            problems.append(f"检查工作目录失败:{exc}")
        self.report["cleanup_problems"] = problems

    def _remove_session(self):
        try:
            present = any(r.get("name") == self.session for r in session_list())
        except Exception:
            present = True                       # 查不到就照样尝试删除;结果由清理后的复核判断
        if present:
            session_stop(self.session)
            session_delete(self.session)

    def _kill_tmux(self):
        name = self.session + "_tty"
        proc = subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True, text=True)
        check = subprocess.run(["tmux", "has-session", "-t", name], capture_output=True, text=True)
        if check.returncode == 0:
            raise RuntimeError(f"tmux 会话 {name} 清理后仍在(kill-session 返回 {proc.returncode}:"
                               f"{proc.stderr.strip()[:200]})")
        # 只有明确的"不存在"才算清理成功;其他失败(权限、连接错误)是查询失败,不能当作已清理
        gone = ("can't find session", "no server running", "(No such file or directory)")
        if not any(text in check.stderr for text in gone):
            raise RuntimeError(f"无法确认 tmux 会话 {name} 已清理:has-session {check.returncode}:"
                               f"{check.stderr.strip()[:200]}")

    @staticmethod
    def _sessions_safely(problems):
        try:
            return session_list()
        except Exception as exc:
            problems.append(f"读取 herdr 会话列表失败:{exc}")
            return []

    # ---- 338 -----------------------------------------------------------
    def turns(self) -> List[dict]:
        path = self.state / "turns-dv_uart.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def wait_idle(self, turns_before, timeout=180):
        """等这一轮开始(检查扩展多记了一轮)且 agent 回到可投递状态。处理很快时可能看不到 working。"""
        deadline = time.monotonic() + timeout
        statuses = []
        while time.monotonic() < deadline:
            status = self.client.agent_get("dv_uart").get("agent_status")
            if not statuses or statuses[-1] != status:
                statuses.append(status)
            if status in ("idle", "done") and len(self.turns()) > turns_before:
                return statuses
            time.sleep(1)
        return statuses + ["timeout"]

    def probe_338(self):
        self.a2a("agent", "spawn", "dv", "uart", "--cwd", str(self.work))
        self.spawned = True
        r: Dict[str, Any] = {}
        self.a2a("topology", "add-edge", *NEW_EDGE)
        r["edges_after_add"] = [e["edge_id"] for e in self.a2a_edges()]
        if not self.kickoff:
            self.report["338"] = r
            return
        before = len(self.turns())
        self.client.agent_prompt("dv_uart", PROMPT_338)
        r["statuses"] = self.wait_idle(before)
        r["turns"] = self.turns()
        r["new_edge_visible_this_turn"] = bool(r["turns"]) and NEW_EDGE[0] in (r["turns"][-1]["enum"] or [])
        if "timeout" in r["statuses"]:
            self.failures.append("338:这一轮没有按时结束")
        if not r["new_edge_visible_this_turn"]:
            self.failures.append("338:这一轮开始时 a2a_send 的可选边里没有新边")
        r["screen_tail"] = self.client.agent_read("dv_uart", source="recent-unwrapped", lines=15)
        self.report["338"] = r

    def a2a_edges(self):
        env = dict(self.env, A2A_PROJECT_ID="probe", A2A_ROLE="dv", A2A_IP="uart")
        proc = subprocess.run([sys.executable, "-m", "a2a.cli", "edges"], env=env, capture_output=True, text=True,
                              timeout=60)
        return json.loads(proc.stdout)["edges"]

    # ---- 探测 8 --------------------------------------------------------
    def session_files(self) -> List[Path]:
        d = HOME / ".forenyx" / "fnx_dv" / "agent" / "sessions" / ("--" + str(self.work).strip("/").replace("/", "-") + "--")
        return sorted(d.glob("*.jsonl")) if d.is_dir() else []

    def probe_8(self):
        self.a2a("agent", "stop", "dv", "uart")
        self.old_session_files = set(self.session_files())     # 只看本轮(restore 后)新建的会话记录

        def with_bash(data):
            data["roles"]["dv"]["launch_args"] = []
        self.store.update(with_bash)
        marker = self.state / "probe-guard-loaded-dv_uart"
        marker.unlink(missing_ok=True)          # 只认本次 restore 后新写的标记
        self.a2a("agent", "restore", "dv", "uart")
        r: Dict[str, Any] = {"guard_loaded": self.guard_loaded(marker)}
        if not r["guard_loaded"]:
            # 守卫没有在本次的 fnx 里生效:全部内置工具开放时不发提示(fail-closed)
            self.failures.append("探测 8:执行前限制(a2a-probe-guard)没有在本次的 fnx_dv 里生效,未发提示")
            self.report["probe8"] = r
            return
        if not self.kickoff:
            self.report["probe8"] = r
            return
        turns_before = len(self.turns())
        self.client.agent_prompt("dv_uart", PROMPT_P8)
        timeline, blocked = [], False
        deadline = time.monotonic() + 180
        seen_busy = False
        while time.monotonic() < deadline:
            status = self.client.agent_get("dv_uart").get("agent_status")
            screen = self.client.agent_read("dv_uart", source="recent-unwrapped", lines=25)
            words = [w for w in APPROVAL_WORDS if w in screen]
            if not timeline or timeline[-1]["status"] != status or timeline[-1]["approval_words"] != words:
                timeline.append({"t": round(time.monotonic() - (deadline - 180), 1), "status": status,
                                 "approval_words": words})
            seen_busy = seen_busy or status == "working" or len(self.turns()) > turns_before
            if status == "blocked":
                blocked = True
                r["screen_when_blocked"] = screen
                break                                   # 不自动批准
            if seen_busy and status in ("idle", "done"):
                break
            time.sleep(1)
        else:
            self.failures.append("探测 8:180 秒内既没有结束也没有 blocked")
        r["timeline"] = timeline
        r["blocked"] = blocked
        r["screen_tail"] = self.client.agent_read("dv_uart", source="recent-unwrapped", lines=25)
        r["turns"] = self.turns()
        # 屏幕上的 probe8 不算证据(提示词里就有):从 fnx 会话记录核实真的调用了 bash 并得到输出
        r["bash_pairs"] = self.bash_evidence()
        r["all_tool_calls"] = self.all_tool_calls
        r["bash_ran"] = any("echo probe8" in cmd and result is not None and "probe8" in result and not error
                            for cmd, result, error in r["bash_pairs"])
        if not blocked and not r["bash_ran"]:
            self.failures.append("探测 8:会话记录里没有 bash 执行 echo probe8 并得到 probe8 的证据")
        if blocked:
            self.a2a("agent", "stop", "dv", "uart", check=False)
        self.report["probe8"] = r

    def guard_loaded(self, marker: Path, timeout: float = 15) -> bool:
        """守卫标记出现,且内容等于本次 fnx_dv 的进程号(注册表里登记的进程组号 = pi 进程号)。"""
        from a2a.registry import Registry
        pgid = Registry(self.state / "registry.json").get("dv_uart").pgid
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if marker.exists() and marker.read_text().strip() == str(pgid):
                return True
            time.sleep(0.5)
        return False

    def bash_evidence(self):
        """本轮新建的 fnx_dv 会话记录里的 bash 调用,按调用 ID 与结果配对:[(命令, 结果文字或 None, 是否出错)]。"""
        old = getattr(self, "old_session_files", set())
        calls, results = {}, {}
        self.all_tool_calls = []                 # 本轮所有工具调用(含被执行前限制拦下的)
        for f in self.session_files():
            if f in old:
                continue
            for line in f.read_text(errors="replace").splitlines():
                try:
                    m = json.loads(line).get("message") or {}
                except ValueError:
                    continue
                for c in m.get("content") or []:
                    if c.get("type") == "toolCall":
                        self.all_tool_calls.append({"name": c.get("name"), "arguments": c.get("arguments")})
                    if c.get("type") == "toolCall" and c.get("name") == "bash" and c.get("id"):
                        calls[c["id"]] = str((c.get("arguments") or {}).get("command", ""))
                if m.get("role") == "toolResult" and m.get("toolName") == "bash" and m.get("toolCallId"):
                    results[m["toolCallId"]] = ("".join(c.get("text", "") for c in m.get("content") or []),
                                                bool(m.get("isError")))
        return [(cmd, *results.get(cid, (None, False))) for cid, cmd in calls.items()]

    def run(self) -> bool:
        try:
            self.setup()
            self.probe_338()
            self.probe_8()
        except Exception as exc:
            self.failures.append(f"{type(exc).__name__}: {exc}")
        finally:
            self.cleanup()
            if self.report["cleanup_problems"]:
                self.failures.append(f"清理有问题:{self.report['cleanup_problems']}")
            if not self.report["work_dir_empty"]:
                self.failures.append("agent 工作目录不为空")
            self.report["failures"] = self.failures
            self.report["ok"] = not self.failures
            (self.out / "report.json").write_text(json.dumps(self.report, ensure_ascii=False, indent=2))
            print(json.dumps(self.report, ensure_ascii=False, indent=2))
        return not self.failures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-kickoff", action="store_true", help="不发提示、不调用模型,只验证准备与清理")
    parser.add_argument("--out")
    args = parser.parse_args()
    if os.environ.get("A2A_REAL_FNX") != "1":
        print("需要设置 A2A_REAL_FNX=1(会调用模型)", file=sys.stderr)
        return 2
    return 0 if Probe(args).run() else 1


if __name__ == "__main__":
    sys.exit(main())
