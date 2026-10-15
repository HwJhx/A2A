// /a2a 列边的显示格式:每条边一段,"边 → 目标"一行,消息另起缩进,段间空一行。运行:node --test tests/js/format.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { pathToFileURL, fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const { formatEdges } = await import(pathToFileURL(join(here, "..", "..", "pi-extension", "a2a.ts")).href);

test("单条边:边名一行,消息另起一行缩进 4 格,不再用「」", () => {
  assert.equal(formatEdges([{ edge_id: "dv_done", dst: "sw_spi", text: "spi ip 完成验证" }]),
    "  dv_done → sw_spi\n    spi ip 完成验证");
});

test("多条边之间空一行", () => {
  const out = formatEdges([
    { edge_id: "a", dst: "x", text: "甲" },
    { edge_id: "b", dst: "y", text: "乙" },
  ]);
  assert.equal(out, "  a → x\n    甲\n\n  b → y\n    乙");
});

test("模板自带的换行(\\n、\\r\\n、\\r)逐行缩进", () => {
  assert.equal(formatEdges([{ edge_id: "e", dst: "d", text: "第一行\n第二行\r\n第三行\r第四行" }]),
    "  e → d\n    第一行\n    第二行\n    第三行\n    第四行");
});

test("空列表输出空串", () => {
  assert.equal(formatEdges([]), "");
});
