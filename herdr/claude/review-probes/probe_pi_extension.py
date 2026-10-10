"""阶段 6 探测:fnx 是否从 agent/extensions/ 自动加载扩展,--no-builtin-tools 是否只保留扩展工具。

不发任何提示词、不调用模型。用独立命名会话 a2ap<pid>,结束时删除会话并移除探测扩展。
用法(VM 里):PYTHONPATH=<repo>/herdr/a2a/src python3 probe_pi_extension.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from a2a.herdr_client import HerdrClient, session_delete, session_stop
from a2a.launcher import build_launch_command

HERE = Path(__file__).resolve().parent
PROBE = HERE / "pi-ext" / "a2a-probe.ts"
SESSION = "a2ap%d" % os.getpid()
TMUX = SESSION + "_tty"
FNX = {"dv": Path.home() / ".forenyx/fnx_dv", "sw": Path.home() / ".forenyx/fnx_sw"}


def run_case(client, role, args, out, cwd):
    tab = client.tab_create(label="probe_%s_%s" % (role, "-".join(args) or "default"), cwd=cwd,
                            env={"A2A_PROBE_OUT": str(out), "A2A_ROLE": role, "A2A_IP": "uart"})
    try:
        client.pane_run(tab.pane_id, build_launch_command(str(FNX[role] / "bin" / ("fnx_" + role)), args))
        client.wait_for_agent_detected(tab.pane_id, timeout_s=60)
        client.agent_wait(tab.pane_id, until=("idle", "done"), timeout_ms=60000)
        time.sleep(3)
        lines = [json.loads(l) for l in out.read_text().splitlines()] if out.exists() else []
        return {"role": role, "args": args, "records": lines}
    finally:
        client.tab_close(tab.tab_id)


def main():
    installed = []
    for role, root in FNX.items():
        ext_dir = root / "agent" / "extensions"
        ext_dir.mkdir(exist_ok=True)
        target = ext_dir / "a2a-probe.ts"
        shutil.copy(PROBE, target)
        installed.append(target)
    subprocess.run(["tmux", "new-session", "-d", "-x", "200", "-y", "50", "-s", TMUX,
                    "herdr session attach %s" % SESSION], check=True)
    work = Path(tempfile.mkdtemp(prefix="a2a-probe-"))
    results = []
    try:
        client = HerdrClient(SESSION)
        deadline = time.monotonic() + 30
        while not client.is_server_running():
            if time.monotonic() > deadline:
                raise RuntimeError("会话没有启动")
            time.sleep(0.5)
        for i, (role, args) in enumerate([("dv", []), ("dv", ["--no-builtin-tools"]),
                                          ("sw", ["--no-builtin-tools"])]):
            results.append(run_case(client, role, args, work / ("out%d.jsonl" % i), str(work)))
    finally:
        for fn in (session_stop, session_delete):
            try:
                fn(SESSION)
            except Exception as exc:
                print("清理 %s 失败: %s" % (fn.__name__, exc), file=sys.stderr)
        subprocess.run(["tmux", "kill-session", "-t", TMUX], capture_output=True)
        for target in installed:
            target.unlink(missing_ok=True)
            try:
                target.parent.rmdir()  # 只在目录为空时删除(原本不存在)
            except OSError:
                pass
        # fnx 会按工作目录在 agent/sessions/ 下建一个(空的)会话目录;只删这次探测建的、且为空的
        for root in FNX.values():
            for d in (root / "agent" / "sessions").glob("--tmp-a2a-probe-*"):
                try:
                    d.rmdir()
                except OSError:
                    print("保留非空的会话目录: %s" % d, file=sys.stderr)
        leftovers = sorted(p.name for p in work.iterdir() if not p.name.startswith("out"))
        shutil.rmtree(work, ignore_errors=True)
    print(json.dumps({"results": results, "files_created_in_cwd": leftovers}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
