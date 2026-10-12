// 用假的 pi 对象加载 pi-extension/a2a.ts,按命令行动作执行并输出 JSON(供 test_pi_extension.py 调用)。
//   node plugin_harness.mjs <插件路径> describe
//   node plugin_harness.mjs <插件路径> send <edge_id>
//   node plugin_harness.mjs <插件路径> tool_call <toolName> <input JSON>
//   node plugin_harness.mjs <插件路径> events <事件列表 JSON>
//   node plugin_harness.mjs <插件路径> rounds <步骤列表 JSON>   每步先改拓扑文件,再触发 before_agent_start
import { copyFileSync, writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const [plugin, action, ...rest] = process.argv.slice(2);
const mod = await import(pathToFileURL(plugin).href);
// 按 fnx 自带 pi 0.79.10 的行为模拟:工具按名字存放(同名再注册 = 替换);新名字自动启用,已有名字保持
// 原启用状态;getActiveTools / setActiveTools 在扩展加载期间不可用
const registry = new Map([["read", { name: "read" }], ["bash", { name: "bash" }]]);
let active = ["read", "bash"];
let loading = true;
const registered = [];          // 每次 registerTool 的记录(含同名重复注册)
const notInitialized = () => { throw new Error("Extension runtime not initialized"); };
const pi = {
  registerTool: (tool) => {
    if (!registry.has(tool.name)) active = [...active, tool.name];
    registry.set(tool.name, tool);
    registered.push(tool.name);
  },
  getActiveTools: () => (loading ? notInitialized() : [...active]),
  setActiveTools: (names) => (loading ? notInitialized() : (active = names.filter((n) => registry.has(n)))),
  on: (event, handler) => (handlers[event] ??= []).push(handler),
};
const handlers = {};
await mod.default(pi);
loading = false;
const tools = [...registry.values()].filter((t) => t.execute);   // 插件注册的工具

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
} else if (action === "rounds") {
  // rest[0]:JSON 列表 [{topology: 要复制成当前拓扑的文件 | null, corrupt: true 时把拓扑写坏}]
  result = [];
  for (const step of JSON.parse(rest[0])) {
    if (step.topology) copyFileSync(step.topology, process.env.A2A_TOPOLOGY);
    if (step.corrupt) writeFileSync(process.env.A2A_TOPOLOGY, "{{{ 不是合法的 yaml");
    const before = registered.length;
    let error = null;
    for (const handler of handlers.before_agent_start ?? []) {
      try {
        await handler({ prompt: "x" }, {});
      } catch (err) {
        error = String(err);
      }
    }
    const send = registry.get("a2a_send");
    result.push({ registered_again: registered.length - before, active: [...active], error,
                  enum: send ? send.parameters.properties.edge_id.enum : null });
  }
} else if (action === "is_herdr_write") {
  result = Object.fromEntries(JSON.parse(rest[0]).map((c) => [c, mod.isHerdrWrite(c)]));
}
process.stdout.write(JSON.stringify(result));
