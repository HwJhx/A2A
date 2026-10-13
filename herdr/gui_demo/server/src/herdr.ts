// herdr 会话与 tmux 会话。herdr 0.9.3 没有单独的 start 命令:在一个隐藏的 tmux 里运行
// `herdr session attach <会话>` 来起服务(之前的实测都这样做)。
// 查询或清理失败一律抛错(fail-closed):调用方据此决定不删状态目录。
import { HERDR, HOME, SESSION_PREFIX, TMUX } from "./config.js";
import { run, sleep } from "./proc.js";

export const hostTmux = (id: string) => `${SESSION_PREFIX}host_${id}`;
export const windowTmuxPrefix = (id: string) => `${SESSION_PREFIX}win_${id}_`;

/** herdr 会话列表 {名字 → 状态};查询失败抛错。 */
export async function herdrSessions(): Promise<Map<string, string>> {
  const r = await run(HERDR, ["session", "list"], { timeoutMs: 15_000 });
  if (r.code !== 0) throw new Error(`herdr session list 失败:${r.stderr.trim()}`);
  const rows = new Map<string, string>();
  for (const line of r.stdout.split("\n")) {
    const parts = line.trim().split(/\s+/);
    if (parts.length >= 4 && parts[0] !== "NAME") rows.set(parts[0], parts[1]);
  }
  return rows;
}

export async function herdrRunning(session: string): Promise<boolean> {
  const r = await run(HERDR, ["--session", session, "workspace", "list"], { timeoutMs: 10_000 });
  return r.code === 0;
}

export async function startHerdr(id: string, session: string): Promise<void> {
  const host = hostTmux(id);
  const r = await run(TMUX, ["new-session", "-d", "-s", host, "-x", "200", "-y", "50", "-c", HOME,
    HERDR, "session", "attach", session], { timeoutMs: 15_000 });
  if (r.code !== 0) throw new Error(`启动 herdr 宿主 tmux 失败:${r.stderr.trim()}`);
  for (let i = 0; i < 100; i++) {
    if (await herdrRunning(session)) return;
    await sleep(300);
  }
  throw new Error(`herdr 会话 ${session} 30 秒内没有起来`);
}

/** tmux 会话名列表;没有 tmux 服务时为空,其他失败抛错。 */
export async function tmuxSessions(): Promise<string[]> {
  const r = await run(TMUX, ["list-sessions", "-F", "#{session_name}"], { timeoutMs: 10_000 });
  if (r.code === 0) return r.stdout.split("\n").filter(Boolean);
  if (/no server running|error connecting to/.test(r.stderr)) return [];
  throw new Error(`tmux list-sessions 失败:${r.stderr.trim()}`);
}

/** 关掉 tmux 会话并确认它已不在;确认不了就抛错。 */
export async function killTmux(name: string): Promise<void> {
  if (!(await tmuxSessions()).includes(name)) return;
  const r = await run(TMUX, ["kill-session", "-t", `=${name}`], { timeoutMs: 10_000 });
  if ((await tmuxSessions()).includes(name)) throw new Error(`tmux 会话 ${name} 关不掉:${r.stderr.trim()}`);
}

/** 停止并删除 herdr 会话、关掉宿主 tmux;最后确认会话已不存在。任何一步确认不了就抛错。 */
export async function stopHerdr(id: string, session: string): Promise<void> {
  if ((await herdrSessions()).has(session)) {
    const stop = await run(HERDR, ["session", "stop", session], { timeoutMs: 30_000 });
    if (stop.code !== 0 && await herdrRunning(session)) throw new Error(`herdr session stop 失败:${stop.stderr.trim()}`);
    const del = await run(HERDR, ["session", "delete", session], { timeoutMs: 30_000 });
    if (del.code !== 0) throw new Error(`herdr session delete 失败:${del.stderr.trim()}`);
  }
  if ((await herdrSessions()).has(session)) throw new Error(`herdr 会话 ${session} 删除后仍在列表里`);
  await killTmux(hostTmux(id));
}
