// 后端接口。修改类请求一律 JSON(后端校验 Content-Type 与 Origin;浏览器会自动带 Origin)。

export class ApiError extends Error {
  constructor(message: string, readonly status: number, readonly step?: string) {
    super(message);
  }
}

export interface Project {
  id: string;
  name: string;
  path: string;
  session: string;
  createdAt: string;
  description?: string;
  topology?: string;
  herdr?: boolean | null;
  broker?: { running: boolean; pid: number | null; restarts: number };
}

export type Role = "dv" | "sw";

export interface AgentRow {
  agentId: string;
  role: Role;
  ip: string;
  lifecycle: string;
  paneId: string | null;
  agentName: string | null;
  live: string | null;
}

export interface AgentsInfo {
  roles: Record<Role, string>;
  ips: string[];
  agents: AgentRow[];
  warnings: string[];
}

let onUnauthorized: () => void = () => undefined;
export const setUnauthorizedHandler = (fn: () => void) => { onUnauthorized = fn; };

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method,
    headers: method === "GET" ? {} : { "Content-Type": "application/json" },
    body: method === "GET" ? undefined : JSON.stringify(body ?? {}),
  });
  const text = await r.text();
  const data = text ? JSON.parse(text) : undefined;
  if (r.status === 401) onUnauthorized();
  if (!r.ok) throw new ApiError(data?.error ?? `请求失败(${r.status})`, r.status, data?.step);
  return data as T;
}

export const api = {
  config: () => call<{ home: string }>("GET", "/api/config"),
  projects: () => call<Project[]>("GET", "/api/projects"),
  project: (id: string) => call<Project>("GET", `/api/projects/${id}`),
  createProject: (body: { name: string; path: string; description: string }) => call<Project>("POST", "/api/projects", body),
  deleteProject: (id: string) => call<{ warnings: string[] }>("DELETE", `/api/projects/${id}`),
  recoverProject: (id: string) => call<{ result: unknown; warnings: string[] }>("POST", `/api/projects/${id}/recover`),
  agents: (id: string) => call<AgentsInfo>("GET", `/api/projects/${id}/agents`),
  addAgent: (id: string, body: { role: Role; ip: string }) => call<AgentRow & { warnings: string[] }>("POST", `/api/projects/${id}/agents`, body),
  stopAgent: (id: string, agentId: string) => call<AgentRow>("POST", `/api/projects/${id}/agents/${agentId}/stop`),
  restoreAgent: (id: string, agentId: string) => call<AgentRow>("POST", `/api/projects/${id}/agents/${agentId}/restore`),
  deleteIp: (id: string, ip: string, expect: string[]) =>
    call<{ removed: string[]; warnings: string[] }>("DELETE", `/api/projects/${id}/ips/${ip}`, { expect }),
};
