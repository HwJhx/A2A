// 运行配置。全部可用环境变量覆盖(测试用),默认值面向 VM 上的 demo。
import os from "node:os";
import path from "node:path";

function defaultHost(): string {
  // VM 在 OrbStack 网络里的地址(DHCP,可能变化),只监听它,不监听 0.0.0.0
  const eth0 = os.networkInterfaces().eth0 ?? [];
  const v4 = eth0.find((a) => a.family === "IPv4" && !a.internal);
  if (!v4) throw new Error("找不到 eth0 的 IPv4 地址;请用 GUI_DEMO_HOST 指定监听地址");
  return v4.address;
}

export const HOME = os.homedir();
export const DATA_DIR = path.resolve(process.env.GUI_DEMO_HOME ?? path.join(HOME, ".a2a_gui_demo"));
export const PROJECTS_FILE = path.join(DATA_DIR, "projects.json");
export const ADMIN_SOCK = path.join(DATA_DIR, "admin.sock");
export const HOST = process.env.GUI_DEMO_HOST ?? defaultHost();
export const PORT = Number(process.env.GUI_DEMO_PORT ?? 8765);
export const ORIGIN = `http://${HOST}:${PORT}`;

// a2a 源码(VM 上经 OrbStack 挂载的 Mac 仓库;只读使用)
export const A2A_SRC = process.env.GUI_DEMO_A2A_SRC ?? "/Users/jhx/Documents/code/personal/pi_agent/A2A/herdr/a2a/src";
export const PYTHON = process.env.GUI_DEMO_PYTHON ?? "python3";
export const HERDR = process.env.GUI_DEMO_HERDR ?? "herdr";
export const TMUX = process.env.GUI_DEMO_TMUX ?? "tmux";

// herdr 会话名与 tmux 会话名的前缀;测试用 guidemo_test_,不会与用户、Codex 的会话重名
export const SESSION_PREFIX = process.env.GUI_DEMO_SESSION_PREFIX ?? "guidemo_";

export const LAUNCHERS: Record<string, string> = {
  dv: process.env.GUI_DEMO_LAUNCHER_DV ?? path.join(HOME, ".forenyx/fnx_dv/bin/fnx_dv"),
  sw: process.env.GUI_DEMO_LAUNCHER_SW ?? path.join(HOME, ".forenyx/fnx_sw/bin/fnx_sw"),
};

// 仅测试用:让新建工程在指定步骤失败,验证回滚
export const FAIL_AT = process.env.GUI_DEMO_FAIL_AT ?? "";
