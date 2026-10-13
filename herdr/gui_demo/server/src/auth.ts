// 一次性登录令牌 + 会话 cookie(21 号文档 §5)。令牌只在内存里。
// 兑换与换新都是同步代码(中间没有 await):Node 单线程,并发兑换至多一个成功。
import crypto from "node:crypto";
import type { IncomingMessage } from "node:http";
import { ORIGIN } from "./config.js";

const COOKIE = "gd_sid";
let loginToken: string | null = null;
// 存会话 id 的 SHA-256,查表不会因比较时长泄露会话 id
const sessions = new Set<string>();

const random = () => crypto.randomBytes(32).toString("base64url");
const digest = (s: string) => crypto.createHash("sha256").update(s).digest("hex");

/** 生成新的一次性令牌,之前未用的随之作废。 */
export function issueLoginToken(): string {
  loginToken = random();
  return loginToken;
}

export const loginUrl = (token: string) => `${ORIGIN}/#token=${token}`;

/** 兑换:成功返回新会话 id,令牌作废;失败返回 null。 */
export function redeem(token: unknown): string | null {
  if (loginToken === null || typeof token !== "string") return null;
  const a = Buffer.from(token);
  const b = Buffer.from(loginToken);
  if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) return null;
  loginToken = null;
  const sid = random();
  sessions.add(digest(sid));
  return sid;
}

export const sessionCookie = (sid: string) => `${COOKIE}=${sid}; HttpOnly; SameSite=Strict; Path=/`;

function cookieValue(req: IncomingMessage): string | null {
  for (const part of (req.headers.cookie ?? "").split(";")) {
    const [k, ...v] = part.trim().split("=");
    if (k === COOKIE) return v.join("=");
  }
  return null;
}

export function authed(req: IncomingMessage): boolean {
  const sid = cookieValue(req);
  return !!sid && sessions.has(digest(sid));
}

export const originOk = (req: IncomingMessage) => req.headers.origin === ORIGIN;
