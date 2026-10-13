// 工程:新建(失败按相反顺序回滚)、列表、删除(21 号文档 §4.1、§4.2)。所有修改操作串行执行。
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { a2a, a2aError, checkProject, parseJson, projectDir, pyTool, sessionName, stateDir } from "./a2a.js";
import { brokerStatus, reapOrphans, startBroker, stopBroker } from "./broker.js";
import { DATA_DIR, FAIL_AT, FAIL_ONCE, HOME, LAUNCHERS, PROJECTS_FILE } from "./config.js";

const failedOnce = new Set<string>(); // 仅测试用:GUI_DEMO_FAIL_ONCE 每个步骤只失败一次
import { herdrRunning, herdrSessions, hostTmux, killTmux, startHerdr, stopHerdr, tmuxSessions, windowTmuxPrefix } from "./herdr.js";

export interface Project {
  id: string;
  name: string;
  path: string;
  session: string;
  createdAt: string;
  description?: string;
}

export class UserError extends Error {
  constructor(message: string, readonly status = 400, readonly step?: string) {
    super(message);
  }
}

// 阶段 8 实测用过的三条边(模板保留"不要真的去做")
const EDGES = [
  { id: "dv_done", from: "dv", to: "sw",
    template: "{ip} ip 我已经完成了uvm验证，你需要对这个{ip} ip进行 驱动程序开发和HAL框架开发。不要真的去做，直接用 a2a_send 回复测试成功或测试失败（二选一）" },
  { id: "sw_test_pass", from: "sw", to: "dv", template: "我已经完成了{ip} ip的驱动程序开发，测试成功。你只回复收到即可，不要真的去做" },
  { id: "sw_test_fail", from: "sw", to: "dv", template: "我已经完成了{ip} ip的驱动程序开发，测试失败。你只回复收到即可，不要真的去做" },
];

let chain: Promise<unknown> = Promise.resolve();
/** 串行执行修改操作(新建、删除;后续的 agent 增删也走这里)。 */
export function serial<T>(fn: () => Promise<T>): Promise<T> {
  const next = chain.then(fn, fn);
  chain = next.catch(() => undefined);
  return next;
}

export function loadProjects(): Project[] {
  try {
    return JSON.parse(fs.readFileSync(PROJECTS_FILE, "utf8")) as Project[];
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code === "ENOENT") return [];
    throw new Error(`读不了 ${PROJECTS_FILE}:${(e as Error).message}`);
  }
}

function saveProjects(list: Project[]): void {
  const tmp = `${PROJECTS_FILE}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(list, null, 2), { mode: 0o600 });
  fs.renameSync(tmp, PROJECTS_FILE);
}

export function getProject(id: string): Project {
  const p = loadProjects().find((x) => x.id === id);
  if (!p) throw new UserError(`工程 ${id} 不存在`, 404);
  return p;
}

const within = (p: string, base: string) => p.startsWith(base + path.sep);

function realOrFail(p: string): string {
  try {
    return fs.realpathSync(p);
  } catch (e) {
    throw new UserError(`项目路径解析失败(${p}):${(e as Error).message}`);
  }
}

/** 项目路径:绝对路径、真实路径在家目录下且不在 demo 数据目录里,不存在就创建。
 *  先核验"最近的已存在祖先"的真实路径,再创建,避免经软链接先在家目录外建出目录。 */
function checkPath(raw: unknown): string {
  if (typeof raw !== "string" || !path.isAbsolute(raw.trim())) throw new UserError("项目路径必须是绝对路径");
  const wanted = path.resolve(raw.trim());
  const home = realOrFail(HOME);
  const data = fs.existsSync(DATA_DIR) ? realOrFail(DATA_DIR) : DATA_DIR;
  const allowed = (real: string, final: boolean) =>
    (final ? within(real, home) : real === home || within(real, home)) && real !== data && !within(real, data);
  if (!within(wanted, HOME) && !within(wanted, home)) throw new UserError(`项目路径必须在 ${HOME} 下`);
  let ancestor = wanted;
  for (;;) {
    try {
      fs.lstatSync(ancestor);
      break;
    } catch {
      ancestor = path.dirname(ancestor);
    }
  }
  if (!allowed(realOrFail(ancestor), ancestor === wanted)) {
    throw new UserError(`项目路径(经 ${ancestor} 解析到 ${realOrFail(ancestor)})必须在 ${HOME} 下、且不在 demo 的数据目录里`);
  }
  try {
    fs.mkdirSync(wanted, { recursive: true });
  } catch (e) {
    throw new UserError(`建不了项目路径 ${wanted}:${(e as Error).message}`);
  }
  const real = realOrFail(wanted);
  if (!allowed(real, true)) throw new UserError(`项目路径(真实路径 ${real})必须在 ${HOME} 下、且不在 demo 的数据目录里`);
  return real;
}

function checkName(raw: unknown): string {
  const name = typeof raw === "string" ? raw.trim() : "";
  // eslint-disable-next-line no-control-regex
  if (!name || [...name].length > 40 || /[\u0000-\u001f\u007f]/.test(name)) throw new UserError("工程名称必须是 1–40 个字符,不含控制字符");
  return name;
}

function checkDescription(raw: unknown): string {
  if (raw === undefined || raw === null) return "";
  const text = typeof raw === "string" ? raw.trim() : "";
  // eslint-disable-next-line no-control-regex
  if (typeof raw !== "string" || [...text].length > 200 || /[\u0000-\u0008\u000b-\u001f\u007f]/.test(text)) {
    throw new UserError("简介必须是不超过 200 个字符的文字");
  }
  return text;
}

export function createProject(input: { name?: unknown; path?: unknown; description?: unknown }): Promise<Project> {
  return serial(async () => {
    const name = checkName(input.name);
    const description = checkDescription(input.description);
    if (loadProjects().some((p) => p.name === name)) throw new UserError(`已有同名工程:${name}`);
    const projectPath = checkPath(input.path);
    const id = `p${crypto.randomBytes(4).toString("hex")}`;
    const project: Project = { id, name, path: projectPath, session: sessionName(id), createdAt: new Date().toISOString(),
      ...(description ? { description } : {}) };
    if ((await herdrSessions()).has(project.session)) throw new UserError(`herdr 会话 ${project.session} 已存在`, 409);

    const undo: Array<() => Promise<unknown>> = [];
    let step = "";
    const at = (s: string) => {
      step = s;
      if (FAIL_AT === s) throw new Error(`测试注入:在"${s}"失败`);
    };
    try {
      at("state_dir");
      fs.mkdirSync(stateDir(id), { recursive: true, mode: 0o700 });
      undo.push(async () => fs.rmSync(projectDir(id), { recursive: true, force: true }));

      at("topology");
      const topology = {
        version: 1, project_id: id, workspace_label: name,
        roles: {
          dv: { label: "验证智能体", launcher: LAUNCHERS.dv },
          sw: { label: "软件智能体", launcher: LAUNCHERS.sw },
        },
        ips: [], edges: EDGES,
      };
      const init = await pyTool(project, ["init", JSON.stringify(topology)]);
      if (init.code !== 0) throw new Error(`写拓扑失败:${a2aError(init)}`);
      await checkProject(project);

      at("herdr");
      undo.push(() => stopHerdr(id, project.session));
      await startHerdr(id, project.session);

      at("broker");
      undo.push(() => stopBroker(id));
      await startBroker(project);

      at("save");
      saveProjects([...loadProjects(), project]);
      return project;
    } catch (e) {
      // 按相反顺序回滚;某一步失败就停下,不再做后面的(尤其是删状态目录),避免会话还在而状态没了
      let rollback = "";
      for (const fn of undo.reverse()) {
        try {
          await fn();
        } catch (u) {
          rollback = `;回滚未完成(${(u as Error).message}),已保留状态目录 ${projectDir(id)},请人工检查 herdr 会话 ${project.session}`;
          break;
        }
      }
      if (e instanceof UserError && !rollback) throw e;
      throw new UserError((e as Error).message + rollback, e instanceof UserError ? e.status : 500, step);
    }
  });
}

/** 删除工程:停 broker → purge 所有 agent → 关窗口 tmux → 停删 herdr 会话 → 删状态目录 → 去掉登记。不删项目路径。 */
export function deleteProject(id: string): Promise<{ warnings: string[] }> {
  return serial(async () => {
    const project = getProject(id);
    const warnings: string[] = [];
    await stopBroker(id);
    if (await herdrRunning(project.session)) {
      const list = await a2a(project, ["agent", "list"]);
      const rows = (parseJson(list.stdout) as Array<{ agent_id: string }> | undefined) ?? [];
      if (list.code !== 0) warnings.push(`agent list 失败:${a2aError(list)}`);
      for (const row of rows) {
        const [role, ...rest] = row.agent_id.split("_");
        const purge = await a2a(project, ["agent", "purge", role, rest.join("_")]);
        if (purge.code !== 0) warnings.push(`purge ${row.agent_id} 失败:${a2aError(purge)}`);
      }
    }
    // 下面任何一步确认不了(查询失败、关不掉)都抛错,工程与状态目录保留,便于重试
    try {
      for (const t of await tmuxSessions()) if (t.startsWith(windowTmuxPrefix(id))) await killTmux(t);
      await stopHerdr(id, project.session);
    } catch (e) {
      throw new UserError(`删除未完成,工程与状态目录保留以便重试:${(e as Error).message}`, 500);
    }
    fs.rmSync(projectDir(id), { recursive: true, force: true });
    saveProjects(loadProjects().filter((p) => p.id !== id));
    return { warnings };
  });
}

/** VM 或 herdr 重启后恢复:停 broker(含后端被强杀留下的孤儿)→ herdr 会话没在运行就重新起 →
 *  `a2a agent recover`(要求 broker 已停;对还活着的 agent 不做事,可重复执行)→ 起 broker。
 *  herdr 会话没在运行、或 broker 没在运行时都可以恢复,所以中途失败后可以再点一次。
 *  窗口 tmux 里的 attach 循环会自己接上恢复后的 agent。 */
export function recoverProject(id: string): Promise<{ result: unknown; warnings: string[] }> {
  return serial(async () => {
    const project = getProject(id);
    let herdrUp: boolean;
    try {
      herdrUp = await herdrRunning(project.session);
    } catch (e) {
      // 查不清会话状态时什么都不动:不关宿主 tmux、不重起会话、不停 broker
      throw new UserError(`${(e as Error).message};会话状态不明,不做恢复`, 503);
    }
    if (herdrUp && brokerStatus(id).running) throw new UserError("herdr 会话与 broker 都在运行,不需要恢复", 409);
    await checkProject(project);
    await stopBroker(id);
    await reapOrphans(project); // 身份不明的 broker 会让这里报错,不自动停
    if (!herdrUp) {
      await killTmux(hostTmux(id)); // 旧的宿主 tmux(会话停了它多半已退出)
      await startHerdr(id, project.session);
    }
    if (FAIL_ONCE === "recover" && !failedOnce.has("recover")) {
      failedOnce.add("recover");
      throw new UserError("测试注入:agent recover 失败", 500);
    }
    const r = await a2a(project, ["agent", "recover"], 300_000);
    const result = parseJson(r.stdout);
    const warnings: string[] = [];
    // 退出码非 0 但有 JSON 结果:部分 agent 没恢复(结果里有原因),仍然起 broker
    if (r.code !== 0 && result === undefined) throw new UserError(`agent recover 失败:${a2aError(r)}`, 500);
    if (r.code !== 0) warnings.push("部分 agent 没有恢复,见结果里的原因");
    await startBroker(project);
    return { result, warnings };
  });
}

/** herdr: true 运行 / false 没在运行 / null 状态查询失败(页面显示"查询失败",恢复会被拒绝)。 */
export async function listProjects(): Promise<Array<Project & { herdr: boolean | null; broker: ReturnType<typeof brokerStatus> }>> {
  const out = [];
  for (const p of loadProjects()) {
    out.push({ ...p, herdr: await herdrRunning(p.session).catch(() => null), broker: brokerStatus(p.id) });
  }
  return out;
}

/** 后端启动时:校验每个工程,herdr 会话在就拉起 broker;不在就留给界面提示(第 5 步做恢复)。 */
export async function resumeProjects(): Promise<string[]> {
  const notes: string[] = [];
  for (const p of loadProjects()) {
    try {
      await checkProject(p);
      if (await herdrRunning(p.session)) await startBroker(p);
      else notes.push(`工程 ${p.name}(${p.id})的 herdr 会话没有运行`);
    } catch (e) {
      notes.push(`工程 ${p.name}(${p.id}):${(e as Error).message}`);
    }
  }
  return notes;
}
