import { pathToFileURL } from "node:url";
import { unlinkSync } from "node:fs";

const [plugin, action, ...args] = process.argv.slice(2);
const mod = await import(pathToFileURL(plugin).href);
const tools = [];
const handlers = {};
const pi = {
  registerTool: (tool) => tools.push(tool),
  on: (name, handler) => (handlers[name] ??= []).push(handler),
};
await mod.default(pi);

let result;
if (action === "describe") {
  result = { tools: tools.map(({ name, description, parameters }) => ({ name, description, parameters })),
             events: Object.keys(handlers) };
} else if (action === "send") {
  // Exercise execFile's spawn-error path only after the extension has loaded edges.
  if (process.env.A2A_TEST_REMOVE_PYTHON_BEFORE_SEND === "1") {
    unlinkSync(process.env.A2A_PYTHON);
  }
  try {
    const value = await tools[0].execute("test-call", { edge_id: args[0] }, undefined, () => {}, {});
    result = { ok: true, content: value.content, details: value.details };
  } catch (error) {
    result = { ok: false, error: error.message };
  }
} else if (action === "guard") {
  result = await handlers.tool_call[0]({ toolName: "bash", input: { command: args[0] } });
}
process.stdout.write(JSON.stringify(result));
