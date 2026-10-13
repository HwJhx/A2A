// 后端入口。顺序:单实例锁 → admin.sock → HTTP 监听 → 恢复已有工程(拉起 broker)→ 打印登录地址。
// 先绑定 HTTP 再拉 broker:端口被占等启动失败时不会留下 broker;失败时按相反顺序回收已拿到的资源。
import type net from "node:net";
import type http from "node:http";
import { acquireInstanceLock, startAdminSocket } from "./admin.js";
import { issueLoginToken, loginUrl } from "./auth.js";
import { stopAllBrokers } from "./broker.js";
import { DATA_DIR, HOST, PORT, SHUTDOWN_DELAY_MS } from "./config.js";
import { sleep } from "./proc.js";
import { createHttpServer } from "./http.js";
import { resumeProjects } from "./projects.js";
import { closeAllTerminals } from "./terminal.js";

let lock: net.Server | null = null;
let admin: net.Server | null = null;
let server: http.Server | null = null;

async function release(): Promise<void> {
  closeAllTerminals();
  if (server) {
    server.close();
    server.closeAllConnections();
  }
  await stopAllBrokers();
  if (SHUTDOWN_DELAY_MS) await sleep(SHUTDOWN_DELAY_MS);
  admin?.close();
  lock?.close();
}

async function main(): Promise<void> {
  lock = await acquireInstanceLock();
  admin = await startAdminSocket();
  const s = createHttpServer();
  await new Promise<void>((resolve, reject) => {
    s.once("error", reject);
    s.listen(PORT, HOST, () => resolve());
  });
  server = s;
  for (const note of await resumeProjects()) console.log(`注意:${note}`);
  console.log(`数据目录:${DATA_DIR}`);
  console.log(`登录地址(一次性):${loginUrl(issueLoginToken())}`);

  let closing = false;
  const shutdown = async (signal: string) => {
    if (closing) return;
    closing = true;
    console.log(`收到 ${signal},停止 broker 后退出(herdr 会话与 agent 保留)`);
    await release();
    process.exit(0);
  };
  process.on("SIGINT", () => void shutdown("SIGINT"));
  process.on("SIGTERM", () => void shutdown("SIGTERM"));
}

main().catch(async (e: unknown) => {
  console.error(`启动失败:${(e as Error).message}`);
  await release().catch(() => undefined);
  process.exit(1);
});
