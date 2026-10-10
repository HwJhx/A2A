"""集成测试用的假 agent(不调用任何模型)。

用法(在 pane 里):bash -c 'exec -a pi python3 fake_agent.py <日志文件> <模式>'
  * 以 pi 的名字运行,herdr 会把它识别为 pi agent。
  * 以 raw 模式读终端,把收到的每个字节追加进日志文件:日志就是"herdr 有没有写入"的判定依据。
  * 模式 work:每收到一次回车,就用 `herdr pane report-agent` 报告 working,1 秒后报告 idle,
    模拟"收到 prompt 后开始处理"。
  * 模式 silent:只记录,不报告状态(用来制造"herdr 接受了但没看到目标开始处理")。
  * 模式 slow:同 work,但收到回车 3 秒后才报告 working(短于 herdr 的 5 秒 stall),
    用来让 `agent prompt --wait` 在一段时间内保持进行中。
  * 模式 script:<边1>,<边2>,...:第 n 次收到输入(按回车计数)时报告 working,第 n 项是边 id 就在自己的
    进程里执行 `a2a send <边>`(用 spawn 注入的 A2A_PYTHON、A2A_SRC,和 pi 插件一样),是 `-` 或超出列表
    就只记录;然后报告 idle。按顺序逐次处理。发送结果写进 <日志>.events(JSON Lines),不混进收到的文字。
    设了环境变量 FAKE_BARRIER(文件路径)时,发送前先在 events 里记 barrier_wait,等该文件出现再发。
"""
import json
import os
import queue
import subprocess
import sys
import termios
import threading
import time
import tty

log, mode = sys.argv[1], sys.argv[2]
herdr = os.environ.get("HERDR_BIN_PATH") or "herdr"
pane = os.environ.get("HERDR_PANE_ID", "")


def report(state):
    subprocess.run([herdr, "pane", "report-agent", pane, "--source", "a2a-fake", "--agent", "pi",
                    "--state", state], capture_output=True)


def work_once(delay=0.0):
    time.sleep(delay)
    report("working")
    time.sleep(1.0)
    report("idle")


def event(record):
    with open(log + ".events", "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def script_worker(edges, inputs):
    n = 0
    while True:
        inputs.get()
        n += 1
        started = time.monotonic()
        report("working")
        edge = edges[n - 1] if n <= len(edges) else "-"
        if edge != "-":
            barrier = os.environ.get("FAKE_BARRIER")
            if barrier:
                event({"n": n, "barrier_wait": edge})
                while not os.path.exists(barrier):
                    time.sleep(0.05)
            env = dict(os.environ, PYTHONPATH=os.environ["A2A_SRC"], PYTHONDONTWRITEBYTECODE="1")
            proc = subprocess.run([os.environ["A2A_PYTHON"], "-m", "a2a.cli", "send", edge], env=env,
                                  capture_output=True, text=True, timeout=60)
            event({"n": n, "edge": edge, "rc": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr})
        else:
            event({"n": n, "edge": None})
        time.sleep(max(0.0, 1.0 - (time.monotonic() - started)))
        report("idle")


inputs = queue.Queue()
if mode.startswith("script:"):
    threading.Thread(target=script_worker, args=(mode[len("script:"):].split(","), inputs), daemon=True).start()

fd = sys.stdin.fileno()
old = termios.tcgetattr(fd)
tty.setraw(fd)
sys.stdout.write("fake agent ready (%s)\r\n" % mode)
sys.stdout.flush()
try:
    with open(log, "ab", buffering=0) as out:
        while True:
            data = os.read(fd, 4096)
            if not data:
                break
            out.write(data)
            if mode.startswith("script:"):
                for _ in range(data.count(b"\r") or data.count(b"\n")):
                    inputs.put(None)
            if mode in ("work", "slow") and (b"\r" in data or b"\n" in data):
                threading.Thread(target=work_once, args=(3.0 if mode == "slow" else 0.0,), daemon=True).start()
            if os.path.exists(log + ".stop"):
                break
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old)
