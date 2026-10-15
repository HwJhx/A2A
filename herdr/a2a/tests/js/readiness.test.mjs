// 22 号方案 §8.2:就绪状态机的确定性时序测试。用真实的 makeReporter(含重试与 valid 检查),
// 只把调用 herdr 的那一步换成可控的 Promise。运行:node --test tests/js/readiness.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const { makeReporter, makeReadiness } = await import(pathToFileURL(join(here, "..", "..", "pi-extension", "a2a.ts")).href);

/** 假 herdr:记录 [state, seq];默认立即成功。hold() 之后的调用挂起,直到 release(ok)。 */
function fakeHerdr() {
  const calls = [];
  const pending = [];
  let holding = false;
  let failNext = 0;
  const run = (args) => {
    const state = args[args.indexOf("--state") + 1];
    const seq = Number(args[args.indexOf("--seq") + 1]);
    calls.push([state, seq]);
    if (failNext > 0) { failNext -= 1; return Promise.resolve(false); }
    if (!holding) return Promise.resolve(true);
    return new Promise((resolve) => pending.push(resolve));
  };
  return {
    calls, run,
    states: () => calls.map(([s]) => s),
    hold() { holding = true; },
    release(ok = true) { holding = false; pending.splice(0).forEach((r) => r(ok)); },
    failNext(n) { failNext = n; },
  };
}

function setup() {
  const herdr = fakeHerdr();
  const logs = [];
  const reporter = makeReporter("herdr", "w1:p2", {}, (m) => logs.push(m), herdr.run);
  return { herdr, logs, r: makeReadiness(reporter) };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

test("空闲时命令:先 working,发送,结束报 idle", async () => {
  const { herdr, r } = setup();
  herdr.hold();
  let started = false;
  const p = r.commandStart().then((ok) => { started = ok; });
  await tick();
  assert.equal(started, false, "working 上报完成之前不能开始发送");
  herdr.release(true);
  await p;
  assert.equal(started, true);
  await r.commandEnd();
  assert.deepEqual(herdr.states(), ["working", "idle"]);
});

test("压缩中执行命令:命令结束不报 idle;之后确定的 agent_end 才报", async () => {
  const { herdr, r } = setup();
  await r.compactStart();
  assert.equal(await r.commandStart(), true);
  await r.commandEnd();
  assert.deepEqual(herdr.states(), ["working", "working"]);
  await r.agentStart();
  await r.agentEnd(true);
  assert.deepEqual(herdr.states(), ["working", "working", "working", "idle"]);
});

test("模型运行中开始命令、模型先结束:agent_end 推迟,命令结束时报 idle", async () => {
  const { herdr, r } = setup();
  await r.agentStart();
  await r.commandStart();
  await r.agentEnd(true);
  assert.deepEqual(herdr.states(), ["working", "working"], "命令在途时 agent_end 不报 idle");
  await r.commandEnd();
  assert.deepEqual(herdr.states(), ["working", "working", "idle"]);
});

test("命令在途时模型又开始:命令结束不报 idle", async () => {
  const { herdr, r } = setup();
  await r.commandStart();
  await r.agentStart();
  await r.commandEnd();
  assert.deepEqual(herdr.states(), ["working", "working"]);
  await r.agentEnd(true);
  assert.deepEqual(herdr.states(), ["working", "working", "idle"]);
});

test("两条命令重叠:共用一次 working,最后一条结束才报 idle", async () => {
  const { herdr, r } = setup();
  herdr.hold();
  const a = r.commandStart();
  const b = r.commandStart();
  await tick();
  assert.deepEqual(herdr.states(), ["working"]);
  herdr.release(true);
  assert.deepEqual([await a, await b], [true, true]);
  await r.commandEnd();
  assert.deepEqual(herdr.states(), ["working"]);
  await r.commandEnd();
  assert.deepEqual(herdr.states(), ["working", "idle"]);
});

test("idle 重试期间状态变化:放弃剩余的 idle 重试", async () => {
  const { herdr, logs, r } = setup();
  await r.agentStart();
  herdr.hold();
  const end = r.agentEnd(true);              // 第一次 idle 挂起
  await tick();
  const start = r.agentStart();              // 新的一轮开始:working
  await tick();
  herdr.release(false);                       // 第一次 idle 与 working 都"失败"返回……
  await Promise.all([end, start]);
  const states = herdr.states();
  assert.equal(states.filter((s) => s === "idle").length, 1, "状态变了就不再重试 idle");
  assert.ok(!logs.some((m) => m.includes("上报 idle 失败")), "放弃不算失败");
  // 序号:idle 的序号小于其后 working 的序号
  const idleSeq = herdr.calls.find(([s]) => s === "idle")[1];
  assert.ok(herdr.calls.filter(([s]) => s === "working").some(([, q]) => q > idleSeq));
});

test("agent_end 不确定:保持 working,命令结束也不报 idle", async () => {
  const { herdr, r } = setup();
  await r.agentStart();
  await r.agentEnd(false);
  await r.commandStart();
  await r.commandEnd();
  assert.deepEqual(herdr.states(), ["working", "working"]);
});

test("命令的 working 上报失败:不发送,并尝试回到 idle", async () => {
  const { herdr, logs, r } = setup();
  herdr.failNext(2);                          // working 两次都失败
  assert.equal(await r.commandStart(), false);
  assert.deepEqual(herdr.states(), ["working", "working", "idle"]);
  assert.ok(logs.some((m) => m.includes("上报 working 失败")));
  // 之后的命令正常
  assert.equal(await r.commandStart(), true);
  await r.commandEnd();
  assert.deepEqual(herdr.states().slice(-2), ["working", "idle"]);
});

test("模型运行中命令的 working 失败:不发送,也不报 idle", async () => {
  const { herdr, r } = setup();
  await r.agentStart();
  herdr.failNext(2);
  assert.equal(await r.commandStart(), false);
  assert.deepEqual(herdr.states(), ["working", "working", "working"]);
});
