// 21 号文档 §11:拓扑文件放在 <项目路径>/topology.yaml。新建时的占位检查、回滚与删除只删本工程的拓扑文件、
// .lock 永远不删、删除前归属检查、删除中被替换、与外部拓扑写入并发、旧格式工程记录。真实 herdr 与 tmux,不建 agent。
import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { after, test } from "node:test";
import { api, cleanupIds, herdrSessionNames, login, sh, startBackend, stopBackend, tempHome } from "./helpers.mjs";

const A2A_SRC = process.env.GUI_DEMO_A2A_SRC ?? "/Users/jhx/Documents/code/personal/pi_agent/A2A/herdr/a2a/src";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const backends = [];
const ids = [];
const dirs = [];
after(async () => {
  for (const b of backends) await stopBackend(b);
  cleanupIds(ids);
  for (const d of dirs) fs.rmSync(d, { recursive: true, force: true });
});

async function session(env = {}, home) {
  const b = await startBackend({ env, ...(home ? { home } : {}) });
  backends.push(b);
  assert.ok(b.url, b.output());
  const { cookie } = await login(b);
  return { b, call: (m, p, body) => api(b, cookie, m, p, body) };
}
function workDir() {
  const d = fs.mkdtempSync(path.join(os.homedir(), "gd-test-proj-"));
  dirs.push(d);
  return d;
}
const T = (dir, suffix = "") => path.join(dir, `topology.yaml${suffix}`);
function a2aRaw(b, P, args) {
  const env = { ...process.env, A2A_STATE_DIR: path.join(b.home, "projects", P.id, "state"), A2A_TOPOLOGY: P.topology,
    PYTHONPATH: A2A_SRC, PYTHONDONTWRITEBYTECODE: "1" };
  try {
    return { code: 0, out: execFileSync("python3", ["-m", "a2a.cli", ...args], { env, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }) };
  } catch (e) {
    return { code: e.status ?? 1, out: String(e.stdout ?? "") + String(e.stderr ?? "") };
  }
}
async function create(call, name, dir) {
  const r = await call("POST", "/api/projects", { name, path: dir });
  if (r.status === 201) ids.push(r.json.id);
  return r;
}

test("项目路径下已有 topology.yaml(普通文件 / 软链接)或 topology.yaml.bak:新建 409,原文件不变,不建会话与状态", async () => {
  const { b, call } = await session();
  const other = path.join(tempHome(), "elsewhere.yaml");
  fs.writeFileSync(other, "not mine\n");
  for (const [suffix, make] of [["", (p) => fs.writeFileSync(p, "keep: me\n")], ["", (p) => fs.symlinkSync(other, p)],
    [".bak", (p) => fs.writeFileSync(p, "keep: bak\n")]]) {
    const dir = workDir();
    make(T(dir, suffix));
    const before = herdrSessionNames();
    const r = await create(call, `占位${suffix || "主"}${Math.random()}`.slice(0, 30), dir);
    assert.equal(r.status, 409, JSON.stringify(r.json));
    assert.match(r.json.error, /topology\.yaml/);
    if (suffix === ".bak") assert.equal(fs.readFileSync(T(dir, ".bak"), "utf8"), "keep: bak\n");
    else if (fs.lstatSync(T(dir)).isSymbolicLink()) assert.equal(fs.readFileSync(other, "utf8"), "not mine\n");
    else assert.equal(fs.readFileSync(T(dir), "utf8"), "keep: me\n");
    assert.deepEqual(herdrSessionNames().filter((s) => !before.includes(s)), []);
  }
  assert.deepEqual((await call("GET", "/api/projects")).json, []);
  assert.deepEqual(fs.existsSync(path.join(b.home, "projects")) ? fs.readdirSync(path.join(b.home, "projects")) : [], []);
});

test("两个工程用同一个项目路径:第二个 409", async () => {
  const { call } = await session();
  const dir = workDir();
  const A = await create(call, "同路径甲", dir);
  assert.equal(A.status, 201, JSON.stringify(A.json));
  const B = await create(call, "同路径乙", dir);
  assert.equal(B.status, 409);
  assert.equal((await call("DELETE", `/api/projects/${A.json.id}`)).status, 200);
});

test("topology.yaml.lock 是软链接或目录:新建 409,软链接指向的文件不被改动", async () => {
  const { call } = await session();
  const target = path.join(tempHome(), "lock-target");
  fs.writeFileSync(target, "untouched\n");
  const d1 = workDir();
  fs.symlinkSync(target, T(d1, ".lock"));
  assert.equal((await create(call, "锁是软链接", d1)).status, 409);
  assert.equal(fs.readFileSync(target, "utf8"), "untouched\n");
  const d2 = workDir();
  fs.mkdirSync(T(d2, ".lock"));
  assert.equal((await create(call, "锁是目录", d2)).status, 409);
});

test("预存普通文件 .lock:新建成功;删除工程后 topology.yaml、.bak 删除,.lock 保留且内容不变,同一路径可再新建", async () => {
  const { b, call } = await session();
  const dir = workDir();
  fs.writeFileSync(T(dir, ".lock"), "MARKER-LOCK\n");
  const P = await create(call, "预存锁", dir);
  assert.equal(P.status, 201, JSON.stringify(P.json));
  assert.equal(P.json.topology, T(dir));
  assert.ok(fs.existsSync(T(dir)));
  // 改一次拓扑,让 a2a 产生 .bak
  assert.equal(a2aRaw(b, P.json, ["topology", "add-edge", "x_ping", "dv", "sw", "{ip} ping"]).code, 0);
  assert.ok(fs.existsSync(T(dir, ".bak")));
  const del = await call("DELETE", `/api/projects/${P.json.id}`);
  assert.equal(del.status, 200, JSON.stringify(del.json));
  assert.deepEqual(del.json.warnings, []);
  assert.ok(!fs.existsSync(T(dir)) && !fs.existsSync(T(dir, ".bak")));
  assert.equal(fs.readFileSync(T(dir, ".lock"), "utf8"), "MARKER-LOCK\n", ".lock 应保留且内容不变");
  const again = await create(call, "预存锁二", dir);
  assert.equal(again.status, 201, JSON.stringify(again.json));
  assert.equal((await call("DELETE", `/api/projects/${again.json.id}`)).status, 200);
});

for (const step of ["herdr", "broker", "save"]) {
  test(`新建在"${step}"失败回滚:topology.yaml 与 .bak 不在,预存 .lock 保留且内容不变`, async () => {
    const { call } = await session({ GUI_DEMO_FAIL_AT: step });
    const dir = workDir();
    fs.writeFileSync(T(dir, ".lock"), "MARKER-ROLLBACK\n");
    const r = await create(call, `回滚拓扑-${step}`, dir);
    assert.equal(r.status, 500, JSON.stringify(r.json));
    assert.ok(!fs.existsSync(T(dir)), "本次建的 topology.yaml 应已删除");
    assert.ok(!fs.existsSync(T(dir, ".bak")));
    assert.equal(fs.readFileSync(T(dir, ".lock"), "utf8"), "MARKER-ROLLBACK\n");
  });
}

test("删除前拓扑或 .bak 不属于本工程:409,broker、会话、状态目录、工程记录、文件全都不动", async () => {
  const { b, call } = await session();
  for (const suffix of ["", ".bak"]) {
    const dir = workDir();
    const P = await create(call, `归属${suffix || "主"}`, dir);
    assert.equal(P.status, 201);
    if (suffix === ".bak") assert.equal(a2aRaw(b, P.json, ["topology", "add-edge", "x_ping", "dv", "sw", "{ip} ping"]).code, 0);
    const file = T(dir, suffix);
    const foreign = fs.readFileSync(file, "utf8").replace(`project_id: ${P.json.id}`, "project_id: pdeadbeef");
    fs.writeFileSync(file, foreign);
    const broker = (await call("GET", "/api/projects")).json.find((x) => x.id === P.json.id).broker.pid;
    const del = await call("DELETE", `/api/projects/${P.json.id}`);
    assert.equal(del.status, 409, JSON.stringify(del.json));
    assert.match(del.json.error, /工程未删除/);
    const now = (await call("GET", "/api/projects")).json.find((x) => x.id === P.json.id);
    assert.ok(now, "工程记录应保留");
    assert.equal(now.broker.pid, broker, "broker 不应被停");
    assert.ok(herdrSessionNames().includes(P.json.session), "会话应保留");
    assert.ok(fs.existsSync(path.join(b.home, "projects", P.json.id, "state", "audit.jsonl")), "状态目录应保留");
    assert.equal(fs.readFileSync(file, "utf8"), foreign, "文件不应被改动");
    // 改回来后能正常删除
    fs.writeFileSync(file, foreign.replace("project_id: pdeadbeef", `project_id: ${P.json.id}`));
    assert.equal((await call("DELETE", `/api/projects/${P.json.id}`)).status, 200);
  }
});

test("删除过程中(归属检查之后)拓扑被替换:报错,状态目录与工程记录保留,替换后的文件不删;.bak 被替换只告警、删除完成", async () => {
  const { b, call } = await session({ GUI_DEMO_DELETE_PAUSE_MS: "1500" });
  const waitPause = async (id) => {
    for (let i = 0; i < 50 && !b.output().includes(`测试:删除停顿 ${id}`); i++) await sleep(100);
    assert.ok(b.output().includes(`测试:删除停顿 ${id}`), b.output());
  };
  // 主拓扑被替换
  const d1 = workDir();
  const P = (await create(call, "删除中替换主", d1)).json;
  const deleting = call("DELETE", `/api/projects/${P.id}`);
  await waitPause(P.id);
  const replaced = fs.readFileSync(T(d1), "utf8").replace(`project_id: ${P.id}`, "project_id: pdeadbeef");
  fs.writeFileSync(T(d1), replaced);
  const del = await deleting;
  assert.equal(del.status, 409, JSON.stringify(del.json));
  assert.equal(fs.readFileSync(T(d1), "utf8"), replaced, "替换后的文件不应被删");
  assert.ok(fs.existsSync(path.join(b.home, "projects", P.id, "state", "audit.jsonl")), "状态目录应保留");
  assert.ok((await call("GET", "/api/projects")).json.some((x) => x.id === P.id), "工程记录应保留");
  fs.writeFileSync(T(d1), replaced.replace("project_id: pdeadbeef", `project_id: ${P.id}`));
  assert.equal((await call("DELETE", `/api/projects/${P.id}`)).status, 200, "改回后可以删");

  // .bak 被替换
  const d2 = workDir();
  const Q = (await create(call, "删除中替换备份", d2)).json;
  assert.equal(a2aRaw(b, Q, ["topology", "add-edge", "x_ping", "dv", "sw", "{ip} ping"]).code, 0);
  const deleting2 = call("DELETE", `/api/projects/${Q.id}`);
  await waitPause(Q.id);
  const bak = fs.readFileSync(T(d2, ".bak"), "utf8").replace(`project_id: ${Q.id}`, "project_id: pdeadbeef");
  fs.writeFileSync(T(d2, ".bak"), bak);
  const del2 = await deleting2;
  assert.equal(del2.status, 200, JSON.stringify(del2.json));
  assert.equal(del2.json.warnings.length, 1);
  assert.match(del2.json.warnings[0], /已保留/);
  assert.ok(!fs.existsSync(T(d2)), "主拓扑应已删除");
  assert.equal(fs.readFileSync(T(d2, ".bak"), "utf8"), bak, "不属于本工程的 .bak 应保留");
  assert.ok(!(await call("GET", "/api/projects")).json.some((x) => x.id === Q.id), "工程记录应已删除");
});

test("删除与外部拓扑写入并发:外部持锁时删除等待;删除后外部写入报读不到文件,不会重新写出 topology.yaml", async () => {
  const { b, call } = await session();
  const dir = workDir();
  const P = (await create(call, "并发写入", dir)).json;
  const holder = spawn("python3", ["-c",
    "import sys,time\nfrom pathlib import Path\nfrom a2a._fsutil import exclusive_lock\n"
    + "with exclusive_lock(Path(sys.argv[1])):\n    print('locked', flush=True)\n    time.sleep(3)\n", T(dir, ".lock")],
  { env: { ...process.env, PYTHONPATH: A2A_SRC }, stdio: ["ignore", "pipe", "inherit"] });
  await new Promise((r) => holder.stdout.once("data", r));
  const t0 = Date.now();
  const del = await call("DELETE", `/api/projects/${P.id}`);
  const waited = Date.now() - t0;
  assert.equal(del.status, 200, JSON.stringify(del.json));
  assert.ok(waited >= 1500, `删除应在锁上等待,实际 ${waited} 毫秒`);
  await new Promise((r) => (holder.exitCode !== null ? r() : holder.once("exit", r)));
  fs.mkdirSync(path.join(b.home, "projects", P.id, "state"), { recursive: true }); // a2a 命令需要状态目录存在
  const late = a2aRaw(b, P, ["topology", "add-edge", "x_late", "dv", "sw", "{ip} late"]);
  assert.notEqual(late.code, 0);
  assert.match(late.out, /无法读取拓扑文件|拓扑/);
  assert.ok(!fs.existsSync(T(dir)), "不应重新写出 topology.yaml");
  fs.rmSync(path.join(b.home, "projects", P.id), { recursive: true, force: true });
});

test("旧格式工程记录(没有 topology 字段、拓扑在状态目录里):重启后仍能列出、读拓扑、删除", async () => {
  const first = await session();
  const dir = workDir();
  const P = (await create(first.call, "旧格式", dir)).json;
  await stopBackend(first.b);
  // 改成旧格式:拓扑挪进状态目录,记录去掉 topology 字段
  const state = path.join(first.b.home, "projects", P.id, "state");
  fs.copyFileSync(T(dir), path.join(state, "topology.yaml")); // 跨文件系统(家目录 / /tmp),不能 rename
  fs.rmSync(T(dir));
  const file = path.join(first.b.home, "projects.json");
  const list = JSON.parse(fs.readFileSync(file, "utf8"));
  delete list[0].topology;
  fs.writeFileSync(file, JSON.stringify(list));
  const second = await session({}, first.b.home);
  const got = await second.call("GET", `/api/projects/${P.id}`);
  assert.equal(got.status, 200);
  assert.equal(got.json.topology, undefined);
  const listed = (await second.call("GET", "/api/projects")).json[0];
  assert.equal(listed.broker.running, true, "后端重启后旧工程的 broker 应拉起(说明拓扑核对通过)");
  assert.equal((await second.call("GET", `/api/projects/${P.id}/agents`)).status, 200);
  assert.equal((await second.call("DELETE", `/api/projects/${P.id}`)).status, 200);
  assert.ok(!fs.existsSync(state));
  void sh;
});

test("拓扑文件已写入、init 随后失败(注入):回滚删掉本次写的 topology.yaml,状态目录与记录都不在,同一路径可重建", async () => {
  const bad = await session({ GUI_DEMO_FAIL_AT: "topology_after_write" });
  const dir = workDir();
  const r = await create(bad.call, "写后失败", dir);
  assert.equal(r.status, 500, JSON.stringify(r.json));
  assert.doesNotMatch(r.json.error, /回滚未完成/);
  assert.ok(!fs.existsSync(T(dir)), "本次写入的 topology.yaml 应被回滚删除");
  assert.deepEqual(fs.existsSync(path.join(bad.b.home, "projects")) ? fs.readdirSync(path.join(bad.b.home, "projects")) : [], []);
  assert.deepEqual((await bad.call("GET", "/api/projects")).json, []);
  const good = await session();
  const again = await create(good.call, "写后失败重建", dir);
  assert.equal(again.status, 201, JSON.stringify(again.json));
  assert.equal((await good.call("DELETE", `/api/projects/${again.json.id}`)).status, 200);
});

for (const [step, label] of [["bak_unlink", "删不掉"], ["bak_lstat", "检查出错"]]) test(`删除工程时 .bak ${label}(注入 ${step}):主拓扑、状态目录、记录照常删除,.bak 保留并给警告`, async () => {
  const { b, call } = await session({ GUI_DEMO_FAIL_AT: step });
  const dir = workDir();
  const P = (await create(call, "备份删不掉", dir)).json;
  assert.equal(a2aRaw(b, P, ["topology", "add-edge", "x_ping", "dv", "sw", "{ip} ping"]).code, 0);
  const del = await call("DELETE", `/api/projects/${P.id}`);
  assert.equal(del.status, 200, JSON.stringify(del.json));
  assert.equal(del.json.warnings.length, 1);
  assert.match(del.json.warnings[0], new RegExp(`测试注入:${step}`));
  assert.ok(!fs.existsSync(T(dir)), "主拓扑应已删除");
  assert.ok(fs.existsSync(T(dir, ".bak")), ".bak 应保留");
  assert.ok(!fs.existsSync(path.join(b.home, "projects", P.id)), "状态目录应已删除");
  assert.ok(!(await call("GET", "/api/projects")).json.some((x) => x.id === P.id), "工程记录应已删除");
});

test(".bak 不是合法 UTF-8:删除前遇到 → 409 什么都不动;删除过程中才被换成这样 → 照常删完并给警告", async () => {
  const { b, call } = await session({ GUI_DEMO_DELETE_PAUSE_MS: "1500" });
  const garbage = Buffer.from([0xff, 0xfe, 0x00, 0x80, 0x81, 0x0a]);
  // 1. 删除前就不是合法 UTF-8
  const d1 = workDir();
  const P = (await create(call, "坏备份一", d1)).json;
  fs.writeFileSync(T(d1, ".bak"), garbage);
  const del1 = await call("DELETE", `/api/projects/${P.id}`);
  assert.equal(del1.status, 409, JSON.stringify(del1.json));
  assert.ok(fs.existsSync(T(d1)), "主拓扑不应被删");
  assert.ok((await call("GET", "/api/projects")).json.some((x) => x.id === P.id), "工程记录应保留");
  fs.rmSync(T(d1, ".bak"));
  assert.equal((await call("DELETE", `/api/projects/${P.id}`)).status, 200);
  // 2. 归属检查之后才被换成非法 UTF-8
  const d2 = workDir();
  const Q = (await create(call, "坏备份二", d2)).json;
  assert.equal(a2aRaw(b, Q, ["topology", "add-edge", "x_ping", "dv", "sw", "{ip} ping"]).code, 0);
  const deleting = call("DELETE", `/api/projects/${Q.id}`);
  for (let i = 0; i < 50 && !b.output().includes(`测试:删除停顿 ${Q.id}`); i++) await sleep(100);
  fs.writeFileSync(T(d2, ".bak"), garbage);
  const del2 = await deleting;
  assert.equal(del2.status, 200, JSON.stringify(del2.json));
  assert.equal(del2.json.warnings.length, 1);
  assert.ok(!fs.existsSync(T(d2)), "主拓扑应已删除");
  assert.ok(fs.existsSync(T(d2, ".bak")), ".bak 应保留");
  assert.ok(!(await call("GET", "/api/projects")).json.some((x) => x.id === Q.id), "工程记录应已删除");
});
