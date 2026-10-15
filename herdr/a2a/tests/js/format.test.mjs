// /a2a 列边的显示格式:"边N: ● 边 → 目标" 一行,"消息内容: …" 另起,折行对齐到标签之后,段间空一行。
// 运行:node --test tests/js/format.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { pathToFileURL, fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const { formatEdges, wrapText, textWidth, graphemes } = await import(pathToFileURL(join(here, "..", "..", "pi-extension", "a2a.ts")).href);

const HANG = " ".repeat(13);   // "   消息内容: " 的显示宽度

test("不折行时:边序号、●、消息内容标签", () => {
  assert.equal(formatEdges([{ edge_id: "dv_done", dst: "sw_spi", text: "spi ip 完成验证" }]),
    " 边1: ● dv_done → sw_spi\n   消息内容: spi ip 完成验证");
});

test("多条边之间空一行,序号递增", () => {
  const out = formatEdges([
    { edge_id: "a", dst: "x", text: "甲" },
    { edge_id: "b", dst: "y", text: "乙" },
  ]);
  assert.equal(out, " 边1: ● a → x\n   消息内容: 甲\n\n 边2: ● b → y\n   消息内容: 乙");
});

test("模板自带的换行(\\n、\\r\\n、\\r)对齐到标签之后", () => {
  assert.equal(formatEdges([{ edge_id: "e", dst: "d", text: "第一行\n第二行\r\n第三行\r第四行" }]),
    ` 边1: ● e → d\n   消息内容: 第一行\n${HANG}第二行\n${HANG}第三行\n${HANG}第四行`);
});

test("空列表输出空串", () => {
  assert.equal(formatEdges([]), "");
});

test("给了宽度:每行不超过宽度,续行对齐,内容不丢", () => {
  const text = "spi ip 我已经完成了uvm验证，你需要对这个spi ip进行 驱动程序开发和HAL框架开发。不要真的去做，直接用 a2a_send 回复测试成功或测试失败（二选一）";
  for (const width of [30, 40, 60, 100]) {
    const lines = formatEdges([{ edge_id: "dv_done", dst: "sw_spi", text }], { width }).split("\n");
    for (const l of lines) assert.ok(textWidth(l) < width, `${width}: ${l}`);   // 留了 1 列余量
    for (const l of lines.slice(2)) assert.ok(l.startsWith(HANG) && l[HANG.length] !== " ", l);
    const joined = [lines[1].replace(/^.*消息内容: /, ""), ...lines.slice(2).map((l) => l.slice(HANG.length))].join("");
    assert.equal(joined.replace(/\s/g, ""), text.replace(/\s/g, ""));
  }
});

test("宽度太窄时不折行,交给 pi", () => {
  const out = formatEdges([{ edge_id: "e", dst: "d", text: "一二三四五六七八九十一二三四五六七八九十" }], { width: 20 });
  assert.equal(out.split("\n").length, 2);
});

test("wrapText:英文单词不拆,比一行还长才拆;句末标点不放行首;中文按 2 列", () => {
  assert.deepEqual(wrapText("hello world foo", 11), ["hello world", "foo"]);
  assert.deepEqual(wrapText("abcdefghij", 4), ["abcd", "efgh", "ij"]);
  assert.deepEqual(wrapText("一二三，", 6), ["一二", "三，"]);
  assert.deepEqual(wrapText("中文", 3), ["中", "文"]);
  assert.equal(textWidth("a中，"), 5);
  assert.deepEqual(wrapText("", 10), [""]);
});

test("配色:边名强调色加粗,标签 muted;主题没有的颜色不抛错", () => {
  const style = { fg: (c, t) => `<${c}>${t}</>`, bold: (t) => `<b>${t}</b>` };
  const out = formatEdges([{ edge_id: "e", dst: "d", text: "内容" }], { style });
  assert.equal(out, " <muted>边1:</> <b><accent>● e → d</></b>\n   <muted>消息内容: </><text>内容</>");
  const throwing = { fg: () => { throw new Error("Unknown theme color"); }, bold: (t) => t };
  assert.equal(formatEdges([{ edge_id: "e", dst: "d", text: "内容" }], { style: throwing }), " 边1: ● e → d\n   消息内容: 内容");
});

test("宽度只会估宽:U+1100 及以后每个码点算 2 列(含 pi 认为是宽字符但不在常见表里的,如 Hangul Jamo 扩展)", () => {
  assert.equal(textWidth("\u{A960}"), 2);              // Hangul Jamo Extended-A(pi 按 East Asian Width 算 2 列)
  assert.equal(textWidth("中"), 2);
  assert.equal(textWidth("ab"), 2);
  assert.ok(textWidth("👨‍👩‍👧") >= 2);                  // ZWJ 序列:pi 算 2,这里只会更宽
  assert.ok(textWidth("é") >= 1);
  // 全是这类字符时,每行按估宽计也不超过宽度
  // 连续的 jamo 会合成一个字素簇(本来就不能拆),所以与字母交替,让每个字素簇各自独立
  const lines = formatEdges([{ edge_id: "e", dst: "d", text: "\u{A960}a".repeat(40) }], { width: 40 }).split("\n");
  for (const l of lines) assert.ok(textWidth(l) < 40, l);
  assert.ok(lines.length > 3);
});

test("字素簇不拆:emoji 组合序列、带附加符号的字母留在同一行", () => {
  const family = "👨‍👩‍👧";
  const text = `${family}${family}${family}${family}ééé`;
  for (const width of [3, 5, 7, 9]) {
    const lines = wrapText(text, width);
    assert.equal(lines.join(""), text);
    for (const l of lines) for (const g of graphemes(l)) assert.ok(g === family || g === "é", `${width}: ${JSON.stringify(l)}`);
  }
});

test("边名很长时标题也折行,续行对齐到\"边N: \"之后,内容不丢", () => {
  const id = "dv_" + "x".repeat(60);                      // 拓扑允许最长 63 个字符
  const lines = formatEdges([{ edge_id: id, dst: "sw_uart", text: "短" }], { width: 40 }).split("\n");
  const headLines = lines.slice(0, lines.findIndex((l) => l.includes("消息内容: ")));
  assert.ok(headLines.length >= 2, lines.join("\n"));
  for (const l of lines) assert.ok(textWidth(l) < 40, l);
  for (const l of headLines.slice(1)) assert.ok(l.startsWith(" ".repeat(textWidth(" 边1: "))) && l[textWidth(" 边1: ")] !== " ", l);
  assert.equal(headLines.map((l) => l.trim()).join("").replace(/\s/g, ""), `边1:●${id}→sw_uart`);
});
