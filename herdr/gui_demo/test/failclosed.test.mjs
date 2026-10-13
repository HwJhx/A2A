// Codex 审核第 0+1 步的阻断 2、应修 1–3:查询 / 清理失败时必须 fail-closed,不能在会话仍在时删状态目录;
// 启动失败要回收资源;后端被 kill -9 后遗留的 broker 要能被接管。
// 用 test/fakes 下的 herdr、tmux 包装注入失败,其余调用走真实的 herdr 与 tmux。
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { after, test } from "node:test";
import { api, cleanupIds, herdrSessionNames, login, PREFIX, ROOT, sh, startBackend, stopBackend, tempHome, tmuxNames, waitExit } from "./helpers.mjs";

const FAKES = path.join(ROOT, "test/fakes");
const REAL = { GD_REAL_HERDR: sh("bash", ["-lc", "command -v herdr"]).out.trim(), GD_REAL_TMUX: sh("bash", ["-lc", "command -v tmux"]).out.trim() };
const backends = [];
const ids = [];
const dirs = [];
after(async () => {
  for (const b of backends) await stopBackend(b);
  cleanupIds(ids);
  for (const d of dirs) fs.rmSync(d, { recursive: true, force: true });
});

function workDir() {
  const d = fs.mkdtempSync(path.join(os.homedir(), "gd-test-proj-"));
  dirs.push(d);
  return d;
}

async function faked(opts = {}) {
  const ctl = path.join(tempHome(), "fail.ctl");
  const b = await startBackend({ ...opts, env: { GUI_DEMO_HERDR: path.join(FAKES, "herdr"), GUI_DEMO_TMUX: path.join(FAKES, "tmux"),
    GD_FAKE_CTL: ctl, ...REAL, ...(opts.env ?? {}) } });
  backends.push(b);
  assert.ok(b.url, b.output());
  const { cookie } = await login(b);
  return { b, ctl, fail: (...pats) => fs.writeFileSync(ctl, pats.join("\n")), call: (m, p, body) => api(b, cookie, m, p, body) };
}

const brokerPids = (session) => sh("pgrep", ["-f", `a2a.cli broker run --session ${session}$`]).out.split("\n").filter(Boolean).map(Number);

test("删除工程时 herdr session list 失败:报错,工程与状态目录保留,会话仍在;恢复后可删", async () => {
  const { b, fail, call } = await faked();
  const P = await call("POST", "/api/projects", { name: "列表失败", path: workDir() });
  assert.equal(P.status, 201, JSON.stringify(P.json));
  ids.push(P.json.id);
  fail("session list");
  const del = await call("DELETE", `/api/projects/${P.json.id}`);
  assert.equal(del.status, 500, JSON.stringify(del.json));
  assert.ok(fs.existsSync(path.join(b.home, "projects", P.json.id, "state", "topology.yaml")), "状态目录必须保留");
  fail();
  assert.ok(herdrSessionNames().includes(P.json.session));
  assert.deepEqual((await call("GET", "/api/projects")).json.map((p) => p.id), [P.json.id]);
  assert.equal((await call("DELETE", `/api/projects/${P.json.id}`)).status, 200);
  assert.ok(!herdrSessionNames().includes(P.json.session));
});

test("删除工程时 herdr session stop / delete 失败:报错,状态目录保留", async () => {
  const { b, fail, call } = await faked();
  const P = await call("POST", "/api/projects", { name: "停止失败", path: workDir() });
  assert.equal(P.status, 201);
  ids.push(P.json.id);
  fail("session stop", "session delete");
  assert.equal((await call("DELETE", `/api/projects/${P.json.id}`)).status, 500);
  assert.ok(fs.existsSync(path.join(b.home, "projects", P.json.id, "state")));
  fail();
  assert.equal((await call("DELETE", `/api/projects/${P.json.id}`)).status, 200);
});

test("新建回滚时停不掉 herdr 会话:不删状态目录,并报告回滚未完成", async () => {
  const { b, fail, call } = await faked({ env: { GUI_DEMO_FAIL_AT: "broker" } });
  // herdr 起来之后、回滚之前让 stop / delete 失败:在 broker 步骤注入失败,stop 已被包装拦截
  fail("session stop", "session delete");
  const r = await call("POST", "/api/projects", { name: "回滚失败", path: workDir() });
  assert.equal(r.status, 500);
  assert.match(r.json.error, /回滚未完成/);
  const left = fs.readdirSync(path.join(b.home, "projects"));
  assert.equal(left.length, 1, "状态目录应保留");
  ids.push(left[0]);
  assert.ok(herdrSessionNames().includes(`${PREFIX}${left[0]}`));
  fail();
});

test("删除工程时 tmux 查询失败:报错,状态目录保留", async () => {
  const { b, fail, call } = await faked();
  const P = await call("POST", "/api/projects", { name: "tmux失败", path: workDir() });
  assert.equal(P.status, 201);
  ids.push(P.json.id);
  fail("list-sessions");
  assert.equal((await call("DELETE", `/api/projects/${P.json.id}`)).status, 500);
  assert.ok(fs.existsSync(path.join(b.home, "projects", P.json.id, "state")));
  fail();
  assert.equal((await call("DELETE", `/api/projects/${P.json.id}`)).status, 200);
});

test("项目路径经软链接指向家目录外:拒绝,且不在外部创建目录", async () => {
  const { call } = await faked();
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), "gd-outside-"));
  dirs.push(outside);
  const link = path.join(workDir(), "link");
  fs.symlinkSync(outside, link);
  const r = await call("POST", "/api/projects", { name: "外部", path: path.join(link, "newdir") });
  assert.equal(r.status, 400, JSON.stringify(r.json));
  assert.ok(!fs.existsSync(path.join(outside, "newdir")), "不应在家目录外创建目录");
});

test("HTTP 端口被占:启动失败退出,不留下 broker", async () => {
  const first = await startBackend({ env: {} });
  backends.push(first);
  const { cookie } = await login(first);
  const P = await api(first, cookie, "POST", "/api/projects", { name: "端口被占", path: workDir() });
  assert.equal(P.status, 201);
  ids.push(P.json.id);
  await stopBackend(first);
  assert.deepEqual(brokerPids(P.json.session), []);
  const blocker = net.createServer().listen(first.port, "127.0.0.1");
  await new Promise((r) => blocker.once("listening", r));
  try {
    const second = await startBackend({ home: first.home, port: first.port });
    backends.push(second);
    assert.equal(second.url, null);
    assert.notEqual(await waitExit(second.child), 0);
    await new Promise((r) => setTimeout(r, 500));
    assert.deepEqual(brokerPids(P.json.session), [], "启动失败不应留下 broker");
  } finally {
    blocker.close();
  }
  const third = await startBackend({ home: first.home });
  backends.push(third);
  const c3 = (await login(third)).cookie;
  assert.equal((await api(third, c3, "DELETE", `/api/projects/${P.json.id}`)).status, 200);
});

test("后端被 kill -9 后重启:接管遗留的 broker,只剩一个新 broker 在运行", async () => {
  const first = await startBackend();
  backends.push(first);
  const { cookie } = await login(first);
  const P = await api(first, cookie, "POST", "/api/projects", { name: "遗留broker", path: workDir() });
  assert.equal(P.status, 201);
  ids.push(P.json.id);
  const [orphan] = brokerPids(P.json.session);
  assert.ok(orphan);
  first.child.kill("SIGKILL");
  await waitExit(first.child);
  assert.deepEqual(brokerPids(P.json.session), [orphan], "kill -9 后 broker 成了孤儿");
  const second = await startBackend({ home: first.home });
  backends.push(second);
  const c2 = (await login(second)).cookie;
  await new Promise((r) => setTimeout(r, 4000)); // 若新 broker 因锁冲突退出,3 秒后会重启
  const list = (await api(second, c2, "GET", "/api/projects")).json;
  assert.equal(list[0].broker.running, true);
  assert.equal(list[0].broker.restarts, 0, "新 broker 不应反复退出重启");
  assert.deepEqual(brokerPids(P.json.session), [list[0].broker.pid]);
  assert.equal((await api(second, c2, "DELETE", `/api/projects/${P.json.id}`)).status, 200);
});

void tmuxNames;

test("同工程有手动启动的 broker(身份对不上):不回收它,GUI 的 broker 不启动并报告", async () => {
  const first = await startBackend();
  backends.push(first);
  const { cookie } = await login(first);
  const P = await api(first, cookie, "POST", "/api/projects", { name: "手动broker", path: workDir() });
  assert.equal(P.status, 201);
  ids.push(P.json.id);
  await stopBackend(first);
  // 用与 GUI 相同的环境手动起一个 broker,但没有 GUI 的身份标记
  const state = path.join(first.home, "projects", P.json.id, "state");
  const env = { ...process.env, A2A_STATE_DIR: state, A2A_TOPOLOGY: path.join(state, "topology.yaml"), HERDR_SESSION: P.json.session,
    PYTHONPATH: process.env.GUI_DEMO_A2A_SRC ?? "/Users/jhx/Documents/code/personal/pi_agent/A2A/herdr/a2a/src", PYTHONDONTWRITEBYTECODE: "1" };
  delete env.GUI_DEMO_BROKER_ID;
  const manual = spawn("python3", ["-m", "a2a.cli", "broker", "run", "--session", P.json.session], { env, stdio: "ignore" });
  try {
    await new Promise((r) => setTimeout(r, 1500));
    assert.deepEqual(brokerPids(P.json.session), [manual.pid]);
    const second = await startBackend({ home: first.home });
    backends.push(second);
    assert.match(second.output(), /身份不明的 broker/);
    const c2 = (await login(second)).cookie;
    const list = (await api(second, c2, "GET", "/api/projects")).json;
    assert.equal(list[0].broker.running, false);
    assert.equal(manual.exitCode, null, "手动启动的 broker 不应被停掉");
    assert.deepEqual(brokerPids(P.json.session), [manual.pid]);
  } finally {
    manual.kill("SIGTERM");
    await waitExit(manual).catch(() => manual.kill("SIGKILL"));
  }
});
