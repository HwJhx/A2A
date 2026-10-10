// 集成测试用扩展:session_start 时把 fnx 里实际生效的工具、a2a_send 的说明与参数写到状态目录。
// 只在 $A2A_STATE_DIR/inspect.enabled 存在时生效;不注册任何工具,不影响模型。
import { existsSync, writeFileSync } from "node:fs";
import { join } from "node:path";

export default function (pi: any) {
  const dir = process.env.A2A_STATE_DIR;
  if (!dir || !existsSync(join(dir, "inspect.enabled"))) return;
  pi.on("session_start", async () => {
    const send = pi.getAllTools().find((t: any) => t.name === "a2a_send");
    writeFileSync(join(dir, `inspect-${process.env.A2A_ROLE}_${process.env.A2A_IP}.json`), JSON.stringify({
      active: pi.getActiveTools(),
      a2a_send: send ? { description: send.description, parameters: send.parameters } : null,
      cwd: process.cwd(),
    }));
  });
}
