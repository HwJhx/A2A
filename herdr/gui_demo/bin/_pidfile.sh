# bin/start、bin/stop 共用(source):pid 文件内容是"pid 启动时间",核对两者和命令行都对得上才认作后端。
# 后端异常退出后 pid 文件会留下,pid 可能被别的进程复用:核对不上就当陈旧文件,绝不给它发信号。
DATA="${GUI_DEMO_HOME:-$HOME/.a2a_gui_demo}"
PIDF="$DATA/server.pid"
LOG="$DATA/server.log"

# /proc/<pid>/stat 第 22 项:进程启动时间(开机后的时钟滴答数);先去掉"(进程名)"部分,避免名字里有空格
start_time() { sed 's/^.*) //' "/proc/$1/stat" 2>/dev/null | awk '{print $20}'; }

# 读 pid 文件;是本后端返回 0 并设 PID,否则返回 1
backend_pid() {
  PID=""
  [ -f "$PIDF" ] || return 1
  local pid st
  read -r pid st < "$PIDF" || true
  [[ "$pid" =~ ^[0-9]+$ && -n "$st" ]] || return 1
  [ "$(start_time "$pid")" = "$st" ] || return 1
  tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q "server/dist/main.js" || return 1
  PID="$pid"
}

# 结束进程组(setsid 启动,进程组号 = pid),等它退出;返回 0 表示已退出
stop_group() {
  local pid="$1" st="$2" waited
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  for waited in $(seq 150); do
    [ "$(start_time "$pid")" = "$st" ] || return 0
    sleep 0.2
  done
  kill -KILL -- "-$pid" 2>/dev/null || true
  for waited in $(seq 25); do
    [ "$(start_time "$pid")" = "$st" ] || return 0
    sleep 0.2
  done
  return 1
}
