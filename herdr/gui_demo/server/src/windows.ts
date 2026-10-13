// 每个 agent 一个"窗口"tmux 会话,里面循环运行 `herdr --session <会话> agent attach <agent>`(21 号文档 §3):
//  * attach 退出(agent 停止、pane 关闭、会话重启)后显示提示,3 秒后重试,所以 stop / restore / recover 后窗口自己接上;
//  * 循环里忽略 Ctrl-C,窗口里不会落到一个可用的 shell;
//  * 多个浏览器窗口可同时 attach 同一个 tmux(herdr 一个终端只允许一个 attach 客户端,tmux 这一层解决共享);
//    window-size latest:窗口尺寸跟随最近有操作的那个浏览器窗口。
import fs from "node:fs";
import path from "node:path";
import { HERDR, HOME, TMUX } from "./config.js";
import { killTmux, tmuxSessions, windowTmuxPrefix } from "./herdr.js";
import { run } from "./proc.js";

/** 把命令名解析成绝对路径:tmux 会话里的命令用的是 tmux 服务进程的 PATH,不一定有 ~/.local/bin。 */
function resolveBin(cmd: string): string {
  if (cmd.includes("/")) return path.resolve(cmd);
  for (const dir of (process.env.PATH ?? "").split(":")) {
    const full = path.join(dir, cmd);
    try {
      fs.accessSync(full, fs.constants.X_OK);
      return full;
    } catch {
      /* 下一个 */
    }
  }
  throw new Error(`找不到可执行文件 ${cmd}`);
}

const HERDR_ABS = resolveBin(HERDR);
export const windowName = (projectId: string, agentId: string) => `${windowTmuxPrefix(projectId)}${agentId}`;

// 只用于 bash -c 的固定脚本;会话名与 agent 名作为位置参数传入,不拼进脚本(两者也都已按规则校验)
const LOOP = `trap '' INT QUIT TSTP
while true; do
  "$0" --session "$1" agent attach "$2"
  printf '\\r\\n\\033[2m[agent %s 未运行或连接已断开,3 秒后重试]\\033[0m\\r\\n' "$2"
  sleep 3
done`;

/** 确保窗口 tmux 存在,并(每次都)设好选项:已存在的窗口也会被纠正。 */
export async function ensureWindow(projectId: string, session: string, agentId: string): Promise<string> {
  const name = windowName(projectId, agentId);
  if (!(await tmuxSessions()).includes(name)) {
    const r = await run(TMUX, ["new-session", "-d", "-s", name, "-x", "120", "-y", "30", "-c", HOME,
      "bash", "-c", LOOP, HERDR_ABS, session, agentId], { timeoutMs: 15_000 });
    if (r.code !== 0 && !(await tmuxSessions()).includes(name)) throw new Error(`建窗口 tmux 失败:${r.stderr.trim()}`);
  }
  // 目标写成 "=会话名:":tmux 3.2 的 set-option 不认 "=会话名",加冒号才是精确匹配(不会按前缀匹配到别的会话)
  for (const [opt, value] of [["status", "off"], ["window-size", "latest"], ["destroy-unattached", "off"]]) {
    const r = await run(TMUX, ["set-option", "-t", `=${name}:`, opt, value], { timeoutMs: 10_000 });
    if (r.code !== 0) throw new Error(`设置窗口 tmux 选项 ${opt} 失败:${r.stderr.trim()}`);
  }
  return name;
}

export async function closeWindow(projectId: string, agentId: string): Promise<void> {
  await killTmux(windowName(projectId, agentId));
}
