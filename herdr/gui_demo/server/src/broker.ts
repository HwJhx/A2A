// 每个工程一个 broker 子进程(a2a broker run --session <会话>),意外退出时 3 秒后重启。
import { ChildProcess, spawn } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { a2aArgs, ProjectRef, projectDir, projectEnv, stateDir } from "./a2a.js";
import { PYTHON } from "./config.js";
import { sleep } from "./proc.js";

interface Entry {
  child: ChildProcess | null;
  stopping: boolean;
  restarts: number;
  lastExit: string | null;
}

const brokers = new Map<string, Entry>();

// GUI 启动的 broker 带一个随机身份(环境变量 GUI_DEMO_BROKER_ID),并把 pid、进程启动时间、身份记在
// <工程目录>/broker.json。后端被 kill -9 后重启时,只回收身份与启动时间都对得上的遗留进程;
// 命令行与状态目录匹配、但身份对不上的(比如有人手动起的 broker)不碰,拒绝启动并报告。
const ID_ENV = "GUI_DEMO_BROKER_ID";
const recordPath = (id: string) => path.join(projectDir(id), "broker.json");

interface Record_ {
  pid: number;
  start: string;
  brokerId: string;
}

function readProc(pid: number | string, file: string): string[] {
  try {
    return fs.readFileSync(`/proc/${pid}/${file}`, "utf8").split("\0").filter(Boolean);
  } catch {
    return [];
  }
}

/** /proc/<pid>/stat 第 22 项:进程启动时间(开机后的时钟滴答数),用来识别 PID 是否被复用。 */
function startTime(pid: number): string | null {
  try {
    const stat = fs.readFileSync(`/proc/${pid}/stat`, "utf8");
    return stat.slice(stat.lastIndexOf(")") + 2).split(" ")[19] ?? null;
  } catch {
    return null;
  }
}

function launch(p: ProjectRef, entry: Entry): void {
  const log = fs.openSync(path.join(projectDir(p.id), "broker.log"), "a");
  try {
    const brokerId = crypto.randomBytes(16).toString("hex");
    const child = spawn(PYTHON, a2aArgs(p, ["broker", "run"]), { env: { ...projectEnv(p), [ID_ENV]: brokerId },
      stdio: ["ignore", log, log] });
    entry.child = child;
    if (child.pid) {
      const rec: Record_ = { pid: child.pid, start: startTime(child.pid) ?? "", brokerId };
      // 先写临时文件再改名:写到一半被强杀也不会留下不完整的记录
      const tmp = `${recordPath(p.id)}.${process.pid}.tmp`;
      fs.writeFileSync(tmp, JSON.stringify(rec), { mode: 0o600 });
      fs.renameSync(tmp, recordPath(p.id));
    }
    child.on("exit", (code, signal) => {
      entry.child = null;
      entry.lastExit = signal ? `signal ${signal}` : `exit ${code}`;
      if (entry.stopping) return;
      entry.restarts += 1;
      setTimeout(() => { if (!entry.stopping && brokers.get(p.id) === entry) launch(p, entry); }, 3000);
    });
  } finally {
    fs.closeSync(log);
  }
}

function readRecord(id: string): Record_ | null {
  try {
    return JSON.parse(fs.readFileSync(recordPath(id), "utf8")) as Record_;
  } catch {
    return null;
  }
}

/** 命令行是本工程的 `broker run --session <会话>`、环境里 A2A_STATE_DIR 是本工程状态目录、且不是本后端当前子进程的 broker。 */
export function strayBrokers(p: ProjectRef): number[] {
  const mine = new Set([...brokers.values()].map((e) => e.child?.pid).filter((x): x is number => !!x));
  const pids: number[] = [];
  for (const pid of fs.readdirSync("/proc").filter((d) => /^\d+$/.test(d))) {
    if (readProc(pid, "cmdline").slice(-5).join(" ") !== `a2a.cli broker run --session ${p.session}`) continue;
    if (mine.has(Number(pid))) continue;
    if (readProc(pid, "environ").includes(`A2A_STATE_DIR=${stateDir(p.id)}`)) pids.push(Number(pid));
  }
  return pids;
}

/** 是不是本 GUI 上次启动、记录在 broker.json 里的那个进程(身份与启动时间都对得上)。 */
function isOurOrphan(p: ProjectRef, pid: number): boolean {
  const rec = readRecord(p.id);
  return !!rec && rec.pid === pid && !!rec.start && startTime(pid) === rec.start
    && readProc(pid, "environ").includes(`${ID_ENV}=${rec.brokerId}`);
}

function signalIfOurs(p: ProjectRef, pid: number, sig: NodeJS.Signals): void {
  // 发信号前再核对一次身份与启动时间,缩小 PID 复用窗口
  if (!isOurOrphan(p, pid)) return;
  try { process.kill(pid, sig); } catch { /* 已退出 */ }
}

export async function reapOrphans(p: ProjectRef): Promise<void> {
  const stray = strayBrokers(p);
  const unknown = stray.filter((pid) => !isOurOrphan(p, pid));
  if (unknown.length) {
    throw new Error(`本工程已有身份不明的 broker 在运行(pid ${unknown.join(",")}),可能是手动启动的;`
      + "为免中断投递不自动停止,请人工确认后停掉它再重启后端");
  }
  for (const pid of stray) signalIfOurs(p, pid, "SIGTERM");
  for (let i = 0; i < 100 && strayBrokers(p).length; i++) await sleep(100);
  for (const pid of strayBrokers(p)) signalIfOurs(p, pid, "SIGKILL");
  for (let i = 0; i < 20 && strayBrokers(p).length; i++) await sleep(100);
  if (strayBrokers(p).length) throw new Error(`遗留的 broker 进程停不掉:${strayBrokers(p).join(",")}`);
}

/** 启动 broker:先接管(停掉)本工程遗留的 broker,再启动,等 2 秒确认它没有立刻退出(比如拿不到 broker.lock)。 */
export async function startBroker(p: ProjectRef): Promise<void> {
  if (brokers.get(p.id)?.child) return;
  await reapOrphans(p);
  const entry: Entry = { child: null, stopping: false, restarts: 0, lastExit: null };
  brokers.set(p.id, entry);
  launch(p, entry);
  await sleep(2000);
  if (!entry.child) {
    entry.stopping = true;
    brokers.delete(p.id);
    throw new Error(`broker 启动后立刻退出(${entry.lastExit}),见 ${path.join(projectDir(p.id), "broker.log")}`);
  }
}

export async function stopBroker(id: string): Promise<void> {
  const entry = brokers.get(id);
  if (!entry) return;
  entry.stopping = true;
  brokers.delete(id);
  const child = entry.child;
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  const exited = new Promise<void>((r) => child.once("exit", () => r()));
  child.kill("SIGTERM");
  const timer = setTimeout(() => child.kill("SIGKILL"), 10_000);
  await exited;
  clearTimeout(timer);
}

export function brokerStatus(id: string): { running: boolean; pid: number | null; restarts: number } {
  const entry = brokers.get(id);
  return { running: !!entry?.child, pid: entry?.child?.pid ?? null, restarts: entry?.restarts ?? 0 };
}

export async function stopAllBrokers(): Promise<void> {
  await Promise.all([...brokers.keys()].map(stopBroker));
}
