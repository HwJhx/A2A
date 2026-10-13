// 单实例锁与管理 socket(21 号文档 §5)。
//  * 单实例锁:Linux 抽象命名空间 Unix socket,不在磁盘上,进程退出(含 kill -9)时内核释放。
//    名字带数据目录的摘要:同一个数据目录只能有一个后端(测试用不同的数据目录互不影响)。
//  * admin.sock:login-url 连上来要新的登录地址。启动时只回收"本用户的、连不上的"陈旧 socket。
import crypto from "node:crypto";
import fs from "node:fs";
import net from "node:net";
import { ADMIN_SOCK, DATA_DIR } from "./config.js";
import { issueLoginToken, loginUrl } from "./auth.js";

export function acquireInstanceLock(): Promise<net.Server> {
  // 按真实路径取名:经软链接换个写法指向同一数据目录,也拿的是同一把锁
  fs.mkdirSync(DATA_DIR, { recursive: true, mode: 0o700 });
  const real = fs.realpathSync(DATA_DIR);
  const name = `\0a2a_gui_demo_${process.getuid!()}_${crypto.createHash("sha256").update(real).digest("hex").slice(0, 16)}`;
  return new Promise((resolve, reject) => {
    const server = net.createServer((c) => c.destroy());
    server.once("error", (e: NodeJS.ErrnoException) =>
      reject(e.code === "EADDRINUSE" ? new Error(`已有一个后端在使用数据目录 ${DATA_DIR}`) : e));
    server.listen(name, () => resolve(server));
  });
}

function probe(sock: string): Promise<"alive" | "refused"> {
  return new Promise((resolve, reject) => {
    const c = net.connect(sock);
    c.once("connect", () => { c.destroy(); resolve("alive"); });
    c.once("error", (e: NodeJS.ErrnoException) => (e.code === "ECONNREFUSED" ? resolve("refused") : reject(e)));
  });
}

/** 必须在拿到单实例锁之后调用。 */
export async function startAdminSocket(): Promise<net.Server> {
  fs.mkdirSync(DATA_DIR, { recursive: true, mode: 0o700 });
  fs.chmodSync(DATA_DIR, 0o700);
  let st: fs.Stats | null = null;
  try {
    st = fs.lstatSync(ADMIN_SOCK);
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code !== "ENOENT") throw e;
  }
  if (st) {
    if (!st.isSocket()) throw new Error(`${ADMIN_SOCK} 已存在但不是 socket,不删除;请人工处理`);
    if (st.uid !== process.getuid!()) throw new Error(`${ADMIN_SOCK} 不属于当前用户,不删除`);
    if ((await probe(ADMIN_SOCK)) === "alive") throw new Error(`${ADMIN_SOCK} 有服务在监听,已有后端在运行`);
    fs.unlinkSync(ADMIN_SOCK); // 陈旧
  }
  const server = net.createServer((conn) => {
    conn.setEncoding("utf8");
    let buf = "";
    conn.on("data", (d) => {
      buf += d;
      if (!buf.includes("\n")) return;
      if (buf.trim() === "login-url") conn.end(loginUrl(issueLoginToken()) + "\n");
      else conn.end("error: 未知请求\n");
    });
    conn.on("error", () => undefined);
  });
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(ADMIN_SOCK, () => resolve());
  });
  fs.chmodSync(ADMIN_SOCK, 0o600);
  const ino = fs.statSync(ADMIN_SOCK).ino;
  server.on("close", () => {
    try {
      if (fs.statSync(ADMIN_SOCK).ino === ino) fs.unlinkSync(ADMIN_SOCK);
    } catch {
      /* 已不在 */
    }
  });
  return server;
}
