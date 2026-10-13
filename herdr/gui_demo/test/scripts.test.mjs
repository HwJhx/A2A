// 第 5 步:bin/start、bin/stop。pid 文件核对身份(陈旧 pid 指向无关进程时绝不发信号);就绪超时会结束后台进程。
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { after, test } from "node:test";
import { api, cleanupIds, freePort, login, PREFIX, ROOT, sh, tempHome } from "./helpers.mjs";

const ids = [];
const dirs = [];
const homes = [];
after(() => {
  for (const home of homes) script("stop", home);
  cleanupIds(ids);
  for (const d of dirs) fs.rmSync(d, { recursive: true, force: true });
});

function script(name, home, extra = {}) {
  // 标准输出与标准错误都收(提示信息有的写在标准错误里)
  const r = spawnSync(path.join(ROOT, "bin", name), [], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"],
    env: { ...process.env, GUI_DEMO_HOME: home, GUI_DEMO_HOST: "127.0.0.1", GUI_DEMO_SESSION_PREFIX: PREFIX, ...extra } });
  return { code: r.status ?? 1, out: `${r.stdout ?? ""}${r.stderr ?? ""}` };
}
const startTime = (pid) => fs.readFileSync(`/proc/${pid}/stat`, "utf8").replace(/^.*\) /, "").split(" ")[19];
const alive = (pid) => { try { process.kill(pid, 0); return true; } catch { return false; } };
const backendProcs = (home) => sh("pgrep", ["-f", "server/dist/main.js"]).out.split("\n").filter(Boolean)
  .filter((pid) => { try { return fs.readFileSync(`/proc/${pid}/environ`, "utf8").includes(`GUI_DEMO_HOME=${home}`); } catch { return false; } });

test("陈旧 pid 文件指向仍在运行的无关进程:stop 不发信号、start 照常启动", async () => {
  const home = tempHome();
  homes.push(home);
  const port = String(await freePort());
  const other = spawn("sleep", ["300"], { stdio: "ignore" });
  try {
    for (const content of [`${other.pid} ${startTime(other.pid)}`, `${other.pid} 12345`]) { // 启动时间对得上(命令行不对)/ 对不上
      fs.writeFileSync(path.join(home, "server.pid"), content + "\n");
      const stop = script("stop", home);
      assert.equal(stop.code, 0, stop.out);
      assert.match(stop.out, /陈旧/);
      assert.ok(alive(other.pid), "无关进程不应被发信号");
      assert.ok(!fs.existsSync(path.join(home, "server.pid")));
    }
    fs.writeFileSync(path.join(home, "server.pid"), `${other.pid} ${startTime(other.pid)}\n`);
    const start = script("start", home, { GUI_DEMO_PORT: port });
    assert.equal(start.code, 0, start.out);
    assert.match(start.out, /陈旧/);
    assert.match(start.out, /登录地址/);
    assert.ok(alive(other.pid));
    const again = script("start", home, { GUI_DEMO_PORT: port });
    assert.equal(again.code, 1, "已在运行时再启动应拒绝");
    const stop = script("stop", home);
    assert.match(stop.out, /已停止/);
    assert.deepEqual(backendProcs(home), []);
    assert.ok(alive(other.pid), "无关进程始终不受影响");
  } finally {
    other.kill();
  }
});

test("就绪超时(1 秒,有工程要拉 broker 所以启动要 2 秒以上):脚本报失败并结束后台进程,不留 pid 文件、后端与 broker", async () => {
  const home = tempHome();
  homes.push(home);
  const port = String(await freePort());
  const first = script("start", home, { GUI_DEMO_PORT: port });
  assert.equal(first.code, 0, first.out);
  const url = first.out.match(/登录地址\(一次性\):(\S+)/)[1];
  const b = { origin: `http://127.0.0.1:${port}`, url };
  const { cookie } = await login(b);
  const work = fs.mkdtempSync(path.join(os.homedir(), "gd-test-proj-"));
  dirs.push(work);
  const P = (await api(b, cookie, "POST", "/api/projects", { name: "慢启动", path: work })).json;
  assert.ok(P.id, JSON.stringify(P));
  ids.push(P.id);
  assert.match(script("stop", home).out, /已停止/);

  const slow = script("start", home, { GUI_DEMO_PORT: port, GUI_DEMO_START_TIMEOUT: "1" });
  assert.equal(slow.code, 1, slow.out);
  assert.match(slow.out, /没有就绪/);
  assert.ok(!fs.existsSync(path.join(home, "server.pid")), "超时后 pid 文件应已删除");
  await new Promise((r) => setTimeout(r, 500));
  assert.deepEqual(backendProcs(home), [], "超时后后端进程应已结束");
  assert.equal(sh("pgrep", ["-f", `a2a.cli broker run --session ${P.session}$`]).out.trim(), "", "不应留下 broker");

  // 清理:正常启动后删工程
  const ok = script("start", home, { GUI_DEMO_PORT: port });
  assert.equal(ok.code, 0, ok.out);
  const b2 = { origin: `http://127.0.0.1:${port}`, url: ok.out.match(/登录地址\(一次性\):(\S+)/)[1] };
  const c2 = (await login(b2)).cookie;
  assert.equal((await api(b2, c2, "DELETE", `/api/projects/${P.id}`)).status, 200);
  assert.match(script("stop", home).out, /已停止/);
});
