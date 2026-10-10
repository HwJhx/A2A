// 阶段 6 探测用扩展:只记录"是否被加载、有哪些工具",不注册真实功能。
// 只在设置了 A2A_PROBE_OUT 的进程里生效,其他时候什么都不做。
import { appendFileSync } from "node:fs";

export default function (pi: any) {
  const out = process.env.A2A_PROBE_OUT;
  if (!out) return;
  const log = (obj: unknown) => appendFileSync(out, JSON.stringify(obj) + "\n");
  log({ event: "loaded", pid: process.pid, role: process.env.A2A_ROLE, ip: process.env.A2A_IP,
        session: process.env.HERDR_SESSION, pane: process.env.HERDR_PANE_ID, cwd: process.cwd() });
  pi.registerTool({
    name: "a2a_probe",
    label: "a2a probe",
    description: "探测用工具,不要调用",
    parameters: { type: "object", properties: {}, additionalProperties: false },
    async execute() {
      return { content: [{ type: "text", text: "probe" }], details: {} };
    },
  });
  pi.on("session_start", async () => {
    log({ event: "session_start", active: pi.getActiveTools(),
          all: pi.getAllTools().map((t: any) => t.name) });
  });
}
