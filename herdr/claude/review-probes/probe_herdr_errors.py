"""herdr 错误码"未提交"语义实测(方案见 herdr/claude/09-herdr-error-probe-plan.md)。

在 VM 里运行:
    python3 herdr/claude/review-probes/probe_herdr_errors.py

只使用独立命名会话 a2a_errprobe 和目录 ~/A2A_Test/errprobe/,结束时全部清理。
假 agent 是 fake_agent_logger.py,不启动 fnx,不调用任何模型。
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "a2a" / "src"))

from a2a.herdr_client import READY_STATUSES, HerdrClient, session_delete, session_list, session_stop  # noqa: E402

SESSION = "a2a_errprobe"
TMUX = SESSION + "_tty"
WORK = Path.home() / "A2A_Test" / "errprobe"
LOGGER = WORK / "fake_agent_logger.py"
PROTECTED = {"default", "test1", "a2a_verify", "a2a_codex_verify"}
RESULTS = []
client: HerdrClient


def log(msg: str) -> None:
    print(msg, flush=True)


def raw(*args: str, timeout: float = 30) -> dict:
    """直接调 herdr 命令行,原样记录退出码、错误码和输出。"""
    argv = ["herdr", "--session", SESSION, *args]
    t0 = time.monotonic()
    try:
        cp = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"rc": None, "code": "client_timeout", "ms": int((time.monotonic() - t0) * 1000)}
    out = (cp.stdout or "") + (cp.stderr or "")
    code = message = None
    value = None
    try:
        value = json.loads(cp.stdout) if cp.stdout.strip() else None
    except ValueError:
        for line in out.splitlines():
            try:
                value = json.loads(line)
                break
            except ValueError:
                continue
    if value is None:
        try:
            value = json.loads(cp.stderr)
        except ValueError:
            value = None
    if isinstance(value, dict) and isinstance(value.get("error"), dict):
        code, message = value["error"].get("code"), value["error"].get("message")
    return {"rc": cp.returncode, "code": code, "message": message, "ms": int((time.monotonic() - t0) * 1000),
            "value": value, "out": out.strip()[:300]}


def agent_snapshot(target: str) -> dict:
    r = raw("agent", "get", target)
    if r["rc"] != 0:
        return {"error": r["code"]}
    agent = r["value"]["result"]["agent"]
    return {k: agent.get(k) for k in ("agent", "agent_status", "state_change_seq", "pane_id")}


def logged(path: Path) -> bytes:
    return path.read_bytes() if path.exists() else b""


def new_fake_pane(label: str, argv0: str = "pi") -> "tuple[str, Path]":
    created = client.tab_create(label=label, cwd=str(Path.home()))
    logfile = WORK / f"{label}.log"
    client.pane_run(created.pane_id, f"bash -c 'exec -a {argv0} python3 {LOGGER} {logfile}'")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not _screen_has(created.pane_id, "fake agent ready"):
        time.sleep(0.3)
    return created.pane_id, logfile


def _screen_has(pane: str, needle: str) -> bool:
    try:
        return needle in client.pane_read(pane, source="recent-unwrapped", lines=40)
    except Exception:
        return False


def prompt_and_check(case: str, target: str, logfile: "Path | None", note: str = "") -> dict:
    marker = f"PROBE-{case}-{secrets.token_hex(4)}"
    before = agent_snapshot(target)
    result = raw("agent", "prompt", target, marker)
    time.sleep(2.0)
    after = agent_snapshot(target)
    written = None if logfile is None else (marker.encode() in logged(logfile))
    row = {"case": case, "note": note, "target": target, "marker": marker, "rc": result["rc"],
           "code": result["code"], "message": result["message"], "ms": result["ms"], "written_to_target": written,
           "before": before, "after": after}
    if result["code"] is None and result["rc"] != 0:
        row["out"] = result.get("out")
    RESULTS.append(row)
    log(json.dumps(row, ensure_ascii=False))
    return row


# ----------------------------------------------------------------------
def case_control() -> None:
    pane, logfile = new_fake_pane("c1_control")
    client.wait_for_agent_detected(pane, timeout_s=20)
    try:
        client.agent_wait(pane, until=READY_STATUSES, timeout_ms=15000)
    except Exception as exc:
        log(f"c1: 等待 READY 失败: {exc}")
    prompt_and_check("C1", pane, logfile, "对照组:识别为 pi 的假 agent")


def case_not_ready() -> None:
    # 2a:前台进程不是 pi,但用 report-agent 声明它是 pi(复现此前 report-agent 场景)
    pane, logfile = new_fake_pane("c2a_notready", argv0="notpi")
    r = raw("pane", "report-agent", pane, "--source", "probe", "--agent", "pi", "--state", "idle")
    log(f"c2a report-agent: rc={r['rc']} code={r['code']}")
    time.sleep(1.0)
    prompt_and_check("C2a", pane, logfile, "前台 argv0=notpi,report-agent 声明为 pi")

    # 2b:真 pi 被识别后退出,前台换成 argv0=notpi 的记录器
    pane, logfile = new_fake_pane("c2b_exit")
    client.wait_for_agent_detected(pane, timeout_s=20)
    stop = Path(str(logfile) + ".stop")
    stop.touch()
    raw("pane", "send-text", pane, "x")  # 让记录器读到一批字节后退出
    time.sleep(0.5)
    logfile2 = WORK / "c2b_after.log"
    client.pane_run(pane, f"bash -c 'exec -a notpi python3 {LOGGER} {logfile2}'")
    time.sleep(1.0)
    prompt_and_check("C2b", pane, logfile2, "pi 退出后前台换成 argv0=notpi(立即发)")


def case_blocked() -> None:
    # 3a:识别为 pi 的假 agent,用 report-agent 把状态报成 blocked
    pane, logfile = new_fake_pane("c3a_blocked")
    client.wait_for_agent_detected(pane, timeout_s=20)
    r = raw("pane", "report-agent", pane, "--source", "probe", "--agent", "pi", "--state", "blocked",
            "--message", "probe approval")
    log(f"c3a report-agent: rc={r['rc']} code={r['code']} -> {agent_snapshot(pane)}")
    time.sleep(1.0)
    prompt_and_check("C3a", pane, logfile, "argv0=pi,report-agent 报 blocked")
    # 3b:前台 notpi + report-agent blocked
    pane, logfile = new_fake_pane("c3b_blocked", argv0="notpi")
    r = raw("pane", "report-agent", pane, "--source", "probe", "--agent", "pi", "--state", "blocked")
    log(f"c3b report-agent: rc={r['rc']} code={r['code']} -> {agent_snapshot(pane)}")
    time.sleep(1.0)
    prompt_and_check("C3b", pane, logfile, "argv0=notpi,report-agent 报 blocked")


def case_not_found_static() -> None:
    prompt_and_check("C5", "w99:p99", None, "不存在的 pane")


def _tally(name: str, note: str, rows: list) -> None:
    by, msgs = {}, {}
    for row in rows:
        key = f"{row['code'] or 'ok'}|written={row['written']}"
        by[key] = by.get(key, 0) + 1
        if row.get("message"):
            msgs[row["code"]] = row["message"]
    RESULTS.append({"case": name, "note": note, "tally": by, "messages": msgs})
    log(f"{name} 汇总: " + json.dumps(by, ensure_ascii=False) + " 错误信息: " + json.dumps(msgs, ensure_ascii=False))


def case_concurrent_no_close(rounds: int = 5) -> None:
    """C6a:8 个 prompt 并发发给同一个 agent,不关闭 pane。看 agent_prompt_failed 是否只由并发引起。"""
    rows = []
    for i in range(rounds):
        pane, logfile = new_fake_pane(f"c6a_{i}")
        client.wait_for_agent_detected(pane, timeout_s=20)
        client.agent_wait(pane, until=READY_STATUSES, timeout_ms=15000)
        results = []

        def fire(n: int) -> None:
            marker = f"PROBE-C6a-{i}-{n}-{secrets.token_hex(3)}"
            results.append((marker, raw("agent", "prompt", pane, marker)))

        threads = [threading.Thread(target=fire, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        time.sleep(1.5)
        text = logged(logfile)
        for marker, r in results:
            rows.append({"code": r["code"], "message": r["message"], "written": marker.encode() in text})
        raw("pane", "close", pane)
    _tally("C6a", "8 个 prompt 并发,不关闭 pane", rows)


def case_sequential_then_close(rounds: int = 20) -> None:
    """C6b:单线程连续发 prompt,另一线程在随机时刻关闭 pane。只有关闭这一个竞态因素。"""
    rows = []
    for i in range(rounds):
        pane, logfile = new_fake_pane(f"c6b_{i}")
        client.wait_for_agent_detected(pane, timeout_s=20)
        client.agent_wait(pane, until=READY_STATUSES, timeout_ms=15000)
        results, stop = [], threading.Event()

        def sender() -> None:
            n = 0
            while not stop.is_set() and n < 60:
                marker = f"PROBE-C6b-{i}-{n}-{secrets.token_hex(3)}"
                r = raw("agent", "prompt", pane, marker)
                results.append((marker, r))
                n += 1
                if r["code"] in ("agent_not_found", "pane_not_found"):
                    break

        t = threading.Thread(target=sender)
        t.start()
        time.sleep(0.15 + 0.05 * (i % 6))
        raw("pane", "close", pane)
        time.sleep(1.0)
        stop.set()
        t.join()
        text = logged(logfile)
        # 只统计关闭前后最后几次调用:前面的都是正常成功
        for marker, r in results[-4:]:
            rows.append({"code": r["code"], "message": r["message"], "written": marker.encode() in text})
    _tally("C6b", "单线程连发,中途关闭 pane(每轮统计最后 4 次)", rows)


def case_server_not_running() -> None:
    pane, logfile = new_fake_pane("c4_server")
    client.wait_for_agent_detected(pane, timeout_s=20)
    # 用 session stop 并显式写会话名,只停本次探测会话(不用 server stop,避免作用到别的会话)
    try:
        session_stop(SESSION)
        log("c4 session stop: ok")
    except Exception as exc:
        log(f"c4 session stop: {exc}")
    time.sleep(2.0)
    prompt_and_check("C4", pane, logfile, "会话服务已停止")


# ----------------------------------------------------------------------
def setup() -> None:
    global client
    rows = {r["name"]: r for r in session_list()}
    if SESSION in rows:
        raise SystemExit(f"会话 {SESSION} 已存在,为避免误伤请先确认后手动删除")
    assert SESSION not in PROTECTED
    WORK.mkdir(parents=True, exist_ok=True)
    shutil.copy(HERE / "fake_agent_logger.py", LOGGER)
    subprocess.run(["tmux", "new-session", "-d", "-x", "200", "-y", "50", "-s", TMUX,
                    f"cd {Path.home()} && herdr session attach {SESSION}"], check=True)
    client = HerdrClient(SESSION)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if client.is_server_running():
            return
        time.sleep(0.5)
    raise RuntimeError("命名会话没有启动")


def cleanup() -> None:
    for fn in (session_stop, session_delete):
        try:
            fn(SESSION)
        except Exception as exc:
            log(f"清理 {fn.__name__}: {exc}")
    subprocess.run(["tmux", "kill-session", "-t", TMUX], capture_output=True)
    shutil.rmtree(WORK, ignore_errors=True)
    names = {r["name"] for r in session_list()}
    left = subprocess.run(["pgrep", "-af", "fake_agent_logger"], capture_output=True, text=True).stdout.strip()
    log(f"清理后:会话 {SESSION} 残留={SESSION in names};假 agent 进程残留={left or '无'}")


def main() -> None:
    setup()
    try:
        for case in (case_control, case_not_ready, case_blocked, case_not_found_static,
                     case_concurrent_no_close, case_sequential_then_close, case_server_not_running):
            log(f"===== {case.__name__}")
            try:
                case()
            except Exception as exc:
                RESULTS.append({"case": case.__name__, "error": repr(exc)})
                log(f"{case.__name__} 出错: {exc!r}")
    finally:
        cleanup()
        print("RESULTS_JSON " + json.dumps(RESULTS, ensure_ascii=False))


if __name__ == "__main__":
    main()
