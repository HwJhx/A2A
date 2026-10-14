// 工程环境与 a2a 调用。所有 a2a 子进程(命令行、Python 辅助脚本、broker)都只通过 projectEnv 拿环境(21 号文档 §3)。
import path from "node:path";
import { fileURLToPath } from "node:url";
import { A2A_SRC, DATA_DIR, PYTHON, SESSION_PREFIX } from "./config.js";
import { run, RunResult } from "./proc.js";

export const PROJECT_ID = /^p[0-9a-f]{8}$/;

export interface ProjectRef {
  id: string;
  session: string;
  /** 拓扑文件的绝对路径。新建的工程放在 <项目路径>/topology.yaml(21 号文档 §11);旧记录没有这个字段。 */
  topology?: string;
}

export const projectDir = (id: string) => path.join(DATA_DIR, "projects", id);
export const stateDir = (id: string) => path.join(projectDir(id), "state");
/** 旧位置:状态目录里的 topology.yaml。旧格式的工程记录(没有 topology 字段)仍用它。 */
export const topologyPath = (id: string) => path.join(stateDir(id), "topology.yaml");
/** 工程实际使用的拓扑文件:记录里有就用记录里的,没有就用旧位置。 */
export const topologyOf = (p: ProjectRef) => p.topology ?? topologyPath(p.id);
export const sessionName = (id: string) => `${SESSION_PREFIX}${id}`;

const PY_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../py");

/** 工程的子进程环境:先删掉继承来的 A2A_* / HERDR_*,再设本工程的三个变量。 */
export function projectEnv(p: ProjectRef): NodeJS.ProcessEnv {
  if (!PROJECT_ID.test(p.id) || p.session !== sessionName(p.id)) {
    throw new Error(`工程 ${p.id} 的会话名 ${p.session} 不符合规则`);
  }
  const env: NodeJS.ProcessEnv = {};
  for (const [k, v] of Object.entries(process.env)) {
    if (!k.startsWith("A2A_") && !k.startsWith("HERDR_")) env[k] = v;
  }
  env.A2A_STATE_DIR = stateDir(p.id);
  env.A2A_TOPOLOGY = topologyOf(p);
  env.HERDR_SESSION = p.session;
  env.PYTHONPATH = A2A_SRC;
  env.PYTHONDONTWRITEBYTECODE = "1";
  return env;
}

/** 只有这些命令有 --session 参数(cli.py);其余(queue、resolve、status、topology…)靠环境变量。 */
export function a2aArgs(p: ProjectRef, args: string[]): string[] {
  const takesSession = args[0] === "agent" || args[0] === "ip" || (args[0] === "broker" && args[1] === "run");
  return ["-m", "a2a.cli", ...args, ...(takesSession ? ["--session", p.session] : [])];
}

export function a2a(p: ProjectRef, args: string[], timeoutMs = 120_000): Promise<RunResult> {
  return run(PYTHON, a2aArgs(p, args), { env: projectEnv(p), timeoutMs });
}

export function pyTool(p: ProjectRef, args: string[]): Promise<RunResult> {
  return run(PYTHON, [path.join(PY_DIR, "topology_tool.py"), ...args], { env: projectEnv(p), timeoutMs: 30_000 });
}

/** a2a 命令行的 JSON 输出(stdout)或错误(stderr 里的 {"error":…})。 */
export function parseJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

export function a2aError(r: RunResult): string {
  const parsed = parseJson(r.stderr.trim()) as { error?: string } | undefined;
  return parsed?.error ?? (r.stderr.trim() || r.stdout.trim() || `退出码 ${r.code}`);
}

/** 一致性校验:会话名符合规则、拓扑文件就是工程记录里的那个、拓扑 project_id 等于工程 id。 */
export async function checkProject(p: ProjectRef): Promise<void> {
  projectEnv(p); // 会话名规则
  const r = await pyTool(p, ["show"]);
  if (r.code !== 0) throw new Error(`读不了工程 ${p.id} 的拓扑:${a2aError(r)}`);
  const info = parseJson(r.stdout) as { project_id?: string; path?: string } | undefined;
  if (info?.project_id !== p.id) throw new Error(`工程 ${p.id} 的拓扑 project_id 是 ${info?.project_id}`);
  if (info?.path !== topologyOf(p)) throw new Error(`工程 ${p.id} 的拓扑路径不对:${info?.path}`);
}
