// 用假的 pi 对象加载 pi-extension/a2a.ts,按命令行动作执行并输出 JSON(供 test_pi_extension.py 调用)。
//   node plugin_harness.mjs <插件路径> describe
//   node plugin_harness.mjs <插件路径> send <edge_id>
//   node plugin_harness.mjs <插件路径> tool_call <toolName> <input JSON>
//   node plugin_harness.mjs <插件路径> events <事件列表 JSON>
import { pathToFileURL } from "node:url";

const [plugin, action, ...rest] = process.argv.slice(2);
const mod = await import(pathToFileURL(plugin).href);
const tools = [];
const handlers = {};
const pi = {
  registerTool: (tool) => tools.push(tool),
  on: (event, handler) => (handlers[event] ??= []).push(handler),
};
await mod.default(pi);

let result;
if (action === "describe") {
  result = { tools: tools.map(({ name, description, parameters }) => ({ name, description, parameters })),
             events: Object.keys(handlers) };
} else if (action === "send") {
  try {
    const out = await tools[0].execute("call-1", { edge_id: rest[0] }, undefined, () => {}, {});
    result = { ok: true, content: out.content, details: out.details };
  } catch (err) {
    result = { ok: false, error: err.message };
  }
} else if (action === "tool_call") {
  const outcome = await handlers.tool_call[0]({ toolName: rest[0], input: JSON.parse(rest[1]) });
  result = { outcome: outcome ?? null };
} else if (action === "events") {
  // rest[0]:JSON 列表 [{name, event, usage}];依次触发,等每个 handler 完成,记录耗时
  result = [];
  for (const spec of JSON.parse(rest[0])) {
    const ctx = { getContextUsage: () => spec.usage ?? undefined, cwd: spec.cwd ?? process.cwd() };
    const started = Date.now();
    let error = null;
    for (const handler of handlers[spec.name] ?? []) {
      try {
        await handler(spec.event ?? {}, ctx);
      } catch (err) {
        error = String(err);
      }
    }
    result.push({ name: spec.name, ms: Date.now() - started, error });
  }
} else if (action === "is_herdr_write") {
  result = Object.fromEntries(JSON.parse(rest[0]).map((c) => [c, mod.isHerdrWrite(c)]));
}
process.stdout.write(JSON.stringify(result));
