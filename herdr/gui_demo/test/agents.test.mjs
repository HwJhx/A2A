// 第 3 步:agent 新增 / 列表 / 停止 / 重启 / 删除(21 号文档 §4.3–§4.5)。
// 用 a2a 集成测试的假 agent(以 pi 的名字运行,herdr 识别为 agent,不调用模型),真实 herdr 与 tmux。
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { after, before, test } from "node:test";
import { execFileSync } from "node:child_process";
import { api, cleanupIds, login, PREFIX, ROOT, sh, startBackend, stopBackend, tempHome } from "./helpers.mjs";

const A2A_SRC = process.env.GUI_DEMO_A2A_SRC ?? "/Users/jhx/Documents/code/personal/pi_agent/A2A/herdr/a2a/src";
const FAKE = path.resolve(A2A_SRC, "../tests/fake_agent.py");
const ids = [];
const dirs = [];
let b;
let call;
let P;
let ctl;
const fail = (...pats) => fs.writeFileSync(ctl, pats.join("\n"));

function launcher(dir, role) {
  const file = path.join(dir, `launch_${role}.sh`);
  // 与 fnx 启动脚本同结构:最后一行 exec;a2a 生命周期会让 argv[0] 变成 pi
  fs.writeFileSync(file, `#!/bin/bash\nexport FAKE_ROLE=${role}\nexec python3 ${FAKE} "${dir}/$A2A_ROLE""_""$A2A_IP"".log" work\n`, { mode: 0o755 });
  return file;
}

before(async () => {
  const fakeDir = tempHome();
  ctl = path.join(fakeDir, "fail.ctl");
  // tmux 走注入失败的包装(控制文件为空时直接调用真实 tmux)
  b = await startBackend({ env: { GUI_DEMO_LAUNCHER_DV: launcher(fakeDir, "dv"), GUI_DEMO_LAUNCHER_SW: launcher(fakeDir, "sw"),
    GUI_DEMO_TMUX: path.join(ROOT, "test/fakes/tmux"), GD_FAKE_CTL: ctl, GD_REAL_TMUX: sh("bash", ["-lc", "command -v tmux"]).out.trim() } });
  assert.ok(b.url, b.output());
  const { cookie } = await login(b);
  call = (m, p, body) => api(b, cookie, m, p, body);
  const work = fs.mkdtempSync(path.join(os.homedir(), "gd-test-proj-"));
  dirs.push(work);
  P = (await call("POST", "/api/projects", { name: "agent测试", path: work })).json;
  assert.ok(P.id, JSON.stringify(P));
  ids.push(P.id);
});

after(async () => {
  if (P?.id) await call("DELETE", `/api/projects/${P.id}`).catch(() => undefined);
  await stopBackend(b);
  cleanupIds(ids);
  for (const d of dirs) fs.rmSync(d, { recursive: true, force: true });
});

const agents = async () => (await call("GET", `/api/projects/${P.id}/agents`)).json;
const registry = () => JSON.parse(fs.readFileSync(path.join(b.home, "projects", P.id, "state", "registry.json"), "utf8")).agents;
const herdrAgent = (name) => sh("herdr", ["--session", P.session, "agent", "get", name]);

test("新增:dv_uart、sw_uart 各在自己角色的 tab,工作目录在项目路径下;列表正确", async () => {
  const dv = await call("POST", `/api/projects/${P.id}/agents`, { role: "dv", ip: "uart" });
  assert.equal(dv.status, 201, JSON.stringify(dv.json));
  assert.equal(dv.json.agentId, "dv_uart");
  assert.equal(dv.json.lifecycle, "running");
  const sw = await call("POST", `/api/projects/${P.id}/agents`, { role: "sw", ip: "uart" });
  assert.equal(sw.status, 201, JSON.stringify(sw.json));

  const list = await agents();
  assert.deepEqual(list.ips, ["uart"]);
  assert.deepEqual(list.roles, { dv: "验证智能体", sw: "软件智能体" });
  assert.deepEqual(list.agents.map((a) => a.agentId).sort(), ["dv_uart", "sw_uart"]);
  const reg = registry();
  assert.notEqual(reg.dv_uart.tab_id, reg.sw_uart.tab_id, "不同角色应在不同 tab");
  assert.equal(reg.dv_uart.workspace_id, reg.sw_uart.workspace_id);
  assert.equal(reg.dv_uart.cwd, path.join(P.path, "dv_uart"));
  assert.ok(fs.existsSync(path.join(P.path, "sw_uart")));
  assert.equal(herdrAgent("dv_uart").code, 0, "herdr 里应能按名字找到 dv_uart");
});

test("新增的参数校验:重复 409、种类不对、IP 名不合法、超长都 400", async () => {
  assert.equal((await call("POST", `/api/projects/${P.id}/agents`, { role: "dv", ip: "uart" })).status, 409);
  assert.equal((await call("POST", `/api/projects/${P.id}/agents`, { role: "rtl", ip: "x" })).status, 400);
  for (const ip of ["Uart", "1uart", "u art", "u/a", "", "a".repeat(33)]) {
    assert.equal((await call("POST", `/api/projects/${P.id}/agents`, { role: "dv", ip })).status, 400, ip);
  }
  assert.equal((await call("POST", `/api/projects/${P.id}/agents`, { role: "dv", ip: "a".repeat(30) })).status, 400, "dv_ + 30 字超过 32");
  assert.equal((await call("POST", "/api/projects/p00000000/agents", { role: "dv", ip: "x" })).status, 404);
});

test("停止与重启:stop 后 stopped、herdr 里没有该 agent;restore 后 running", async () => {
  const stop = await call("POST", `/api/projects/${P.id}/agents/dv_uart/stop`);
  assert.equal(stop.status, 200, JSON.stringify(stop.json));
  assert.equal(stop.json.lifecycle, "stopped");
  assert.notEqual(herdrAgent("dv_uart").code, 0);
  const restore = await call("POST", `/api/projects/${P.id}/agents/dv_uart/restore`);
  assert.equal(restore.status, 200, JSON.stringify(restore.json));
  assert.equal(restore.json.lifecycle, "running");
  assert.equal(herdrAgent("dv_uart").code, 0);
  assert.equal((await call("POST", `/api/projects/${P.id}/agents/dv_nope/stop`)).status, 404);
  assert.equal((await call("POST", `/api/projects/${P.id}/agents/rtl_uart/stop`)).status, 404);
});

test("删除 IP:expect 与实际不一致(只写了 dv_uart)时 409,两个都还在;一致时两个都删、IP 从拓扑删除", async () => {
  const wrong = await call("DELETE", `/api/projects/${P.id}/ips/uart`, { expect: ["dv_uart"] });
  assert.equal(wrong.status, 409, JSON.stringify(wrong.json));
  assert.deepEqual((await agents()).agents.map((a) => a.agentId).sort(), ["dv_uart", "sw_uart"]);
  assert.equal((await call("DELETE", `/api/projects/${P.id}/ips/uart`, {})).status, 400);

  const ok = await call("DELETE", `/api/projects/${P.id}/ips/uart`, { expect: ["sw_uart", "dv_uart"] });
  assert.equal(ok.status, 200, JSON.stringify(ok.json));
  assert.deepEqual(ok.json.removed, ["dv_uart", "sw_uart"]);
  const list = await agents();
  assert.deepEqual(list.agents, []);
  assert.deepEqual(list.ips, []);
  assert.notEqual(herdrAgent("dv_uart").code, 0);
  assert.notEqual(herdrAgent("sw_uart").code, 0);
});

const windowName = (agentId) => `${PREFIX}win_${P.id}_${agentId}`;
const tmuxHas = (name) => sh("tmux", ["has-session", "-t", `=${name}`]).code === 0;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

test("删除 IP 后关窗口 tmux 失败:报告部分完成(200 + 提示);残留窗口在刷新列表时被回收", async () => {
  assert.equal((await call("POST", `/api/projects/${P.id}/agents`, { role: "dv", ip: "win" })).status, 201);
  // 第 4 步才会自动建窗口;这里手动建一个同名 tmux 代替
  sh("tmux", ["new-session", "-d", "-s", windowName("dv_win"), "sleep", "600"]);
  assert.ok(tmuxHas(windowName("dv_win")));
  fail(`kill-session -t =${windowName("dv_win")}`);
  const del = await call("DELETE", `/api/projects/${P.id}/ips/win`, { expect: ["dv_win"] });
  assert.equal(del.status, 200, JSON.stringify(del.json));
  assert.deepEqual(del.json.removed, ["dv_win"]);
  assert.equal(del.json.warnings.length, 1);
  assert.match(del.json.warnings[0], /窗口没关掉/);
  assert.ok(tmuxHas(windowName("dv_win")), "注入失败时窗口应还在");
  fail();
  await call("GET", `/api/projects/${P.id}/agents`); // 触发回收(异步)
  for (let i = 0; i < 50 && tmuxHas(windowName("dv_win")); i++) await sleep(200);
  assert.ok(!tmuxHas(windowName("dv_win")), "刷新列表后残留窗口应被回收");
  assert.deepEqual((await call("GET", `/api/projects/${P.id}/agents`)).json.warnings, []);
});

test("队列里有未投递的消息时删除 IP:报错,agent 与窗口都保留;消息投递后再删成功", async () => {
  assert.equal((await call("POST", `/api/projects/${P.id}/agents`, { role: "dv", ip: "q" })).status, 201);
  assert.equal((await call("POST", `/api/projects/${P.id}/agents`, { role: "sw", ip: "q" })).status, 201);
  sh("tmux", ["new-session", "-d", "-s", windowName("dv_q"), "sleep", "600"]);
  const brokerPid = (await call("GET", "/api/projects")).json.find((x) => x.id === P.id).broker.pid;
  const state = path.join(b.home, "projects", P.id, "state");
  const env = { ...process.env, A2A_STATE_DIR: state, A2A_TOPOLOGY: P.topology, HERDR_SESSION: P.session,
    PYTHONPATH: A2A_SRC, PYTHONDONTWRITEBYTECODE: "1", A2A_PROJECT_ID: P.id, A2A_ROLE: "sw", A2A_IP: "q",
    HERDR_PANE_ID: registry().sw_q.pane_id };
  process.kill(brokerPid, "SIGSTOP"); // 冻住 broker:消息留在队列里(进程没退出,不会被重启)
  try {
    const sent = JSON.parse(execFileSync("python3", ["-m", "a2a.cli", "send", "sw_test_pass"], { env, encoding: "utf8" }));
    assert.ok(sent.msg_id, JSON.stringify(sent));
    const del = await call("DELETE", `/api/projects/${P.id}/ips/q`, { expect: ["dv_q", "sw_q"] });
    assert.equal(del.status, 500, JSON.stringify(del.json));
    assert.match(del.json.error, /删除 IP q 失败/);
    assert.deepEqual((await call("GET", `/api/projects/${P.id}/agents`)).json.agents.filter((a) => a.ip === "q").map((a) => a.agentId).sort(),
      ["dv_q", "sw_q"]);
    assert.ok(tmuxHas(windowName("dv_q")), "删除失败时不应关窗口");
  } finally {
    process.kill(brokerPid, "SIGCONT");
  }
  // broker 恢复后把消息投给 dv_q(假 agent 报告 working → idle),队列清空后再删
  let ok;
  for (let i = 0; i < 30; i++) {
    ok = await call("DELETE", `/api/projects/${P.id}/ips/q`, { expect: ["dv_q", "sw_q"] });
    if (ok.status === 200) break;
    await sleep(1000);
  }
  assert.equal(ok.status, 200, JSON.stringify(ok.json));
  assert.ok(!tmuxHas(windowName("dv_q")));
});

test("只有一类时删除:dv_spi 单独删除,IP spi 从拓扑删除;删除工程时还在的 agent 一起清掉", async () => {
  assert.equal((await call("POST", `/api/projects/${P.id}/agents`, { role: "dv", ip: "spi" })).status, 201);
  const del = await call("DELETE", `/api/projects/${P.id}/ips/spi`, { expect: ["dv_spi"] });
  assert.equal(del.status, 200, JSON.stringify(del.json));
  assert.deepEqual((await agents()).ips, []);

  assert.equal((await call("POST", `/api/projects/${P.id}/agents`, { role: "sw", ip: "i2c" })).status, 201);
  const pane = registry().sw_i2c.pane_id;
  const delProject = await call("DELETE", `/api/projects/${P.id}`);
  assert.equal(delProject.status, 200, JSON.stringify(delProject.json));
  assert.ok(!sh("herdr", ["session", "list"]).out.includes(P.session));
  P = null;
  void pane;
});
