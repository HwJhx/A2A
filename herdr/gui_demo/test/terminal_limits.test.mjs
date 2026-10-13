// 第 4 步 Codex 审核应修 1–3:终端数上限在并发握手下也成立;pty 启动失败时释放名额;后端退出时不留 tmux 客户端进程。
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createRequire } from "node:module";
import { after, test } from "node:test";
import { api, cleanupIds, login, PREFIX, ROOT, sh, startBackend, stopBackend, tempHome, waitExit } from "./helpers.mjs";

const require = createRequire(path.join(ROOT, "package.json"));
const { WebSocket } = require("ws");
const A2A_SRC = process.env.GUI_DEMO_A2A_SRC ?? "/Users/jhx/Documents/code/personal/pi_agent/A2A/herdr/a2a/src";
const FAKE = path.resolve(A2A_SRC, "../tests/fake_agent.py");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const backends = [];
const ids = [];
const dirs = [];

after(async () => {
  for (const b of backends) await stopBackend(b);
  cleanupIds(ids);
  for (const d of dirs) fs.rmSync(d, { recursive: true, force: true });
});

function launchers() {
  const dir = tempHome();
  const env = { fakeDir: dir };
  for (const role of ["dv", "sw"]) {
    const file = path.join(dir, `launch_${role}.sh`);
    fs.writeFileSync(file, `#!/bin/bash\nexec python3 ${FAKE} "${dir}/$A2A_ROLE""_""$A2A_IP"".log" work\n`, { mode: 0o755 });
    env[`GUI_DEMO_LAUNCHER_${role.toUpperCase()}`] = file;
  }
  return env;
}

/** 起后端、建工程、加 dv_uart;返回 { b, call, cookie, P }。 */
async function setup(env, home) {
  const { fakeDir, ...launch } = launchers();
  const b = await startBackend({ env: { ...launch, ...env }, ...(home ? { home } : {}) });
  backends.push(b);
  assert.ok(b.url, b.output());
  const { cookie } = await login(b);
  const call = (m, p, body) => api(b, cookie, m, p, body);
  const work = fs.mkdtempSync(path.join(os.homedir(), "gd-test-proj-"));
  dirs.push(work);
  const P = (await call("POST", "/api/projects", { name: `限额${Math.random()}`.slice(0, 20), path: work })).json;
  ids.push(P.id);
  assert.equal((await call("POST", `/api/projects/${P.id}/agents`, { role: "dv", ip: "uart" })).status, 201);
  return { b, call, cookie, P, fakeDir };
}

function connect(b, cookie, P) {
  const q = new URLSearchParams({ project: P.id, agent: "dv_uart" });
  const ws = new WebSocket(`ws://127.0.0.1:${b.port}/ws/term?${q}`, { headers: { Cookie: cookie, Origin: b.origin } });
  const c = { ws, status: null, opened: false, closeCode: null };
  return new Promise((resolve) => {
    ws.on("open", () => { c.opened = true; resolve(c); });
    ws.on("unexpected-response", (_q, res) => { c.status = res.statusCode; resolve(c); });
    ws.on("close", (code) => { c.closeCode = code; resolve(c); });
    ws.on("error", () => resolve(c));
  });
}

const attachProcs = (P) => sh("pgrep", ["-f", `attach-session -t =${PREFIX}win_${P.id}_`]).out.split("\n").filter(Boolean);

test("并发 8 个握手、上限 3:最多 3 个连上,其余 503;关掉后名额归还", async () => {
  const { b, cookie, P } = await setup({ GUI_DEMO_MAX_TERMINALS: "3" });
  const conns = await Promise.all(Array.from({ length: 8 }, () => connect(b, cookie, P)));
  const opened = conns.filter((c) => c.opened);
  assert.equal(opened.length, 3, `实际连上 ${opened.length} 个`);
  assert.ok(conns.filter((c) => !c.opened).every((c) => c.status === 503), JSON.stringify(conns.map((c) => c.status)));
  for (const c of opened) c.ws.close();
  await sleep(800);
  const again = await connect(b, cookie, P);
  assert.ok(again.opened, "关掉之后应能再连上");
  again.ws.close();
});

test("pty 启动失败(注入):连接以 1011 关闭,名额不泄漏(上限 2 下连续 5 次都能完成握手)", async () => {
  const { b, cookie, P } = await setup({ GUI_DEMO_MAX_TERMINALS: "2", GUI_DEMO_FAIL_AT: "pty" });
  for (let i = 0; i < 5; i++) {
    const c = await connect(b, cookie, P);
    if (c.opened) await new Promise((r) => (c.closeCode !== null ? r() : c.ws.on("close", (code) => { c.closeCode = code; r(); })));
    assert.equal(c.closeCode, 1011, `第 ${i + 1} 次:状态 ${c.status},关闭码 ${c.closeCode}`);
  }
  assert.deepEqual(attachProcs(P), []);
});

test("后端收到 SIGTERM:打开着的终端对应的 tmux 客户端进程都结束", async () => {
  const { b, cookie, P } = await setup({});
  const conns = [await connect(b, cookie, P), await connect(b, cookie, P)];
  assert.ok(conns.every((c) => c.opened));
  await sleep(500);
  assert.equal(attachProcs(P).length, 2, "应有 2 个 tmux 客户端进程");
  b.child.kill("SIGTERM");
  await waitExit(b.child, 20000);
  await sleep(500);
  assert.deepEqual(attachProcs(P), [], "后端退出后不应留下 tmux 客户端进程");
  // 清理:用同一数据目录再起一个后端删工程
  const again = await startBackend({ home: b.home });
  backends.push(again);
  const c2 = (await login(again)).cookie;
  assert.equal((await api(again, c2, "DELETE", `/api/projects/${P.id}`)).status, 200);
});

function capture(P, agent) {
  return sh("tmux", ["capture-pane", "-p", "-t", `=${PREFIX}win_${P.id}_${agent}:`]).out;
}

test("栅栏(状态查询间隔调成 60 秒,旧的可写结果一直不过期):GUI 停止后立刻发的输入不进 shell;重启后立刻重查、恢复可写", async () => {
  const { b, call, cookie, P, fakeDir } = await setup({ GUI_DEMO_GATE_POLL_MS: "60000" });
  const c = await connect(b, cookie, P);
  assert.ok(c.opened);
  assert.equal((await call("POST", `/api/projects/${P.id}/agents/dv_uart/stop`)).status, 200);
  c.ws.send(JSON.stringify({ t: "in", d: "echo FENCELEAK5" }));
  c.ws.send(JSON.stringify({ t: "in", d: "\r" }));
  await sleep(1500);
  const screen = capture(P, "dv_uart");
  // 从 pi 退出后出现的第一个 shell 提示符开始看(输入的命令若被执行,后面还会冒出新的提示符)
  const first = screen.search(/\S+@\S+:\S*\$/);
  assert.ok(first >= 0, `窗口里应有 pane 的 shell 提示符:\n${screen}`);
  const shellPart = screen.slice(first);
  assert.ok(!shellPart.includes("FENCELEAK5"), `停止后立刻发的输入不应进到 shell:\n${shellPart}`);
  assert.equal((await call("POST", `/api/projects/${P.id}/agents/dv_uart/restore`)).status, 200);
  // 查询间隔是 60 秒:能在几秒内恢复可写,只能是栅栏解除时立刻重查的结果
  let ok = false;
  for (let i = 0; i < 20 && !ok; i++) {
    c.ws.send(JSON.stringify({ t: "in", d: "KICK6" }));
    await sleep(300);
    try { ok = fs.readFileSync(path.join(fakeDir, "dv_uart.log"), "latin1").includes("KICK6"); } catch { ok = false; }
  }
  assert.ok(ok, "重启后应在几秒内恢复可写(不必等 60 秒)");
  c.ws.close();
});

test("握手与删除交错(握手在核对后停 1.5 秒,其间删除 IP):握手被拒,不留下重新建出来的窗口", async () => {
  const { b, call, cookie, P } = await setup({ GUI_DEMO_HANDSHAKE_DELAY_MS: "1500" });
  const pending = connect(b, cookie, P);
  await sleep(300); // 让握手先过第一次核对
  const del = await call("DELETE", `/api/projects/${P.id}/ips/uart`, { expect: ["dv_uart"] });
  assert.equal(del.status, 200, JSON.stringify(del.json));
  const c = await pending;
  assert.ok(!c.opened, "握手应被拒");
  assert.equal(c.status, 409);
  await sleep(500);
  assert.notEqual(sh("tmux", ["has-session", "-t", `=${PREFIX}win_${P.id}_dv_uart`]).code, 0, "不应留下重新建出来的窗口");
});

test("握手等待期间客户端断开(切页 / 断网):名额归还,之后仍能连上(上限 2,断开 3 次)", async () => {
  const net = await import("node:net");
  const { b, cookie, P } = await setup({ GUI_DEMO_MAX_TERMINALS: "2", GUI_DEMO_HANDSHAKE_DELAY_MS: "1000" });
  for (let i = 0; i < 3; i++) {
    const s = net.connect(b.port, "127.0.0.1", () => {
      s.write([`GET /ws/term?project=${P.id}&agent=dv_uart HTTP/1.1`, `Host: 127.0.0.1:${b.port}`, "Upgrade: websocket",
        "Connection: Upgrade", "Sec-WebSocket-Version: 13", "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
        `Origin: ${b.origin}`, `Cookie: ${cookie}`, "", ""].join("\r\n"));
    });
    s.on("error", () => undefined);
    await sleep(200);
    s.destroy(); // 握手还在服务器那边等待时断开
  }
  await sleep(2500); // 等服务器那边的握手都走完
  const c = await connect(b, cookie, P);
  assert.ok(c.opened, `断开过的握手应归还名额,实际状态 ${c.status}`);
  c.ws.close();
});

test("配置不合法(终端上限不是正整数):后端拒绝启动", async () => {
  for (const bad of ["abc", "0", "-1", "1.5"]) {
    const b = await startBackend({ env: { GUI_DEMO_MAX_TERMINALS: bad } });
    backends.push(b);
    assert.equal(b.url, null, `GUI_DEMO_MAX_TERMINALS=${bad} 不应启动`);
    assert.notEqual(await waitExit(b.child), 0);
    assert.match(b.output(), /GUI_DEMO_MAX_TERMINALS/);
  }
});

test("关服期间待完成的握手醒过来(握手停 1 秒、关服停 3 秒,确认握手已在等待再发 SIGTERM):不升级、不启动 tmux 客户端", async () => {
  const net = await import("node:net");
  const { b, cookie, P } = await setup({ GUI_DEMO_HANDSHAKE_DELAY_MS: "1000", GUI_DEMO_SHUTDOWN_DELAY_MS: "3000" });
  const s = net.connect(b.port, "127.0.0.1", () => {
    s.write([`GET /ws/term?project=${P.id}&agent=dv_uart HTTP/1.1`, `Host: 127.0.0.1:${b.port}`, "Upgrade: websocket",
      "Connection: Upgrade", "Sec-WebSocket-Version: 13", "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
      `Origin: ${b.origin}`, `Cookie: ${cookie}`, "", ""].join("\r\n"));
  });
  let reply = "";
  s.on("data", (d) => { reply += d; });
  s.on("error", () => undefined);
  // 屏障:等后端打印"握手进入等待",确认握手已在等待阶段,再发信号
  for (let i = 0; i < 50 && !b.output().includes("测试:握手进入等待 dv_uart"); i++) await sleep(100);
  assert.ok(b.output().includes("测试:握手进入等待 dv_uart"), b.output());
  const seen = new Set();
  const watch = setInterval(() => { for (const x of attachProcs(P)) seen.add(x); }, 100);
  b.child.kill("SIGTERM");
  await waitExit(b.child, 30000); // 关服要停 3 秒:握手 1 秒后醒来时进程还在
  clearInterval(watch);
  await sleep(500);
  assert.deepEqual([...seen], [], "关服期间不应启动 tmux 客户端");
  assert.doesNotMatch(reply, / 101 /, `关服期间不应升级成功:${reply.split("\r\n")[0]}`);
  assert.deepEqual(attachProcs(P), []);
  s.destroy();
  const again = await startBackend({ home: b.home });
  backends.push(again);
  const c2 = (await login(again)).cookie;
  assert.equal((await api(again, c2, "DELETE", `/api/projects/${P.id}`)).status, 200);
});

test("整数配置的边界:端口、查询间隔、握手延时、关服停顿不合法时拒绝启动,合法边界值能启动", async () => {
  const bad = { GUI_DEMO_PORT: ["0", "65536", "80a"], GUI_DEMO_GATE_POLL_MS: ["199", "600001"],
    GUI_DEMO_HANDSHAKE_DELAY_MS: ["-5", "60001"], GUI_DEMO_SHUTDOWN_DELAY_MS: ["x"] };
  for (const [name, values] of Object.entries(bad)) {
    for (const v of values) {
      const b = await startBackend({ env: { [name]: v } });
      backends.push(b);
      assert.equal(b.url, null, `${name}=${v} 不应启动`);
      assert.match(b.output(), new RegExp(name));
    }
  }
  const ok = await startBackend({ env: { GUI_DEMO_GATE_POLL_MS: "200", GUI_DEMO_HANDSHAKE_DELAY_MS: "0", GUI_DEMO_MAX_TERMINALS: "1" } });
  backends.push(ok);
  assert.ok(ok.url, ok.output());
});
