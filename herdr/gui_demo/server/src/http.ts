// HTTP 与 WebSocket 入口。免 cookie 的只有:静态页面文件(只读、不含数据)和 POST /api/login。
import fs from "node:fs";
import http, { IncomingMessage, ServerResponse } from "node:http";
import path from "node:path";
import type { Duplex } from "node:stream";
import { fileURLToPath } from "node:url";
import { authed, originOk, redeem, sessionCookie } from "./auth.js";
import { addAgent, agentAction, deleteIp, listAgents } from "./agents.js";
import { HOME } from "./config.js";
import { createProject, deleteProject, getProject, listProjects, UserError } from "./projects.js";

const WEB = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../web");
const STATIC: Record<string, [string, string]> = {
  "/": ["index.html", "text/html; charset=utf-8"],
  "/index.html": ["index.html", "text/html; charset=utf-8"],
  "/main.js": ["dist/main.js", "text/javascript; charset=utf-8"],
  "/main.css": ["dist/main.css", "text/css; charset=utf-8"],
};
const BASE_HEADERS = {
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
  "Cache-Control": "no-store",
  "Content-Security-Policy": "default-src 'self'; connect-src 'self'; frame-ancestors 'none'",
};

function send(res: ServerResponse, status: number, body?: unknown, headers: Record<string, string> = {}): void {
  const text = body === undefined ? "" : JSON.stringify(body);
  res.writeHead(status, { ...BASE_HEADERS, ...(text ? { "Content-Type": "application/json; charset=utf-8" } : {}), ...headers });
  res.end(text);
}

function readJson(req: IncomingMessage): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => {
      size += c.length;
      if (size > 64 * 1024) {
        reject(new UserError("请求体太大", 413));
        req.destroy();
      } else chunks.push(c);
    });
    req.on("end", () => {
      try {
        const v = chunks.length ? JSON.parse(Buffer.concat(chunks).toString("utf8")) : {};
        if (typeof v !== "object" || v === null || Array.isArray(v)) throw new Error();
        resolve(v as Record<string, unknown>);
      } catch {
        reject(new UserError("请求体必须是 JSON 对象"));
      }
    });
    req.on("error", reject);
  });
}

async function handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
  const url = new URL(req.url ?? "/", "http://x");
  const method = req.method ?? "GET";

  if (method === "GET" && STATIC[url.pathname]) {
    const [file, type] = STATIC[url.pathname];
    try {
      const data = fs.readFileSync(path.join(WEB, file));
      res.writeHead(200, { ...BASE_HEADERS, "Content-Type": type });
      res.end(data);
    } catch {
      send(res, 404, { error: "页面文件不存在(没有构建?)" });
    }
    return;
  }

  const mutating = method !== "GET" && method !== "HEAD";
  if (mutating) {
    if (!originOk(req)) return send(res, 403, { error: "来源(Origin)不对" });
    if (!(req.headers["content-type"] ?? "").startsWith("application/json")) return send(res, 415, { error: "只接受 application/json" });
  }

  if (url.pathname === "/api/login" && method === "POST") {
    const body = await readJson(req);
    const sid = redeem(body.token);
    if (!sid) return send(res, 401, { error: "登录令牌无效或已用过;在 VM 上运行 gui_demo/bin/login-url 获取新地址" });
    return send(res, 204, undefined, { "Set-Cookie": sessionCookie(sid) });
  }

  if (!authed(req)) return send(res, 401, { error: "未登录" });

  if (url.pathname === "/api/me" && method === "GET") return send(res, 200, { ok: true });
  if (url.pathname === "/api/config" && method === "GET") return send(res, 200, { home: HOME });
  if (url.pathname === "/api/projects" && method === "GET") return send(res, 200, await listProjects());
  if (url.pathname === "/api/projects" && method === "POST") {
    const body = await readJson(req);
    return send(res, 201, await createProject(body));
  }
  const m = url.pathname.match(/^\/api\/projects\/(p[0-9a-f]{8})$/);
  if (m && method === "GET") return send(res, 200, getProject(m[1]));
  if (m && method === "DELETE") return send(res, 200, await deleteProject(m[1]));

  const ag = url.pathname.match(/^\/api\/projects\/(p[0-9a-f]{8})\/agents$/);
  if (ag && method === "GET") return send(res, 200, await listAgents(ag[1]));
  if (ag && method === "POST") return send(res, 201, await addAgent(ag[1], await readJson(req)));
  const act = url.pathname.match(/^\/api\/projects\/(p[0-9a-f]{8})\/agents\/([a-z0-9_-]{1,40})\/(stop|restore)$/);
  if (act && method === "POST") return send(res, 200, await agentAction(act[1], act[2], act[3] as "stop" | "restore"));
  const ipm = url.pathname.match(/^\/api\/projects\/(p[0-9a-f]{8})\/ips\/([a-z0-9_-]{1,40})$/);
  if (ipm && method === "DELETE") return send(res, 200, await deleteIp(ipm[1], ipm[2], await readJson(req)));
  return send(res, 404, { error: "没有这个接口" });
}

function rejectUpgrade(socket: Duplex, status: number, text: string): void {
  socket.end(`HTTP/1.1 ${status} ${text}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n`);
}

export function createHttpServer(): http.Server {
  const server = http.createServer((req, res) => {
    handle(req, res).catch((e: unknown) => {
      const status = e instanceof UserError ? e.status : 500;
      const body: Record<string, unknown> = { error: (e as Error).message };
      if (e instanceof UserError && e.step) body.step = e.step;
      if (!res.headersSent) send(res, status, body);
      else res.end();
    });
  });
  // WebSocket(终端,第 4 步实现):握手前先校验会话 cookie 与 Origin
  server.on("upgrade", (req, socket) => {
    if (!originOk(req)) return rejectUpgrade(socket, 403, "Forbidden");
    if (!authed(req)) return rejectUpgrade(socket, 401, "Unauthorized");
    rejectUpgrade(socket, 404, "Not Found");
  });
  return server;
}
