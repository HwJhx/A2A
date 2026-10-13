// 探测 8 专用的执行前限制:工具调用在执行之前经过 tool_call 钩子,这里只放行 bash 执行 `echo probe8`,
// 其他一律拦下(edit / write / read / 其他 bash 命令 / a2a_send)。探测 8 给 fnx_dv 开放了全部内置工具,
// 不能只靠提示词约束模型。只在 $A2A_STATE_DIR/probe-guard.enabled 存在时生效;只在测试时复制进 fnx,结束删除。
import { existsSync, writeFileSync } from "node:fs";
import { join } from "node:path";

export const ALLOWED_COMMAND = "echo probe8";

export function allowed(toolName: string, input: any): boolean {
  return toolName === "bash" && String(input?.command ?? "").trim() === ALLOWED_COMMAND;
}

export default function (pi: any) {
  const dir = process.env.A2A_STATE_DIR;
  if (!dir || !existsSync(join(dir, "probe-guard.enabled"))) return;
  pi.on("tool_call", async (event: any) => {
    if (allowed(event.toolName, event.input)) return undefined;
    return { block: true, reason: `探测限制:只允许 bash 执行 \`${ALLOWED_COMMAND}\`` };
  });
  // 钩子注册之后才写标记(内容是 fnx 进程号):探测脚本发提示前确认它存在且属于本次的 fnx,否则不发(fail-closed)
  writeFileSync(join(dir, `probe-guard-loaded-${process.env.A2A_ROLE}_${process.env.A2A_IP}`), String(process.pid));
}
