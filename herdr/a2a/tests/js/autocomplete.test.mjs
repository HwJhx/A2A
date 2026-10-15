// 22 号方案 §15:/a2a 补全项的来源标签改写。运行:node --test tests/js/autocomplete.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const m = await import(pathToFileURL(join(here, "..", "..", "pi-extension", "a2a.ts")).href);
const DESC = m.COMMAND_DESCRIPTION;
const TAG = "[u:git:github.com/HwJhx/A2A@a2a-plugin-v0.2.0]";

const result = (...items) => ({ prefix: "/a", items });

test("只改写本插件的 /a2a 项:来源标签换成版本号", () => {
  const out = m.rewriteSuggestions(result(
    { value: "a2a", label: "a2a", description: `${TAG} ${DESC}` },
    { value: "agent", label: "agent", description: "[u] 别的扩展" },
    { value: "about", label: "about", description: "Show info" },
  ), "/a", "0.2.1");
  assert.equal(out.items[0].description, `[v0.2.1] ${DESC}`);
  assert.equal(out.items[0].value, "a2a");
  assert.equal(out.items[1].description, "[u] 别的扩展");
  assert.equal(out.items[2].description, "Show info");
  assert.equal(out.prefix, "/a");
});

test("各种来源标签都能换掉(本地 [u] / 项目 [p] / npm)", () => {
  for (const tag of ["[u]", "[p]", "[t]", "[u:npm:a2a-pi-plugin]", ""]) {
    const desc = tag ? `${tag} ${DESC}` : DESC;
    const out = m.rewriteSuggestions(result({ value: "a2a", label: "a2a", description: desc }), "/a2", "1.0.0");
    assert.equal(out.items[0].description, `[v1.0.0] ${DESC}`, tag);
  }
});

test("不动:别的扩展注册的同名命令、重复安装的 a2a:1、参数补全阶段、非斜杠输入、空结果", () => {
  const other = result({ value: "a2a", label: "a2a", description: "[u] 别人的 a2a 命令" });
  assert.equal(m.rewriteSuggestions(other, "/a", "0.2.1"), other);
  const dup = result({ value: "a2a:1", label: "a2a:1", description: `${TAG} ${DESC}` });
  assert.equal(m.rewriteSuggestions(dup, "/a", "0.2.1"), dup);
  const args = { prefix: "d", items: [{ value: "dv_done", label: "dv_done", description: `${TAG} ${DESC}` }] };
  assert.equal(m.rewriteSuggestions(args, "/a2a d", "0.2.1"), args);
  const mine = result({ value: "a2a", label: "a2a", description: `${TAG} ${DESC}` });
  assert.equal(m.rewriteSuggestions(mine, "@a", "0.2.1"), mine);
  assert.equal(m.rewriteSuggestions(null, "/a", "0.2.1"), null);
});

test("改写是幂等的(同一会话登记两次包装)", () => {
  const once = m.rewriteSuggestions(result({ value: "a2a", label: "a2a", description: `${TAG} ${DESC}` }), "/a", "0.2.1");
  const twice = m.rewriteSuggestions(once, "/a", "0.2.1");
  assert.deepEqual(twice, once);
});

test("包装继承原 provider:其他方法、字段与 this 照常可用", async () => {
  class Base {
    constructor() { this.commands = ["a2a"]; this.triggerCharacters = ["#"]; }
    async getSuggestions(lines, line, col) {
      return result({ value: "a2a", label: "a2a", description: `${TAG} ${DESC}` },
                    { value: "x", label: lines[line].slice(0, col), description: String(this.commands.length) });
    }
    applyCompletion(lines, line, col, item, prefix) { return { applied: item.value, prefix, n: this.commands.length }; }
    shouldTriggerFileCompletion() { return false; }
  }
  const base = new Base();
  const w = m.makeAutocompleteWrapper("0.2.1")(base);
  const out = await w.getSuggestions(["/a2"], 0, 3, {});
  assert.equal(out.items[0].description, `[v0.2.1] ${DESC}`);
  assert.deepEqual(out.items[1], { value: "x", label: "/a2", description: "1" });
  assert.deepEqual(w.applyCompletion([""], 0, 0, { value: "a2a" }, "/a"), { applied: "a2a", prefix: "/a", n: 1 });
  assert.equal(w.shouldTriggerFileCompletion(), false);
  assert.deepEqual(w.triggerCharacters, ["#"]);
  assert.equal((await base.getSuggestions(["/a2"], 0, 3)).items[0].description, `${TAG} ${DESC}`, "不改原对象");
});

test("版本号:读仓库根 package.json;名字不对或读不到时为 null(不改写)", () => {
  assert.match(m.pluginVersion(), /^\d+\.\d+\.\d+$/);
  const d = mkdtempSync(join(tmpdir(), "a2a-ver-"));
  writeFileSync(join(d, "ok.json"), JSON.stringify({ name: "a2a-pi-plugin", version: "9.9.9" }));
  writeFileSync(join(d, "other.json"), JSON.stringify({ name: "something", version: "1.0.0" }));
  writeFileSync(join(d, "bad.json"), "{");
  assert.equal(m.pluginVersion(join(d, "ok.json")), "9.9.9");
  assert.equal(m.pluginVersion(join(d, "other.json")), null);
  assert.equal(m.pluginVersion(join(d, "bad.json")), null);
  assert.equal(m.pluginVersion(join(d, "missing.json")), null);
});
