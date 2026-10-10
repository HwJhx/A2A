"""集成测试用的假 agent(不调用任何模型)。

用法(在 pane 里):bash -c 'exec -a pi python3 fake_agent.py <日志文件> <模式>'
  * 以 pi 的名字运行,herdr 会把它识别为 pi agent。
  * 以 raw 模式读终端,把收到的每个字节追加进日志文件:日志就是"herdr 有没有写入"的判定依据。
  * 模式 work:每收到一次回车,就用 `herdr pane report-agent` 报告 working,1 秒后报告 idle,
    模拟"收到 prompt 后开始处理"。
  * 模式 silent:只记录,不报告状态(用来制造"herdr 接受了但没看到目标开始处理")。
"""
import os
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


def work_once():
    report("working")
    time.sleep(1.0)
    report("idle")


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
            if mode == "work" and (b"\r" in data or b"\n" in data):
                threading.Thread(target=work_once, daemon=True).start()
            if os.path.exists(log + ".stop"):
                break
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old)
