// 第 1 步:工程新建 / 列表 / 删除、回滚、工程环境隔离(21 号文档 §3、§4.1、§4.2)。需要真实 herdr 与 tmux。
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { after, test } from "node:test";
import { api, cleanupIds, herdrSessionNames, login, PREFIX, ROOT, startBackend, stopBackend, tmuxNames } from "./helpers.mjs";

const backends = [];
const ids = [];
const workDirs = [];
after(async () => {
  for (const b of backends) await stopBackend(b);
  cleanupIds(ids);
  for (const d of workDirs) fs.rmSync(d, { recursive: true, force: true });
});

async function session(env) {
  const b = await startBackend({ env });
  backends.push(b);
  assert.ok(b.url, b.output());
  const { cookie } = await login(b);
  return { b, call: (method, p, body) => api(b, cookie, method, p, body) };
}

function workDir() {
  const d = fs.mkdtempSync(path.join(os.homedir(), "gd-test-proj-"));
  workDirs.push(d);
  return d;
}

const A2A_SRC = process.env.GUI_DEMO_A2A_SRC ?? "/Users/jhx/Documents/code/personal/pi_agent/A2A/herdr/a2a/src";
function a2aRaw(state, args) {
  const env = { ...process.env, A2A_STATE_DIR: state, A2A_TOPOLOGY: path.join(state, "topology.yaml"),
    PYTHONPATH: A2A_SRC, PYTHONDONTWRITEBYTECODE: "1" };
  return execFileSync("python3", ["-m", "a2a.cli", ...args], { env, encoding: "utf8" });
}

test("新建两个工程:各自的 herdr 会话、宿主 tmux、broker、拓扑;删除后全部清干净,项目路径保留", async () => {
  const { b, call } = await session();
  const pa = workDir();
  const pb = workDir();
  const A = await call("POST", "/api/projects", { name: "工程甲", path: pa });
  assert.equal(A.status, 201, JSON.stringify(A.json));
  ids.push(A.json.id);
  const B = await call("POST", "/api/projects", { name: "工程乙", path: pb });
  assert.equal(B.status, 201, JSON.stringify(B.json));
  ids.push(B.json.id);

  for (const p of [A.json, B.json]) {
    assert.equal(p.session, `${PREFIX}${p.id}`);
    assert.ok(herdrSessionNames().includes(p.session));
    assert.ok(tmuxNames().includes(`${PREFIX}host_${p.id}`));
  }
  const list = (await call("GET", "/api/projects")).json;
  assert.deepEqual(list.map((p) => p.name).sort(), ["工程乙", "工程甲"]);
  for (const p of list) {
    assert.equal(p.herdr, true);
    assert.equal(p.broker.running, true);
    // broker 进程确实带着本工程的会话名
    const cmdline = fs.readFileSync(`/proc/${p.broker.pid}/cmdline`, "utf8").split("\0").filter(Boolean);
    assert.deepEqual(cmdline.slice(-4), ["broker", "run", "--session", p.session]);
    const env = fs.readFileSync(`/proc/${p.broker.pid}/environ`, "utf8").split("\0");
    assert.ok(env.includes(`A2A_STATE_DIR=${path.join(b.home, "projects", p.id, "state")}`));
    assert.ok(env.includes(`HERDR_SESSION=${p.session}`));
  }

  // 拓扑:project_id = 工程 id,workspace_label = 工程名;两个工程互不串
  const stA = path.join(b.home, "projects", A.json.id, "state");
  const stB = path.join(b.home, "projects", B.json.id, "state");
  const show = (st) => JSON.parse(a2aRaw(st, ["topology", "show"])).topology;
  assert.equal(show(stA).project_id, A.json.id);
  assert.equal(show(stA).workspace_label, "工程甲");
  assert.equal(show(stB).project_id, B.json.id);
  a2aRaw(stA, ["topology", "add-ip", "uart"]);
  assert.deepEqual(show(stA).ips, ["uart"]);
  assert.deepEqual(show(stB).ips, []);
  a2aRaw(stA, ["topology", "remove-ip", "uart"]);

  // 删除 A
  const del = await call("DELETE", `/api/projects/${A.json.id}`);
  assert.equal(del.status, 200, JSON.stringify(del.json));
  assert.ok(!herdrSessionNames().includes(A.json.session));
  assert.ok(!tmuxNames().some((t) => t.includes(A.json.id)));
  assert.ok(!fs.existsSync(path.join(b.home, "projects", A.json.id)));
  assert.ok(fs.existsSync(pa), "项目路径保留");
  const brokerA = list.find((p) => p.id === A.json.id).broker.pid;
  assert.throws(() => process.kill(brokerA, 0), "broker 应已退出");
  assert.deepEqual((await call("GET", "/api/projects")).json.map((p) => p.id), [B.json.id]);
  assert.equal((await call("DELETE", `/api/projects/${A.json.id}`)).status, 404);

  const delB = await call("DELETE", `/api/projects/${B.json.id}`);
  assert.equal(delB.status, 200, JSON.stringify(delB.json));
});

test("后端重启:工程列表恢复,broker 重新拉起", async () => {
  const { b, call } = await session();
  const P = await call("POST", "/api/projects", { name: "重启测试", path: workDir() });
  assert.equal(P.status, 201, JSON.stringify(P.json));
  ids.push(P.json.id);
  const oldPid = (await call("GET", "/api/projects")).json[0].broker.pid;
  await stopBackend(b);
  assert.throws(() => process.kill(oldPid, 0), "后端退出时 broker 一起停");
  assert.ok(herdrSessionNames().includes(P.json.session), "herdr 会话保留");

  const again = await startBackend({ home: b.home });
  backends.push(again);
  const { cookie } = await login(again);
  const list = (await api(again, cookie, "GET", "/api/projects")).json;
  assert.equal(list.length, 1);
  assert.equal(list[0].broker.running, true);
  assert.notEqual(list[0].broker.pid, oldPid);
  assert.equal((await api(again, cookie, "DELETE", `/api/projects/${P.json.id}`)).status, 200);
});

for (const step of ["topology", "herdr", "broker", "save"]) {
  test(`新建在"${step}"失败:按相反顺序回滚,不留会话、tmux、状态目录与登记`, async () => {
    const { b, call } = await session({ GUI_DEMO_FAIL_AT: step });
    const before = herdrSessionNames();
    const r = await call("POST", "/api/projects", { name: `回滚-${step}`, path: workDir() });
    assert.equal(r.status, 500);
    assert.equal(r.json.step, step);
    assert.deepEqual(herdrSessionNames().filter((s) => s.startsWith(PREFIX) && !before.includes(s)), []);
    assert.deepEqual(fs.existsSync(path.join(b.home, "projects")) ? fs.readdirSync(path.join(b.home, "projects")) : [], []);
    assert.deepEqual((await call("GET", "/api/projects")).json, []);
  });
}

test("参数校验:相对路径、家目录外、数据目录内、空名、重名都被拒", async () => {
  const { b, call } = await session();
  const bad = async (body) => (await call("POST", "/api/projects", body)).status;
  assert.equal(await bad({ name: "x", path: "rel/path" }), 400);
  assert.equal(await bad({ name: "x", path: "/tmp/gd-outside" }), 400);
  assert.equal(await bad({ name: "x", path: os.homedir() + "/../" }), 400);
  assert.equal(await bad({ name: "  ", path: workDir() }), 400);
  assert.equal(await bad({ name: "x".repeat(41), path: workDir() }), 400);
  const link = path.join(workDir(), "escape");
  fs.symlinkSync("/tmp", link);
  assert.equal(await bad({ name: "x", path: link }), 400);
  const ok = await call("POST", "/api/projects", { name: "唯一", path: workDir() });
  assert.equal(ok.status, 201);
  ids.push(ok.json.id);
  assert.equal(await bad({ name: "唯一", path: workDir() }), 400);
  assert.equal((await call("DELETE", `/api/projects/${ok.json.id}`)).status, 200);
  void b;
});

test("工程环境:只对 agent / ip / broker run 传 --session,其余靠环境变量;继承的 A2A_* / HERDR_* 被清掉", async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "gd-unit-"));
  workDirs.push(home);
  process.env.GUI_DEMO_HOME = home;
  process.env.GUI_DEMO_HOST = "127.0.0.1";
  process.env.GUI_DEMO_SESSION_PREFIX = PREFIX;
  const mod = await import(path.join(ROOT, "server/dist/a2a.js"));
  const p = { id: "p0123abcd", session: `${PREFIX}p0123abcd` };
  assert.deepEqual(mod.a2aArgs(p, ["agent", "list"]).slice(-2), ["--session", p.session]);
  assert.deepEqual(mod.a2aArgs(p, ["ip", "add", "uart"]).slice(-2), ["--session", p.session]);
  assert.deepEqual(mod.a2aArgs(p, ["broker", "run"]).slice(-2), ["--session", p.session]);
  for (const args of [["queue"], ["status"], ["resolve", "x", "delivered"], ["topology", "show"]]) {
    assert.ok(!mod.a2aArgs(p, args).includes("--session"), args.join(" "));
  }
  process.env.A2A_STATE_DIR = "/should/not/leak";
  process.env.HERDR_PANE_ID = "w9:p9";
  const env = mod.projectEnv(p);
  delete process.env.A2A_STATE_DIR;
  delete process.env.HERDR_PANE_ID;
  assert.equal(env.A2A_STATE_DIR, mod.stateDir(p.id));
  assert.equal(env.A2A_TOPOLOGY, mod.topologyPath(p.id));
  assert.equal(env.HERDR_SESSION, p.session);
  assert.equal(env.HERDR_PANE_ID, undefined);
  assert.throws(() => mod.projectEnv({ id: p.id, session: "default" }));

  // 经包装函数调用不带 --session 的命令(topology show、queue):按环境变量读到本工程的状态
  fs.mkdirSync(mod.stateDir(p.id), { recursive: true });
  const topo = { version: 1, project_id: p.id, workspace_label: "单测", roles: { dv: { label: "dv", launcher: "/bin/true" } }, ips: [], edges: [] };
  const init = await mod.pyTool(p, ["init", JSON.stringify(topo)]);
  assert.equal(init.code, 0, init.stderr);
  await mod.checkProject(p);
  const shown = await mod.a2a(p, ["topology", "show"]);
  assert.equal(shown.code, 0, shown.stderr);
  assert.equal(JSON.parse(shown.stdout).path, mod.topologyPath(p.id));
  // 用本工程专属的数据断言:在本工程状态目录放一个带工程 id 的 dispatch.halted,queue 必须读到它;
  // 另一个工程(没有这个文件)读到的必须是 null
  fs.writeFileSync(path.join(mod.stateDir(p.id), "dispatch.halted"), JSON.stringify({ reason: `unit-${p.id}` }));
  const queue = await mod.a2a(p, ["queue"]);
  assert.equal(queue.code, 0, queue.stderr);
  assert.equal(JSON.parse(queue.stdout).dispatch_halted.reason, `unit-${p.id}`);
  const other = { id: "p4567cdef", session: `${PREFIX}p4567cdef` };
  fs.mkdirSync(mod.stateDir(other.id), { recursive: true });
  const queueOther = await mod.a2a(other, ["queue"]);
  assert.equal(queueOther.code, 0, queueOther.stderr);
  assert.equal(JSON.parse(queueOther.stdout).dispatch_halted, null);
  await assert.rejects(mod.checkProject({ id: "p99999999", session: `${PREFIX}p99999999` }));
});

test("页面一用到的接口:简介、按 id 取工程、配置", async () => {
  const { call } = await session();
  const cfg = await call("GET", "/api/config");
  assert.equal(cfg.status, 200);
  assert.equal(cfg.json.home, os.homedir());
  assert.equal((await call("POST", "/api/projects", { name: "简介太长", path: workDir(), description: "x".repeat(201) })).status, 400);
  assert.equal((await call("POST", "/api/projects", { name: "简介类型", path: workDir(), description: 42 })).status, 400);
  const P = await call("POST", "/api/projects", { name: "<b>有简介</b>", path: workDir(), description: "  smoke。 " });
  assert.equal(P.status, 201, JSON.stringify(P.json));
  ids.push(P.json.id);
  const got = await call("GET", `/api/projects/${P.json.id}`);
  assert.equal(got.status, 200);
  assert.equal(got.json.description, "smoke。");
  assert.equal(got.json.name, "<b>有简介</b>");
  assert.equal((await call("GET", "/api/projects/p00000000")).status, 404);
  assert.equal((await call("DELETE", `/api/projects/${P.json.id}`)).status, 200);
});
