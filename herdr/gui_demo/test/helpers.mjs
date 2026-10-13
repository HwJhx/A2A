// 测试辅助:每个后端用独立的临时数据目录、127.0.0.1 上的随机端口、guidemo_test_ 前缀。
import { execFileSync, spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
export const PREFIX = "guidemo_test_";

export function freePort() {
  return new Promise((resolve) => {
    const s = net.createServer().listen(0, "127.0.0.1", () => {
      const { port } = s.address();
      s.close(() => resolve(port));
    });
  });
}

const tempDirs = [];
process.on("exit", () => { for (const d of tempDirs) fs.rmSync(d, { recursive: true, force: true }); });

export function tempHome() {
  const d = fs.mkdtempSync(path.join(os.tmpdir(), "gd-test-"));
  tempDirs.push(d);
  return d;
}

/** 启动后端,等到打印登录地址(或退出)。返回 { child, port, origin, url, home, output() }。 */
export async function startBackend({ home = tempHome(), port, env = {} } = {}) {
  port ??= await freePort();
  const child = spawn(process.execPath, [path.join(ROOT, "server/dist/main.js")], {
    env: { ...process.env, GUI_DEMO_HOME: home, GUI_DEMO_HOST: "127.0.0.1", GUI_DEMO_PORT: String(port),
      GUI_DEMO_SESSION_PREFIX: PREFIX, ...env },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let out = "";
  child.stdout.on("data", (d) => (out += d));
  child.stderr.on("data", (d) => (out += d));
  const origin = `http://127.0.0.1:${port}`;
  const url = await new Promise((resolve) => {
    const timer = setInterval(() => {
      const m = out.match(/登录地址\(一次性\):(\S+)/);
      if (m) { clearInterval(timer); resolve(m[1]); }
      if (child.exitCode !== null) { clearInterval(timer); resolve(null); }
    }, 100);
  });
  return { child, port, origin, url, home, output: () => out };
}

export function waitExit(child, ms = 15000) {
  if (child.exitCode !== null || child.signalCode !== null) return Promise.resolve(child.exitCode);
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error("进程没有在时限内退出")), ms);
    child.once("exit", (code) => { clearTimeout(t); resolve(code); });
  });
}

export async function stopBackend(b) {
  if (b.child.exitCode === null && b.child.signalCode === null) {
    b.child.kill("SIGTERM");
    await waitExit(b.child, 20000).catch(() => b.child.kill("SIGKILL"));
  }
}

export const tokenOf = (url) => url.split("#token=")[1];

export async function login(b, token = tokenOf(b.url)) {
  const r = await fetch(`${b.origin}/api/login`, {
    method: "POST", headers: { "Content-Type": "application/json", Origin: b.origin }, body: JSON.stringify({ token }),
  });
  const cookie = (r.headers.get("set-cookie") ?? "").split(";")[0];
  return { status: r.status, cookie };
}

export async function api(b, cookie, method, p, body) {
  const headers = { Cookie: cookie };
  if (method !== "GET") Object.assign(headers, { Origin: b.origin, "Content-Type": "application/json" });
  const r = await fetch(`${b.origin}${p}`, { method, headers, body: method === "GET" ? undefined : JSON.stringify(body ?? {}) });
  const text = await r.text();
  let json;
  try { json = text ? JSON.parse(text) : undefined; } catch { json = text; }
  return { status: r.status, json };
}

export function loginUrlCli(home) {
  try {
    const out = execFileSync(process.execPath, [path.join(ROOT, "bin/login-url")], {
      env: { ...process.env, GUI_DEMO_HOME: home }, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
    return { code: 0, stdout: out, stderr: "" };
  } catch (e) {
    return { code: e.status ?? 1, stdout: String(e.stdout ?? ""), stderr: String(e.stderr ?? "") };
  }
}

/** 原始 HTTP 升级请求,返回状态行。 */
export function wsHandshake(b, headers) {
  return new Promise((resolve, reject) => {
    const s = net.connect(b.port, "127.0.0.1", () => {
      const lines = ["GET /ws/term HTTP/1.1", `Host: 127.0.0.1:${b.port}`, "Upgrade: websocket", "Connection: Upgrade",
        "Sec-WebSocket-Version: 13", "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
        ...Object.entries(headers).map(([k, v]) => `${k}: ${v}`), "", ""];
      s.write(lines.join("\r\n"));
    });
    let data = "";
    s.on("data", (d) => { data += d; if (data.includes("\r\n")) { s.destroy(); resolve(data.split("\r\n")[0]); } });
    s.on("error", reject);
    s.setTimeout(5000, () => { s.destroy(); reject(new Error("握手无响应")); });
  });
}

export function sh(cmd, args) {
  try {
    return { code: 0, out: execFileSync(cmd, args, { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }) };
  } catch (e) {
    return { code: e.status ?? 1, out: String(e.stdout ?? "") + String(e.stderr ?? "") };
  }
}

export function herdrSessionNames() {
  return sh("herdr", ["session", "list"]).out.split("\n").map((l) => l.trim().split(/\s+/)[0]).filter(Boolean);
}

export function tmuxNames() {
  const r = sh("tmux", ["list-sessions", "-F", "#{session_name}"]);
  return r.code === 0 ? r.out.split("\n").filter(Boolean) : [];
}

/** 兜底清理本次测试留下的 guidemo_test_ 会话(只清名字里带给定工程 id 的)。 */
export function cleanupIds(ids) {
  for (const id of ids) {
    for (const t of tmuxNames()) if (t.startsWith(PREFIX) && t.includes(id)) sh("tmux", ["kill-session", "-t", `=${t}`]);
    const s = `${PREFIX}${id}`;
    if (herdrSessionNames().includes(s)) { sh("herdr", ["session", "stop", s]); sh("herdr", ["session", "delete", s]); }
  }
}
