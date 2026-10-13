// 写权限栅栏:GUI 对某个 agent 做停止 / 重启 / 删除时,先同步撤销它的终端写权限(在调用 a2a 之前),
// 操作完成后只认"完成之后才开始"的状态查询(终端桥据此判断可否写入)。
// 原因:输入闸门的状态查询每 2 秒一次;agent 停止后窗口里是 pane 的 shell,旧的"可写"结果不能再用。
// 用单调递增的序号而不是时间戳:同一毫秒内"栅栏解除"和"查询开始"的先后也分得清
let seq = 0;
export const nextSeq = () => ++seq;
const fences = new Map<string, number>();
const listeners = new Set<(projectId: string) => void>();
const key = (projectId: string, agentId: string) => `${projectId}/${agentId}`;

/** 撤销这些 agent 的写权限;返回的函数在操作结束时调用(成功或失败都要调)。 */
export function fence(projectId: string, agentIds: string[]): () => void {
  for (const a of agentIds) fences.set(key(projectId, a), Infinity);
  return () => {
    const now = nextSeq();
    for (const a of agentIds) fences.set(key(projectId, a), now);
    for (const fn of listeners) fn(projectId);
  };
}

/** 状态查询的序号必须大于它才算数;Infinity 表示操作进行中。 */
export function fencedSince(projectId: string, agentId: string): number {
  return fences.get(key(projectId, agentId)) ?? 0;
}

/** 栅栏解除时通知(终端桥据此立刻重新查询)。 */
export function onFenceReleased(fn: (projectId: string) => void): void {
  listeners.add(fn);
}
