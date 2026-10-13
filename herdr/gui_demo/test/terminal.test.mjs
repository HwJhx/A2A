// 第 4 步:终端桥(窗口 tmux + node-pty + WebSocket)。假 agent(不调模型),真实 herdr 与 tmux。
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { after, before, test } from "node:test";
import { createRequire } from "node:module";
import { api, cleanupIds, login, PREFIX, ROOT, sh, startBackend, stopBackend, tempHome, wsHandshake } from "./helpers.mjs";

const require = createRequire(path.join(ROOT, "package.json"));
const { WebSocket } = require("ws");
const A2A_SRC = process.env.GUI_DEMO_A2A_SRC ?? "/Users/jhx/Documents/code/personal/pi_agent/A2A/herdr/a2a/src";
const FAKE = path.resolve(A2A_SRC, "../tests/fake_agent.py");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ids = [];
const dirs = [];
let b, call, cookie, P, fakeDir;

function launcher(dir, role) {
  const file = path.join(dir, `launch_${role}.sh`);
  fs.writeFileSync(file, `#!/bin/bash\nexport FAKE_ROLE=${role}\nexec python3 ${FAKE} "${dir}/$A2A_ROLE""_""$A2A_IP"".log" work\n`, { mode: 0o755 });
  return file;
}

before(async () => {
  fakeDir = tempHome();
  b = await startBackend({ env: { GUI_DEMO_LAUNCHER_DV: launcher(fakeDir, "dv"), GUI_DEMO_LAUNCHER_SW: launcher(fakeDir, "sw") } });
  assert.ok(b.url, b.output());
  cookie = (await login(b)).cookie;
  call = (m, p, body) => api(b, cookie, m, p, body);
  const work = fs.mkdtempSync(path.join(os.homedir(), "gd-test-proj-"));
  dirs.push(work);
  P = (await call("POST", "/api/projects", { name: "终端测试", path: work })).json;
  ids.push(P.id);
  for (const role of ["dv", "sw"]) {
    const r = await call("POST", `/api/projects/${P.id}/agents`, { role, ip: "uart" });
    assert.equal(r.status, 201, JSON.stringify(r.json));
    assert.deepEqual(r.json.warnings, []);
  }
});

after(async () => {
  if (P?.id) await call("DELETE", `/api/projects/${P.id}`).catch(() => undefined);
  await stopBackend(b);
  cleanupIds(ids);
  for (const d of dirs) fs.rmSync(d, { recursive: true, force: true });
});

/** 打开终端连接;收到的输出累积在 conn.out。 */
function connect(agent, extra = {}) {
  const q = new URLSearchParams({ project: P.id, agent, cols: "100", rows: "30", ...extra });
  const ws = new WebSocket(`ws://127.0.0.1:${b.port}/ws/term?${q}`, { headers: { Cookie: cookie, Origin: b.origin } });
  const conn = { ws, out: "", closed: false, status: null };
  ws.on("message", (d) => { conn.out += String(d); });
  ws.on("close", () => { conn.closed = true; });
  ws.on("unexpected-response", (_req, res) => { conn.status = res.statusCode; conn.closed = true; });
  ws.on("error", () => { conn.closed = true; });
  return new Promise((resolve) => {
    ws.on("open", () => resolve(conn));
    ws.on("close", () => resolve(conn));
    ws.on("unexpected-response", () => resolve(conn));
  });
}
const send = (conn, obj) => conn.ws.send(JSON.stringify(obj));
const fakeLog = (agent) => { try { return fs.readFileSync(path.join(fakeDir, `${agent}.log`), "latin1"); } catch { return ""; } };
const windowName = (agent) => `${PREFIX}win_${P.id}_${agent}`;
const capture = (agent) => sh("tmux", ["capture-pane", "-p", "-t", `=${windowName(agent)}:`]).out;
async function until(fn, ms = 10000) {
  for (let t = 0; t < ms; t += 200) { if (await fn()) return true; await sleep(200); }
  return false;
}

test("新增 agent 后窗口 tmux 已建好,选项正确(状态栏关、尺寸跟随最近的客户端)", async () => {
  for (const agent of ["dv_uart", "sw_uart"]) {
    assert.equal(sh("tmux", ["has-session", "-t", `=${windowName(agent)}`]).code, 0);
    assert.match(sh("tmux", ["show-options", "-t", `=${windowName(agent)}:`, "status"]).out, /status off/);
    assert.match(sh("tmux", ["show-options", "-w", "-t", `=${windowName(agent)}:`, "window-size"]).out, /latest/);
  }
});

test("握手校验:参数不对 400、agent 或工程不存在 404、没登录 401", async () => {
  assert.equal((await connect("DV_UART")).status, 400);
  assert.equal((await connect("dv_nope")).status, 404);
  const q = new URLSearchParams({ project: "p00000000", agent: "dv_uart" });
  const other = new WebSocket(`ws://127.0.0.1:${b.port}/ws/term?${q}`, { headers: { Cookie: cookie, Origin: b.origin } });
  const status = await new Promise((r) => other.on("unexpected-response", (_q, res) => r(res.statusCode)));
  assert.equal(status, 404);
  assert.match(await wsHandshake(b, { Origin: b.origin }), / 401 /);
});

test("输入送到对应的 agent、不串到别的;改尺寸后 agent 终端跟着变", async () => {
  const dv = await connect("dv_uart");
  assert.equal(dv.ws.readyState, WebSocket.OPEN);
  assert.ok(await until(() => dv.out.length > 0), "应收到终端输出");
  send(dv, { t: "in", d: "TERMDV1" });
  assert.ok(await until(() => fakeLog("dv_uart").includes("TERMDV1")), "dv_uart 应收到输入");
  assert.ok(!fakeLog("sw_uart").includes("TERMDV1"), "不应串到 sw_uart");

  send(dv, { t: "size", c: 111, r: 33 });
  const pid = fs.readFileSync(path.join(fakeDir, "dv_uart.log.pid"), "utf8").trim();
  const tty = fs.readlinkSync(`/proc/${pid}/fd/0`);
  assert.ok(await until(() => sh("stty", ["-F", tty, "size"]).out.trim() === "33 111"), `agent 终端尺寸应变为 33 111,实际 ${sh("stty", ["-F", tty, "size"]).out}`);
  dv.ws.close();
});

test("两个连接同时打开同一个 agent:都能收到输出,都不被挤掉", async () => {
  const a = await connect("sw_uart");
  const c = await connect("sw_uart");
  assert.equal(a.ws.readyState, WebSocket.OPEN);
  assert.equal(c.ws.readyState, WebSocket.OPEN);
  a.out = ""; c.out = "";
  send(a, { t: "in", d: "SHARED1" });
  assert.ok(await until(() => fakeLog("sw_uart").includes("SHARED1")));
  await sleep(1000);
  assert.ok(!a.closed && !c.closed, "两个连接都应还在");
  a.ws.close(); c.ws.close();
});

test("agent 停止后后端丢弃输入(只读);重启后同一个连接自动接回、输入恢复", async () => {
  const dv = await connect("dv_uart");
  assert.equal((await call("POST", `/api/projects/${P.id}/agents/dv_uart/stop`)).status, 200);
  await sleep(2600); // 输入闸门每 2 秒刷新
  send(dv, { t: "in", d: "echo GATELEAK7" });
  send(dv, { t: "in", d: "\r" });
  await sleep(1500);
  assert.ok(!capture("dv_uart").includes("GATELEAK7"), "agent 停止时输入不应进到窗口(pane 的 shell)");
  assert.ok(!dv.closed, "停止期间连接应保持");

  assert.equal((await call("POST", `/api/projects/${P.id}/agents/dv_uart/restore`)).status, 200);
  await sleep(2600);
  send(dv, { t: "in", d: "AFTERRESTORE9" });
  assert.ok(await until(() => fakeLog("dv_uart").includes("AFTERRESTORE9"), 15000), "重启后输入应送到新的 agent");
  assert.ok(!dv.closed, "同一个连接应一直在");
  dv.ws.close();
});

test("GUI 停止 agent:请求一返回就发的输入也被丢弃(不进 pane 的 shell);停止期间的输入同样丢弃", async () => {
  const dv = await connect("dv_uart");
  await sleep(2600); // 先让闸门进入"可写"状态,确认下面的拒绝来自栅栏而不是本来就不可写
  send(dv, { t: "in", d: "PRESTOP1" });
  assert.ok(await until(() => fakeLog("dv_uart").includes("PRESTOP1")), "停止前应可写");
  const stopping = call("POST", `/api/projects/${P.id}/agents/dv_uart/stop`);
  let during = 0;
  let done = false;
  stopping.then(() => { done = true; });
  while (!done) { send(dv, { t: "in", d: "Q7Q" }); during += 1; await sleep(30); }
  assert.equal((await stopping).status, 200);
  send(dv, { t: "in", d: "echo GATELEAK8" }); // 停止请求刚返回,没有任何等待
  send(dv, { t: "in", d: "\r" });
  await sleep(1500);
  // 停止请求发出后、服务器设好栅栏前到达的输入会送给 agent(合法),agent 退出后它的旧画面还留在窗口里;
  // 所以只看 pane 的 shell:最后一个提示符那一行之后不能出现任何输入
  const screen = capture("dv_uart");
  // 从 pi 退出后出现的第一个 shell 提示符开始看(输入的命令若被执行,后面还会冒出新的提示符)
  const first = screen.search(/\S+@\S+:\S*\$/);
  assert.ok(first >= 0, `窗口里应有 pane 的 shell 提示符:\n${screen}`);
  const shellPart = screen.slice(first);
  assert.ok(!shellPart.includes("GATELEAK8"), `停止返回后立刻发的输入不应进到 shell:\n${shellPart}`);
  assert.ok(!shellPart.includes("Q7Q"), `停止过程中的输入不应进到 shell:\n${shellPart}`);
  assert.ok(during > 0);
  assert.equal((await call("POST", `/api/projects/${P.id}/agents/dv_uart/restore`)).status, 200);
  // 重启后栅栏解除会立刻触发一次查询,不必等满一个周期
  let ok = false;
  for (let i = 0; i < 20 && !ok; i++) {
    send(dv, { t: "in", d: "RESUME4" });
    ok = await until(() => fakeLog("dv_uart").includes("RESUME4"), 300);
  }
  assert.ok(ok, "重启后应恢复可写");
  dv.ws.close();
});

test("删除 IP 进行中不断发起终端握手:删除后不会留下被重新建出来的窗口,连接都已关闭", async () => {
  assert.equal((await call("POST", `/api/projects/${P.id}/agents`, { role: "dv", ip: "race" })).status, 201);
  const conns = [];
  const deleting = call("DELETE", `/api/projects/${P.id}/ips/race`, { expect: ["dv_race"] });
  let done = false;
  deleting.then(() => { done = true; });
  while (!done) { conns.push(connect("dv_race")); await sleep(80); }
  assert.equal((await deleting).status, 200);
  const results = await Promise.all(conns);
  await sleep(1500);
  assert.notEqual(sh("tmux", ["has-session", "-t", `=${windowName("dv_race")}`]).code, 0, "删除后不应再有 dv_race 的窗口");
  assert.ok(results.every((c) => c.closed), "所有连接都应已关闭");
  assert.ok(results.some((c) => c.status === 409 || c.status === 404), `删除过程中的握手应被拒,实际 ${results.map((c) => c.status)}`);
});

test("删除 IP:窗口 tmux 关掉,打开着的终端连接随之关闭", async () => {
  const sw = await connect("sw_uart");
  assert.equal(sw.ws.readyState, WebSocket.OPEN);
  const del = await call("DELETE", `/api/projects/${P.id}/ips/uart`, { expect: ["dv_uart", "sw_uart"] });
  assert.equal(del.status, 200, JSON.stringify(del.json));
  assert.deepEqual(del.json.warnings, []);
  assert.ok(await until(() => sw.closed), "连接应关闭");
  assert.notEqual(sh("tmux", ["has-session", "-t", `=${windowName("sw_uart")}`]).code, 0);
  assert.notEqual(sh("tmux", ["has-session", "-t", `=${windowName("dv_uart")}`]).code, 0);
});
