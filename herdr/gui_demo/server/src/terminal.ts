// 终端桥:浏览器(xterm.js)⇄ WebSocket ⇄ node-pty 里的 `tmux attach -t <窗口 tmux>`。
// 握手前已在 http.ts 校验会话 cookie 与 Origin;这里再校验工程、agent 必须是登记过的(不能 attach 任意东西)。
// 协议:客户端发文本帧 JSON {"t":"in","d":"…"} 或 {"t":"size","c":列,"r":行};服务端发文本帧(终端输出)。
import type { IncomingMessage } from "node:http";
import type { Duplex } from "node:stream";
import pty, { IPty } from "node-pty";
import { WebSocket, WebSocketServer } from "ws";
import { AGENT_ID, AgentRow, agentRows } from "./agents.js";
import { FAIL_AT, GATE_POLL_MS, HANDSHAKE_DELAY_MS, MAX_TERMINALS, TMUX } from "./config.js";
import { sleep } from "./proc.js";
import { fencedSince, nextSeq, onFenceReleased } from "./fence.js";
import { getProject, UserError } from "./projects.js";
import { closeWindow, ensureWindow } from "./windows.js";

// ---- 输入闸门 ---------------------------------------------------------------
// 只有"运行中且 herdr 识别得到"的 agent 才转发输入:agent 停止后窗口里是 pane 的 shell(20 号文档 §7),
// 前端虽然设成只读,后端也不能只靠前端。状态每 2 秒查一次;GUI 自己做的停止 / 重启 / 删除另有栅栏
// (fence.ts):操作开始前同步撤销写权限,完成后只认完成之后才开始的查询,并立刻重查。
// 已知限制:从外部用 a2a 命令行停止 agent 时,最多有一个查询周期(约 2 秒)的延迟。
interface Gate { allowed: Set<string>; since: number; refs: number; timer: NodeJS.Timeout; polling: boolean; again: boolean }
const gates = new Map<string, Gate>();

const writable = (rows: AgentRow[]) => new Set(rows.filter((a) => a.lifecycle === "running" && a.live).map((a) => a.agentId));

function poll(projectId: string, gate: Gate): void {
  if (gate.polling) {
    gate.again = true; // 正在查的这次可能早于栅栏解除,查完马上再查一次
    return;
  }
  gate.polling = true;
  gate.again = false;
  const started = nextSeq();
  // getProject 在工程被删后会同步抛错:放进 then 里,失败一律当作"不可写",不让异常逃出定时器
  Promise.resolve().then(() => agentRows(getProject(projectId)))
    .then((r) => { gate.allowed = writable(r); }, () => { gate.allowed = new Set(); })
    .finally(() => {
      gate.since = started;
      gate.polling = false;
      if (gate.again && gates.get(projectId) === gate) poll(projectId, gate);
    });
}

function acquireGate(projectId: string, rows: AgentRow[], rowsAt: number): Gate {
  let gate = gates.get(projectId);
  if (!gate) {
    const g: Gate = { allowed: writable(rows), since: rowsAt, refs: 0, polling: false, again: false,
      timer: setInterval(() => poll(projectId, g), GATE_POLL_MS) };
    gates.set(projectId, g);
    gate = g;
  }
  gate.refs += 1;
  return gate;
}

function releaseGate(projectId: string): void {
  const gate = gates.get(projectId);
  if (!gate || --gate.refs > 0) return;
  clearInterval(gate.timer);
  gates.delete(projectId);
}

onFenceReleased((projectId) => {
  const gate = gates.get(projectId);
  if (gate) poll(projectId, gate);
});

const canWrite = (gate: Gate, projectId: string, agentId: string) =>
  gate.allowed.has(agentId) && gate.since > fencedSince(projectId, agentId);

// ---- 连接 -------------------------------------------------------------------
const wss = new WebSocketServer({ noServer: true, maxPayload: 64 * 1024 });
const terms = new Map<WebSocket, IPty>();
let slots = 0; // 已打开 + 正在握手的终端数;握手一开始就占名额,避免并发握手一起越过上限
let shuttingDown = false;
const pending = new Set<Duplex>(); // 还在等待(查 agent、建窗口)的握手

const clamp = (v: unknown, lo: number, hi: number, dflt: number) => {
  const n = Number(v);
  return Number.isInteger(n) && n >= lo && n <= hi ? n : dflt;
};

function reject(socket: Duplex, status: number, text: string): void {
  socket.end(`HTTP/1.1 ${status} ${text}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n`);
}

export async function handleTerminalUpgrade(req: IncomingMessage, socket: Duplex, head: Buffer): Promise<void> {
  const url = new URL(req.url ?? "/", "http://x");
  if (url.pathname !== "/ws/term") return reject(socket, 404, "Not Found");
  const projectId = url.searchParams.get("project") ?? "";
  const agentId = url.searchParams.get("agent") ?? "";
  if (!/^p[0-9a-f]{8}$/.test(projectId) || !AGENT_ID.test(agentId)) return reject(socket, 400, "Bad Request");
  if (shuttingDown) return reject(socket, 503, "Service Unavailable");
  if (slots >= MAX_TERMINALS) return reject(socket, 503, "Service Unavailable");
  // 握手一开始就占名额。名额只在两种情况下交出去:升级成功、回调里交给 bridge(之后由 bridge 归还);
  // 其他所有情况(拒绝、出错、等待期间客户端断开、handleUpgrade 没调回调)都由这里归还,且只归还一次。
  slots += 1;
  let held = true;
  const giveBack = () => { if (held) { held = false; slots -= 1; } };
  let bridged = false;
  socket.once("close", () => { pending.delete(socket); if (!bridged) giveBack(); });
  pending.add(socket);
  // 客户端已断开,或后端开始关服:都不再往下做
  const gone = () => shuttingDown || socket.destroyed || !socket.writable;
  try {
    const p = getProject(projectId);
    if (fencedSince(p.id, agentId) === Infinity) return reject(socket, 409, "Conflict"); // 正在停止 / 重启 / 删除
    const rowsAt = nextSeq();
    const rows = await agentRows(p);
    if (gone()) return giveBack(); // 客户端已断开:不再往下做
    if (!rows.some((a) => a.agentId === agentId)) return reject(socket, 404, "Not Found");
    if (HANDSHAKE_DELAY_MS) {
      console.log(`测试:握手进入等待 ${agentId}`); // 测试据此确认握手已在等待,再发信号
      await sleep(HANDSHAKE_DELAY_MS);
    }
    if (gone()) return giveBack();
    const window = await ensureWindow(p.id, p.session, agentId);
    // 建窗口期间 agent 可能刚被删除:再核对一次,已不在就把刚建的窗口关掉
    if (fencedSince(p.id, agentId) === Infinity || !(await agentRows(p)).some((a) => a.agentId === agentId)) {
      await closeWindow(p.id, agentId).catch(() => undefined);
      return reject(socket, 409, "Conflict");
    }
    if (gone()) return giveBack();
    const cols = clamp(url.searchParams.get("cols"), 20, 500, 120);
    const rowsN = clamp(url.searchParams.get("rows"), 5, 200, 30);
    wss.handleUpgrade(req, socket, head, (ws) => {
      bridged = true;
      held = false; // 名额交给 bridge,由它在连接关闭时归还
      bridge(ws, projectId, agentId, window, rows, rowsAt, cols, rowsN);
    });
    if (!bridged) giveBack(); // handleUpgrade 遇到已断开的 socket 会直接销毁、不调回调
  } catch (e) {
    reject(socket, e instanceof UserError && e.status === 404 ? 404 : 500, "Error");
  } finally {
    pending.delete(socket);
    if (!bridged) giveBack();
  }
}

function bridge(ws: WebSocket, projectId: string, agentId: string, window: string,
  initial: AgentRow[], initialAt: number, cols: number, rows: number): void {
  const gate = acquireGate(projectId, initial, initialAt);
  let term: IPty;
  try {
    if (FAIL_AT === "pty") throw new Error("测试注入:pty 启动失败");
    // 只 attach 这个 tmux 会话;tmux 客户端退出(会话被关)时 pty 结束,WebSocket 随之关闭
    term = pty.spawn(TMUX, ["attach-session", "-t", `=${window}`], {
      name: "xterm-256color", cols, rows, cwd: process.env.HOME, env: { ...process.env, TERM: "xterm-256color" } as Record<string, string>,
    });
  } catch {
    releaseGate(projectId);
    slots -= 1;
    ws.close(1011, "terminal start failed");
    return;
  }
  terms.set(ws, term);
  let paused = false;
  const out = term.onData((d) => {
    if (ws.readyState !== WebSocket.OPEN) return;
    ws.send(d);
    // 背压:浏览器读得慢时暂停读 pty,缓冲降下来再继续
    if (!paused && ws.bufferedAmount > 1024 * 1024) {
      paused = true;
      term.pause();
      const timer = setInterval(() => {
        if (ws.readyState !== WebSocket.OPEN || ws.bufferedAmount < 256 * 1024) {
          clearInterval(timer);
          paused = false;
          term.resume();
        }
      }, 50);
    }
  });
  const exit = term.onExit(() => ws.close(1000, "terminal closed"));
  ws.on("message", (raw, isBinary) => {
    if (isBinary) return;
    let msg: { t?: string; d?: unknown; c?: unknown; r?: unknown };
    try {
      msg = JSON.parse(String(raw));
    } catch {
      return;
    }
    if (msg.t === "in" && typeof msg.d === "string") {
      if (canWrite(gate, projectId, agentId)) term.write(msg.d); // 否则丢弃:agent 没在运行或正被操作
    } else if (msg.t === "size") {
      term.resize(clamp(msg.c, 20, 500, cols), clamp(msg.r, 5, 200, rows));
    }
  });
  ws.on("close", () => {
    if (!terms.delete(ws)) return; // 已由 closeAllTerminals 清理
    slots -= 1;
    releaseGate(projectId);
    out.dispose();
    exit.dispose();
    try {
      term.kill(); // 只结束这个 tmux 客户端(detach),窗口 tmux 与 agent 不受影响
    } catch {
      /* 已退出 */
    }
  });
}

/** 后端退出时:先拒绝新握手、销毁还在等待的握手,再直接结束所有 tmux 客户端子进程(不等客户端完成关闭握手)。 */
export function closeAllTerminals(): void {
  shuttingDown = true;
  for (const socket of pending) socket.destroy();
  pending.clear();
  for (const [ws, term] of terms) {
    terms.delete(ws);
    try {
      term.kill();
    } catch {
      /* 已退出 */
    }
    ws.terminate();
  }
  slots = 0;
  for (const [id, gate] of gates) {
    clearInterval(gate.timer);
    gates.delete(id);
  }
}
