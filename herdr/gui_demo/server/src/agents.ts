// agent:列表、新增、删除(只走 IP 级排空)、停止、重启(21 号文档 §4.3–§4.5)。修改操作与工程操作共用串行队列。
import { a2a, a2aError, parseJson, pyTool } from "./a2a.js";
import { herdrRunning, killTmux, tmuxSessions, windowTmuxPrefix } from "./herdr.js";
import { getProject, Project, serial, UserError } from "./projects.js";

export const ROLES = { dv: "验证智能体", sw: "软件智能体" } as const;
export type Role = keyof typeof ROLES;
const IP = /^[a-z][a-z0-9_-]{0,31}$/;
export const AGENT_ID = /^(dv|sw)_([a-z][a-z0-9_-]{0,31})$/;

export interface AgentRow {
  agentId: string;
  role: Role;
  ip: string;
  lifecycle: string;
  paneId: string | null;
  agentName: string | null;
  live: string | null;
}

function checkRole(raw: unknown): Role {
  if (raw !== "dv" && raw !== "sw") throw new UserError("智能体种类只能是 dv(验证智能体)或 sw(软件智能体)");
  return raw;
}

function checkIp(raw: unknown, role: Role): string {
  if (typeof raw !== "string" || !IP.test(raw)) {
    throw new UserError("IP 名必须是小写字母开头,只含小写字母、数字、下划线、连字符,最长 32 个字符");
  }
  if (`${role}_${raw}`.length > 32) throw new UserError(`agent 名 ${role}_${raw} 超过 32 个字符`);
  return raw;
}

function parseAgentId(agentId: string): { role: Role; ip: string } {
  const m = agentId.match(AGENT_ID);
  if (!m) throw new UserError(`agent 名不合法:${agentId}`, 404);
  return { role: m[1] as Role, ip: m[2] };
}

async function requireHerdr(p: Project): Promise<void> {
  if (!(await herdrRunning(p.session))) throw new UserError(`工程的 herdr 会话 ${p.session} 没有运行`, 409);
}

async function agentRows(p: Project): Promise<AgentRow[]> {
  const r = await a2a(p, ["agent", "list"]);
  if (r.code !== 0) throw new UserError(`agent list 失败:${a2aError(r)}`, 500);
  const rows = (parseJson(r.stdout) as Array<Record<string, string | null>> | undefined) ?? [];
  const out: AgentRow[] = [];
  for (const row of rows) {
    const m = String(row.agent_id).match(AGENT_ID);
    if (!m) continue; // demo 只管 dv / sw
    out.push({ agentId: m[0], role: m[1] as Role, ip: m[2], lifecycle: String(row.lifecycle),
      paneId: row.pane_id ?? null, agentName: row.agent_name ?? null, live: row.live_status ?? null });
  }
  return out;
}

/** 关掉"注册表里已经没有对应 agent"的窗口 tmux(比如删除 IP 后关 tmux 失败留下的)。
 *  只报告、不抛错:列表每 3 秒刷新一次,下次会再试。 */
async function reapStaleWindows(p: Project, agents: AgentRow[]): Promise<string[]> {
  const warnings: string[] = [];
  const prefix = windowTmuxPrefix(p.id);
  const known = new Set(agents.map((a) => a.agentId));
  let names: string[];
  try {
    names = await tmuxSessions();
  } catch (e) {
    return [`查不了 tmux:${(e as Error).message}`];
  }
  for (const name of names) {
    if (!name.startsWith(prefix) || known.has(name.slice(prefix.length))) continue;
    try {
      await killTmux(name);
    } catch (e) {
      warnings.push((e as Error).message);
    }
  }
  return warnings;
}

export async function listAgents(id: string): Promise<{ roles: typeof ROLES; ips: string[]; agents: AgentRow[]; warnings: string[] }> {
  const p = getProject(id);
  const topo = await pyTool(p, ["show"]);
  if (topo.code !== 0) throw new UserError(`读不了拓扑:${a2aError(topo)}`, 500);
  const ips = (parseJson(topo.stdout) as { ips?: string[] } | undefined)?.ips ?? [];
  const agents = await agentRows(p);
  // 回收放进串行队列、不等它:和新增 / 删除互斥(否则可能把刚建好的窗口当残留关掉),列表也不被长操作卡住。
  // 返回上一次回收的提示。
  if (!reaping.has(p.id)) {
    reaping.add(p.id);
    void serial(async () => {
      try {
        reapWarnings.set(p.id, await reapStaleWindows(p, await agentRows(p)));
      } catch (e) {
        reapWarnings.set(p.id, [(e as Error).message]);
      } finally {
        reaping.delete(p.id);
      }
    });
  }
  return { roles: ROLES, ips, agents, warnings: reapWarnings.get(p.id) ?? [] };
}

const reaping = new Set<string>();
const reapWarnings = new Map<string, string[]>();

/** 在某类智能体下新增一个 IP 的 agent:`a2a ip add <ip> --roles <role> --cwd <项目路径>/{role}_{ip}`。 */
export function addAgent(id: string, input: { role?: unknown; ip?: unknown }): Promise<AgentRow> {
  return serial(async () => {
    const p = getProject(id);
    const role = checkRole(input.role);
    const ip = checkIp(input.ip, role);
    await requireHerdr(p);
    if ((await agentRows(p)).some((a) => a.agentId === `${role}_${ip}`)) throw new UserError(`${role}_${ip} 已存在`, 409);
    const r = await a2a(p, ["ip", "add", ip, "--roles", role, "--cwd", `${p.path}/{role}_{ip}`], 180_000);
    if (r.code !== 0) throw new UserError(`新增 ${role}_${ip} 失败:${a2aError(r)}`, 500);
    const row = (await agentRows(p)).find((a) => a.agentId === `${role}_${ip}`);
    if (!row) throw new UserError(`ip add 成功了,但注册表里没有 ${role}_${ip}`, 500);
    return row;
  });
}

/** 删除一个 IP 的全部 agent 并从拓扑删除(`ip remove --force`,有排空)。
 *  expect 是界面上看到的该 IP 下的 agent;和当前实际不一致就拒绝(防止删掉界面没显示的 agent)。 */
export function deleteIp(id: string, rawIp: string, input: { expect?: unknown }): Promise<{ removed: string[]; result: unknown; warnings: string[] }> {
  return serial(async () => {
    const p = getProject(id);
    if (!IP.test(rawIp)) throw new UserError(`IP 名不合法:${rawIp}`, 404);
    if (!Array.isArray(input.expect) || !input.expect.every((x) => typeof x === "string")) {
      throw new UserError("请求必须带 expect:界面上看到的该 IP 下的 agent 列表");
    }
    await requireHerdr(p);
    const actual = (await agentRows(p)).filter((a) => a.ip === rawIp).map((a) => a.agentId).sort();
    const expected = [...(input.expect as string[])].sort();
    if (actual.join(",") !== expected.join(",")) {
      throw new UserError(`IP ${rawIp} 下的智能体已经变化(现在是:${actual.join("、") || "无"}),请刷新后再删`, 409);
    }
    const r = await a2a(p, ["ip", "remove", rawIp, "--force"], 240_000);
    if (r.code !== 0) {
      throw new UserError(`删除 IP ${rawIp} 失败:${a2aError(r)}。可在 VM 上用 a2a queue 查看队列,`
        + `或 a2a ip undrain ${rawIp} 放弃这次删除`, 500);
    }
    // agent 已经删掉了:关窗口 tmux 失败不算删除失败,如实报告"部分完成";残留的窗口由列表刷新时回收
    const warnings: string[] = [];
    for (const agentId of actual) {
      try {
        await killTmux(`${windowTmuxPrefix(p.id)}${agentId}`);
      } catch (e) {
        warnings.push(`agent 已删除,但窗口没关掉(刷新列表时会再试):${(e as Error).message}`);
      }
    }
    return { removed: actual, result: parseJson(r.stdout), warnings };
  });
}

/** 停止 / 重启(`agent stop` 只停进程、保留 pane;`agent restore` 按登记信息在原 pane 或新 pane 里重启)。 */
export function agentAction(id: string, agentId: string, action: "stop" | "restore"): Promise<AgentRow> {
  return serial(async () => {
    const p = getProject(id);
    const { role, ip } = parseAgentId(agentId);
    await requireHerdr(p);
    if (!(await agentRows(p)).some((a) => a.agentId === agentId)) throw new UserError(`${agentId} 不存在`, 404);
    const r = await a2a(p, ["agent", action, role, ip], 180_000);
    if (r.code !== 0) throw new UserError(`${action === "stop" ? "停止" : "重启"} ${agentId} 失败:${a2aError(r)}`, 500);
    const row = (await agentRows(p)).find((a) => a.agentId === agentId);
    if (!row) throw new UserError(`${agentId} 操作后不在注册表里`, 500);
    return row;
  });
}
