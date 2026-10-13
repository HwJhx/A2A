// 第 5 步:herdr 会话停掉(模拟 VM / herdr 重启)后,"恢复"把会话、agent、broker 拉回来,终端自动接上。假 agent,不调模型。
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createRequire } from "node:module";
import { after, test } from "node:test";
import { api, cleanupIds, login, PREFIX, ROOT, sh, startBackend, stopBackend, tempHome } from "./helpers.mjs";

const PREFIX_HOST = `${PREFIX}host_`;

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

test("herdr 会话停掉后:列表显示未运行、不能新增 agent;恢复后 agent 回来、broker 换新、原终端连接自动接上可写", async () => {
  const fakeDir = tempHome();
  const env = {};
  for (const role of ["dv", "sw"]) {
    const file = path.join(fakeDir, `launch_${role}.sh`);
    fs.writeFileSync(file, `#!/bin/bash\nexec python3 ${FAKE} "${fakeDir}/$A2A_ROLE""_""$A2A_IP"".log" work\n`, { mode: 0o755 });
    env[`GUI_DEMO_LAUNCHER_${role.toUpperCase()}`] = file;
  }
  const b = await startBackend({ env });
  backends.push(b);
  const { cookie } = await login(b);
  const call = (m, p, body) => api(b, cookie, m, p, body);
  const work = fs.mkdtempSync(path.join(os.homedir(), "gd-test-proj-"));
  dirs.push(work);
  const P = (await call("POST", "/api/projects", { name: "恢复测试", path: work })).json;
  ids.push(P.id);
  assert.equal((await call("POST", `/api/projects/${P.id}/agents`, { role: "dv", ip: "uart" })).status, 201);
  assert.equal((await call("POST", `/api/projects/${P.id}/recover`)).status, 409, "会话在运行时不需要恢复");

  const q = new URLSearchParams({ project: P.id, agent: "dv_uart" });
  const ws = new WebSocket(`ws://127.0.0.1:${b.port}/ws/term?${q}`, { headers: { Cookie: cookie, Origin: b.origin } });
  let closed = false;
  ws.on("close", () => { closed = true; });
  await new Promise((r) => ws.on("open", r));
  const oldBroker = (await call("GET", "/api/projects")).json[0].broker.pid;

  // 模拟 VM / herdr 重启:停掉工程的 herdr 会话(agent 随之退出)
  assert.equal(sh("herdr", ["session", "stop", P.session]).code, 0);
  let list;
  for (let i = 0; i < 50; i++) {
    list = (await call("GET", "/api/projects")).json;
    if (!list[0].herdr) break;
    await sleep(200);
  }
  assert.equal(list[0].herdr, false);
  assert.equal((await call("POST", `/api/projects/${P.id}/agents`, { role: "sw", ip: "uart" })).status, 409, "会话没在运行时不能新增");

  const rec = await call("POST", `/api/projects/${P.id}/recover`);
  assert.equal(rec.status, 200, JSON.stringify(rec.json));
  assert.match(JSON.stringify(rec.json.result), /dv_uart/);
  list = (await call("GET", "/api/projects")).json;
  assert.equal(list[0].herdr, true);
  assert.equal(list[0].broker.running, true);
  assert.notEqual(list[0].broker.pid, oldBroker);
  const agents = (await call("GET", `/api/projects/${P.id}/agents`)).json.agents;
  assert.equal(agents[0].lifecycle, "running");
  assert.equal(agents[0].live, "idle");

  // 原来那条终端连接:窗口里的 attach 循环会接上恢复后的 agent;闸门下一次查询后恢复可写
  assert.ok(!closed, "恢复过程中终端连接不应断开");
  let ok = false;
  for (let i = 0; i < 40 && !ok; i++) {
    ws.send(JSON.stringify({ t: "in", d: "RECOVERED5" }));
    await sleep(400);
    try { ok = fs.readFileSync(path.join(fakeDir, "dv_uart.log"), "latin1").includes("RECOVERED5"); } catch { ok = false; }
  }
  assert.ok(ok, "恢复后原终端连接应能把输入送到新的 agent");
  ws.close();
  assert.equal((await call("DELETE", `/api/projects/${P.id}`)).status, 200);
});

async function backendWithFakes(extraEnv = {}, home) {
  const fakeDir = tempHome();
  const env = { ...extraEnv };
  for (const role of ["dv", "sw"]) {
    const file = path.join(fakeDir, `launch_${role}.sh`);
    fs.writeFileSync(file, `#!/bin/bash\nexec python3 ${FAKE} "${fakeDir}/$A2A_ROLE""_""$A2A_IP"".log" work\n`, { mode: 0o755 });
    env[`GUI_DEMO_LAUNCHER_${role.toUpperCase()}`] = file;
  }
  const b = await startBackend({ env, ...(home ? { home } : {}) });
  backends.push(b);
  assert.ok(b.url, b.output());
  const { cookie } = await login(b);
  return { b, env, call: (m, p, body) => api(b, cookie, m, p, body) };
}

async function waitHerdrDown(call, id) {
  for (let i = 0; i < 50; i++) {
    const p = (await call("GET", "/api/projects")).json.find((x) => x.id === id);
    if (!p.herdr) return p;
    await sleep(200);
  }
  throw new Error("herdr 会话没有停下");
}

const brokerPids = (session) => sh("pgrep", ["-f", `a2a.cli broker run --session ${session}$`]).out.split("\n").filter(Boolean).map(Number);

test("恢复中途失败(注入一次):herdr 已起、broker 没起 → 列表显示未就绪,可以再点恢复,第二次成功", async () => {
  const { call } = await backendWithFakes({ GUI_DEMO_FAIL_ONCE: "recover" });
  const work = fs.mkdtempSync(path.join(os.homedir(), "gd-test-proj-"));
  dirs.push(work);
  const P = (await call("POST", "/api/projects", { name: "恢复重试", path: work })).json;
  ids.push(P.id);
  assert.equal((await call("POST", `/api/projects/${P.id}/agents`, { role: "dv", ip: "uart" })).status, 201);
  assert.equal(sh("herdr", ["session", "stop", P.session]).code, 0);
  await waitHerdrDown(call, P.id);
  const first = await call("POST", `/api/projects/${P.id}/recover`);
  assert.equal(first.status, 500, JSON.stringify(first.json));
  const mid = (await call("GET", "/api/projects")).json.find((x) => x.id === P.id);
  assert.equal(mid.herdr, true, "herdr 会话已重新起来");
  assert.equal(mid.broker.running, false, "broker 没起:页面上应仍显示恢复按钮");
  const second = await call("POST", `/api/projects/${P.id}/recover`);
  assert.equal(second.status, 200, JSON.stringify(second.json));
  const done = (await call("GET", "/api/projects")).json.find((x) => x.id === P.id);
  assert.equal(done.broker.running, true);
  const agents = (await call("GET", `/api/projects/${P.id}/agents`)).json.agents;
  assert.equal(agents[0].lifecycle, "running");
  assert.equal(agents[0].live, "idle");
  assert.equal((await call("POST", `/api/projects/${P.id}/recover`)).status, 409, "都在运行时不需要恢复");
  assert.equal((await call("DELETE", `/api/projects/${P.id}`)).status, 200);
});

test("后端被 kill -9 留下孤儿 broker、随后 herdr 会话也停了:新后端上恢复会先停掉孤儿 broker,再恢复成功", async () => {
  const first = await backendWithFakes();
  const work = fs.mkdtempSync(path.join(os.homedir(), "gd-test-proj-"));
  dirs.push(work);
  const P = (await first.call("POST", "/api/projects", { name: "孤儿broker", path: work })).json;
  ids.push(P.id);
  assert.equal((await first.call("POST", `/api/projects/${P.id}/agents`, { role: "dv", ip: "uart" })).status, 201);
  const [orphan] = brokerPids(P.session);
  first.b.child.kill("SIGKILL");
  await new Promise((r) => first.b.child.once("exit", r));
  assert.deepEqual(brokerPids(P.session), [orphan], "kill -9 后 broker 成了孤儿");
  assert.equal(sh("herdr", ["session", "stop", P.session]).code, 0);
  const second = await backendWithFakes({}, first.b.home);
  const p0 = await waitHerdrDown(second.call, P.id);
  assert.equal(p0.broker.running, false, "herdr 没在运行时启动不会拉 broker");
  assert.deepEqual(brokerPids(P.session), [orphan], "孤儿 broker 仍占着锁");
  const rec = await second.call("POST", `/api/projects/${P.id}/recover`);
  assert.equal(rec.status, 200, JSON.stringify(rec.json));
  const now = brokerPids(P.session);
  assert.equal(now.length, 1);
  assert.notEqual(now[0], orphan, "孤儿 broker 应已被停掉、换成新的");
  assert.equal((await second.call("DELETE", `/api/projects/${P.id}`)).status, 200);
});

test("会话在运行、但查询状态报错(不是 server_not_running):恢复被拒,不关宿主 tmux、不重起会话、不停 broker;列表显示查询失败", async () => {
  const ctl = path.join(tempHome(), "fail.ctl");
  const real = { GD_REAL_HERDR: sh("bash", ["-lc", "command -v herdr"]).out.trim() };
  const { call } = await backendWithFakes({ GUI_DEMO_HERDR: path.join(ROOT, "test/fakes/herdr"), GD_FAKE_CTL: ctl, ...real });
  const work = fs.mkdtempSync(path.join(os.homedir(), "gd-test-proj-"));
  dirs.push(work);
  const P = (await call("POST", "/api/projects", { name: "查询报错", path: work })).json;
  ids.push(P.id);
  const host = `${PREFIX_HOST}${P.id}`;
  const hostCreated = sh("tmux", ["display-message", "-p", "-t", `=${host}:`, "#{session_created}"]).out.trim();
  const broker = (await call("GET", "/api/projects")).json.find((x) => x.id === P.id).broker.pid;
  fs.writeFileSync(ctl, `--session ${P.session} workspace list`);
  const rec = await call("POST", `/api/projects/${P.id}/recover`);
  assert.equal(rec.status, 503, JSON.stringify(rec.json));
  assert.match(rec.json.error, /状态不明/);
  const listed = (await call("GET", "/api/projects")).json.find((x) => x.id === P.id);
  assert.equal(listed.herdr, null, "列表应显示状态查询失败");
  fs.writeFileSync(ctl, "");
  assert.equal(sh("tmux", ["display-message", "-p", "-t", `=${host}:`, "#{session_created}"]).out.trim(), hostCreated, "宿主 tmux 不应被关掉重建");
  const after = (await call("GET", "/api/projects")).json.find((x) => x.id === P.id);
  assert.equal(after.herdr, true);
  assert.equal(after.broker.pid, broker, "broker 不应被停");
  assert.equal((await call("DELETE", `/api/projects/${P.id}`)).status, 200);
});
