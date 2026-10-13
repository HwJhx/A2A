// 真实验证用扩展(路线图 338):每轮 agent_start 时(在 a2a 插件的 before_agent_start 刷新之后)
// 把 a2a_send 当前的可选边与启用的工具追加到 $A2A_STATE_DIR/turns-<角色>_<IP>.jsonl。
// 只在 $A2A_STATE_DIR/inspect.enabled 存在时生效;不注册任何工具,不影响模型。
import { appendFileSync, existsSync } from "node:fs";
import { join } from "node:path";

export default function (pi: any) {
  const dir = process.env.A2A_STATE_DIR;
  if (!dir || !existsSync(join(dir, "inspect.enabled"))) return;
  pi.on("agent_start", async () => {
    const send = pi.getAllTools().find((t: any) => t.name === "a2a_send");
    appendFileSync(join(dir, `turns-${process.env.A2A_ROLE}_${process.env.A2A_IP}.jsonl`), JSON.stringify({
      ts: new Date().toISOString(),
      enum: send ? send.parameters?.properties?.edge_id?.enum ?? null : null,
      active: pi.getActiveTools(),
    }) + "\n");
  });
}
