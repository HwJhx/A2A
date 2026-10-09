"""假 agent:把终端收到的每个字节原样追加写进日志文件(09-herdr-error-probe-plan.md)。

用法: bash -c 'exec -a pi python3 fake_agent_logger.py <日志文件>'
以 raw 模式读终端,不需要回车也能记录;日志文件就是"herdr 有没有写入"的判定依据。
存在 <日志文件>.stop 时,收到下一批字节后退出。不调用任何模型。
"""
import os
import sys
import termios
import tty

log = sys.argv[1]
fd = sys.stdin.fileno()
try:
    old = termios.tcgetattr(fd)
    tty.setraw(fd)
except termios.error:
    old = None
sys.stdout.write("fake agent ready\r\n")
sys.stdout.flush()
try:
    with open(log, "ab", buffering=0) as out:
        while True:
            data = os.read(fd, 4096)
            if not data:
                break
            out.write(data)
            if os.path.exists(log + ".stop"):
                break
finally:
    if old is not None:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
