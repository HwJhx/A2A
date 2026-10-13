// 第 1 步:登录、会话 cookie、Origin、WebSocket 握手、login-url、单实例锁与 admin.sock(21 号文档 §5)。
import assert from "node:assert/strict";
import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import { after, test } from "node:test";
import { api, freePort, login, loginUrlCli, startBackend, stopBackend, tempHome, tokenOf, waitExit, wsHandshake } from "./helpers.mjs";

const backends = [];
after(async () => { for (const b of backends) await stopBackend(b); });
const start = async (opts) => { const b = await startBackend(opts); backends.push(b); return b; };

test("免 cookie 的只有静态页面与 /api/login;其余 401", async () => {
  const b = await start();
  assert.ok(b.url, b.output());
  assert.equal((await fetch(`${b.origin}/`)).status, 200);
  assert.equal((await fetch(`${b.origin}/api/projects`)).status, 401);
  assert.equal((await fetch(`${b.origin}/api/me`)).status, 401);
  assert.equal((await fetch(`${b.origin}/api/projects`, { headers: { Cookie: "gd_sid=forged" } })).status, 401);
  const r = await fetch(`${b.origin}/`);
  assert.equal(r.headers.get("referrer-policy"), "no-referrer");
});

test("登录:错令牌 401;正确令牌换 cookie;同一令牌再用 401", async () => {
  const b = await start();
  assert.equal((await login(b, "x".repeat(43))).status, 401);
  const ok = await login(b);
  assert.equal(ok.status, 204);
  assert.match(ok.cookie, /^gd_sid=/);
  assert.equal((await api(b, ok.cookie, "GET", "/api/me")).status, 200);
  assert.equal((await login(b)).status, 401);
});

test("修改类请求:没有或错误的 Origin 403,非 JSON 415", async () => {
  const b = await start();
  const { cookie } = await login(b);
  const post = (headers) => fetch(`${b.origin}/api/projects`, { method: "POST", headers: { Cookie: cookie, ...headers }, body: "{}" });
  assert.equal((await post({ "Content-Type": "application/json" })).status, 403);
  assert.equal((await post({ "Content-Type": "application/json", Origin: "http://evil.example" })).status, 403);
  assert.equal((await post({ "Content-Type": "text/plain", Origin: b.origin })).status, 415);
  const loginNoOrigin = await fetch(`${b.origin}/api/login`, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: tokenOf(b.url) }) });
  assert.equal(loginNoOrigin.status, 403);
});

test("WebSocket 握手:无 cookie、错 cookie、错 Origin 都被拒", async () => {
  const b = await start();
  const { cookie } = await login(b);
  assert.match(await wsHandshake(b, { Origin: b.origin }), / 401 /);
  assert.match(await wsHandshake(b, { Origin: b.origin, Cookie: "gd_sid=forged" }), / 401 /);
  assert.match(await wsHandshake(b, { Origin: "http://evil.example", Cookie: cookie }), / 403 /);
});

test("并发 20 个请求兑换同一令牌,恰好 1 个成功", async () => {
  const b = await start();
  const results = await Promise.all(Array.from({ length: 20 }, () => login(b)));
  assert.equal(results.filter((r) => r.status === 204).length, 1);
  assert.equal(results.filter((r) => r.status === 401).length, 19);
});

test("login-url:新地址能登录,之前未用的令牌失效;后端不在时报错且不输出令牌", async () => {
  const b = await start();
  const r = loginUrlCli(b.home);
  assert.equal(r.code, 0, r.stderr);
  assert.match(r.stdout, /#token=/);
  assert.equal((await login(b)).status, 401, "启动时打印的令牌应已作废");
  assert.equal((await login(b, tokenOf(r.stdout.trim()))).status, 204);
  await stopBackend(b);
  assert.ok(!fs.existsSync(path.join(b.home, "admin.sock")), "正常退出应删除 admin.sock");
  const gone = loginUrlCli(b.home);
  assert.notEqual(gone.code, 0);
  assert.equal(gone.stdout, "");
  assert.doesNotMatch(gone.stderr, /token=/);
});

test("单实例:已有后端时再启动报错退出,不删正在用的 admin.sock", async () => {
  const a = await start();
  const second = await start({ home: a.home });
  assert.equal(second.url, null);
  assert.notEqual(await waitExit(second.child), 0);
  assert.match(second.output(), /已有一个后端/);
  assert.equal(loginUrlCli(a.home).code, 0, "第一个后端的 admin.sock 仍可用");
});

test("kill -9 留下陈旧 admin.sock 后能立刻再启动", async () => {
  const a = await start();
  a.child.kill("SIGKILL");
  await waitExit(a.child);
  assert.ok(fs.lstatSync(path.join(a.home, "admin.sock")).isSocket(), "kill -9 后应留下陈旧 socket");
  const b = await start({ home: a.home });
  assert.ok(b.url, b.output());
  assert.equal(loginUrlCli(a.home).code, 0);
});

test("有陈旧 socket 时两个后端同时启动:恰好一个成功,admin.sock 可用", async () => {
  const a = await start();
  a.child.kill("SIGKILL");
  await waitExit(a.child);
  const port = await freePort();
  const [x, y] = await Promise.all([start({ home: a.home, port }), start({ home: a.home, port })]);
  const ok = [x, y].filter((b) => b.url);
  assert.equal(ok.length, 1, x.output() + y.output());
  const loser = [x, y].find((b) => !b.url);
  assert.notEqual(await waitExit(loser.child), 0);
  const r = loginUrlCli(a.home);
  assert.equal(r.code, 0, r.stderr);
  assert.equal((await login(ok[0], tokenOf(r.stdout.trim()))).status, 204);
});

test("admin.sock 路径被普通文件占用:报错退出,不删该文件", async () => {
  const home = tempHome();
  fs.writeFileSync(path.join(home, "admin.sock"), "keep me");
  const b = await start({ home });
  assert.equal(b.url, null);
  assert.notEqual(await waitExit(b.child), 0);
  assert.match(b.output(), /不是 socket/);
  assert.equal(fs.readFileSync(path.join(home, "admin.sock"), "utf8"), "keep me");
});

test("admin.sock 权限 0600,数据目录 0700", async () => {
  const b = await start();
  assert.equal(fs.statSync(path.join(b.home, "admin.sock")).mode & 0o777, 0o600);
  assert.equal(fs.statSync(b.home).mode & 0o777, 0o700);
  // 未知请求不给令牌
  const reply = await new Promise((resolve) => {
    const c = net.connect(path.join(b.home, "admin.sock"), () => c.write("hello\n"));
    let out = "";
    c.on("data", (d) => (out += d));
    c.on("end", () => resolve(out));
  });
  assert.doesNotMatch(reply, /token=/);
});

test("经软链接指向同一数据目录的第二个后端:同样被单实例锁挡住", async () => {
  const a = await start();
  const link = `${tempHome()}/link`;
  fs.symlinkSync(a.home, link);
  const b = await start({ home: link });
  assert.equal(b.url, null);
  assert.notEqual(await waitExit(b.child), 0);
  assert.match(b.output(), /已有一个后端/);
  assert.equal(loginUrlCli(a.home).code, 0);
});

test("admin.sock 上有别的进程在监听(没拿锁也绕不过):报错退出,不删它", async () => {
  const home = tempHome();
  const sock = path.join(home, "admin.sock");
  const foreign = net.createServer((c) => c.end()).listen(sock);
  await new Promise((r) => foreign.once("listening", r));
  try {
    const b = await start({ home });
    assert.equal(b.url, null);
    assert.notEqual(await waitExit(b.child), 0);
    assert.match(b.output(), /有服务在监听/);
    assert.ok(fs.lstatSync(sock).isSocket(), "不应删除正在监听的 socket");
  } finally {
    foreign.close();
  }
});
