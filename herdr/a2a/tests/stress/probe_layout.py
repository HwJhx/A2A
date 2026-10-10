"""阶段 8 §3.0:探测 herdr 的布局上限(不涉及 a2a 的路由与投递)。方案见 herdr/claude/13-stage8-scale-plan.md。

在独立命名会话里按实际目标布局创建:1 个 workspace、TABS 个 tab、每个 tab 向下拆到 PANES 个 pane,
每个 pane 里运行一个空闲的假 agent(以 pi 的名字运行,work 模式)。记录每一步的耗时、失败点与报错,
以及全部建好后 `agent list` / `pane list` 的耗时和 herdr 服务的内存。结束时删除会话。

只有设置 A2A_STRESS=1 才会运行:
  A2A_STRESS=1 PYTHONPATH=src python3 tests/stress/probe_layout.py [--tabs 5] [--panes 30]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "src"))

from a2a.herdr_client import HerdrClient, HerdrError, session_delete, session_list, session_stop  # noqa: E402

FAKE = HERE.parent / "fake_agent.py"


def mem_available_mb() -> float:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024
    return float("nan")


def pss_mb(pid: int) -> float:
    try:
        for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
            if line.startswith("Pss:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    return float("nan")


def server_pids() -> set:
    """当前所有 `herdr server` 进程。服务进程的命令行里没有会话名,所以靠会话启动前后对比找出本会话的。"""
    out = subprocess.run(["pgrep", "-f", "herdr server"], capture_output=True, text=True).stdout
    return {int(pid) for pid in out.split()}


def timed(fn, *args, **kwargs):
    started = time.monotonic()
    value = fn(*args, **kwargs)
    return value, time.monotonic() - started


def main() -> int:
    if os.environ.get("A2A_STRESS") != "1":
        print("需要设置 A2A_STRESS=1")
        return 2
    parser = argparse.ArgumentParser()
    parser.add_argument("--tabs", type=int, default=5)
    parser.add_argument("--panes", type=int, default=30)
    parser.add_argument("--session", default=f"a2a_s8probe_{os.getpid()}")
    args = parser.parse_args()

    # 只使用、只清理本次新建的会话:同名会话已存在就直接退出,不碰它
    tmux = args.session + "_tty"
    if any(row.get("name") == args.session for row in session_list()):
        print(f"herdr 会话 {args.session} 已存在,不使用、也不清理它")
        return 2
    if subprocess.run(["tmux", "has-session", "-t", tmux], capture_output=True).returncode == 0:
        print(f"tmux 会话 {tmux} 已存在,不使用、也不清理它")
        return 2
    work = Path(tempfile.mkdtemp(prefix="a2a-s8probe-"))
    report = {"tabs": args.tabs, "panes_per_tab": args.panes, "mem_available_mb_before": mem_available_mb(),
              "steps": [], "failures": []}
    servers_before = server_pids()
    subprocess.run(["tmux", "new-session", "-d", "-x", "200", "-y", "50", "-s", tmux,
                    f"cd {Path.home()} && herdr session attach {args.session}"], check=True)
    client = HerdrClient(args.session)
    owns_session = False
    status = 0
    try:
        deadline = time.monotonic() + 30
        while not client.is_server_running():
            if time.monotonic() > deadline:
                raise RuntimeError("会话没有启动")
            time.sleep(0.5)
        if not any(row.get("name") == args.session for row in session_list()):
            raise RuntimeError(f"会话 {args.session} 没有出现在 herdr session list 里")
        owns_session = True
        new_servers = server_pids() - servers_before
        if len(new_servers) != 1:
            # 找不准本会话的服务进程就无法测它的内存;不输出可能误导的数值
            raise RuntimeError(f"会话启动后新增的 herdr server 进程有 {len(new_servers)} 个(预期 1 个)")
        spid = new_servers.pop()
        report["server_pid"] = spid
        report["server_pss_mb_empty"] = pss_mb(spid)

        launch = f"bash -c 'exec -a pi {sys.executable} {FAKE} {work}/%s.log work'"
        ws, dt = timed(client.workspace_create, label="s8probe", cwd=str(work))
        report["steps"].append({"op": "workspace_create", "s": round(dt, 3)})
        panes = []
        for t in range(args.tabs):
            if t == 0:
                tab_id, first = ws.tab_id, ws.pane_id
            else:
                created, dt = timed(client.tab_create, workspace_id=ws.workspace_id, label=f"tab{t}", cwd=str(work))
                report["steps"].append({"op": "tab_create", "tab": t, "s": round(dt, 3)})
                tab_id, first = created.tab_id, created.pane_id
            last = first
            tab_panes = [first]
            for p in range(1, args.panes):
                try:
                    created, dt = timed(client.pane_split, last, direction="down", cwd=str(work))
                except HerdrError as exc:
                    # 方案 §3.0:布局放不下就停下来请用户决定,不在不完整的布局上继续
                    report["failures"].append({"tab": t, "pane_index": p, "error": f"{type(exc).__name__}: {exc}"})
                    raise RuntimeError(f"tab{t} 第 {p + 1} 个 pane 拆分失败:{exc}") from exc
                report["steps"].append({"op": "pane_split", "tab": t, "pane": p, "s": round(dt, 3)})
                last = created.pane_id
                tab_panes.append(last)
            panes.extend((tab_id, pane) for pane in tab_panes)
            print(f"tab{t}:{len(tab_panes)} 个 pane")

        started = time.monotonic()
        for i, (_, pane) in enumerate(panes):
            client.pane_run(pane, launch % i)
        report["pane_run_all_s"] = round(time.monotonic() - started, 3)

        # 等全部被识别为 pi
        deadline = time.monotonic() + 120
        detected = 0
        while time.monotonic() < deadline:
            detected = sum(1 for a in client.agent_list() if a.get("agent") == "pi")
            if detected >= len(panes):
                break
            time.sleep(1)
        report["panes_created"] = len(panes)
        report["agents_detected"] = detected
        report["detect_all_s"] = round(time.monotonic() - started, 3)

        for name, fn in (("agent_list", client.agent_list), ("pane_list", client.pane_list)):
            samples = [timed(fn)[1] for _ in range(5)]
            report[f"{name}_s"] = {"min": round(min(samples), 3), "max": round(max(samples), 3)}
        report["server_pss_mb_full"] = pss_mb(spid)
        report["mem_available_mb_full"] = mem_available_mb()
        if detected < len(panes):
            raise RuntimeError(f"只识别到 {detected}/{len(panes)} 个 agent")
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        print("探测失败:", report["error"])
        status = 1
    finally:
        cleanup_failures = []

        def attempt(what, fn):
            """一步清理或检查;任何异常都记为失败并继续,保证报告能写出。"""
            try:
                problem = fn()
                if problem:
                    cleanup_failures.append(f"{what}: {problem}")
            except Exception as exc:
                cleanup_failures.append(f"{what}: {type(exc).__name__}: {exc}")

        def tmux_gone():
            subprocess.run(["tmux", "kill-session", "-t", tmux], capture_output=True, timeout=30)
            alive = subprocess.run(["tmux", "has-session", "-t", tmux], capture_output=True, timeout=30).returncode == 0
            return "仍在" if alive else None

        left = []
        if owns_session:
            attempt("session_stop", lambda: session_stop(args.session) and None)
            attempt("session_delete", lambda: session_delete(args.session) and None)
        attempt(f"tmux 会话 {tmux}", tmux_gone)  # 本次新建(开头已确认不存在)
        time.sleep(2)
        attempt("检查残留进程", lambda: left.extend(
            subprocess.run(["pgrep", "-f", f"{FAKE} {work}"], capture_output=True, text=True, timeout=30).stdout.split()))
        if left:
            cleanup_failures.append(f"残留假 agent 进程: {left}")
        if owns_session:
            attempt(f"会话 {args.session}",
                    lambda: "仍在" if any(row.get("name") == args.session for row in session_list()) else None)
        report["leftover_fake_pids"] = left
        report["cleanup_failures"] = cleanup_failures
        if cleanup_failures:
            print("清理失败:", cleanup_failures)
            status = 1
        report["mem_available_mb_after"] = mem_available_mb()
        print(json.dumps({k: v for k, v in report.items() if k != "steps"}, ensure_ascii=False, indent=2))
        splits = [s["s"] for s in report["steps"] if s["op"] == "pane_split"]
        if splits:
            print(f"pane_split 次数 {len(splits)},耗时 最小 {min(splits)}s 最大 {max(splits)}s")
        (work / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print("完整报告:", work / "report.json")
    return status


if __name__ == "__main__":
    sys.exit(main())
