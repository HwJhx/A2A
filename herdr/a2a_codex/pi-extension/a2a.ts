/**
 * Pi extension for the independent a2a_codex implementation.
 * It exposes only fixed topology edges and delegates all authorization/routing to Router.
 */
import { execFile, execFileSync } from "node:child_process";

type Edge = { edge_id: string; dst: string; text: string };
type Config = { python: string; env: NodeJS.ProcessEnv; timeoutMs: number };

const TIMEOUT_MS = 30_000;
const EXIT_REJECTED = 4;
export const HERDR_WRITE = /\bherdr\b[^\n;|&]*?\b(?:agent\s+prompt|pane\s+(?:send-text|send-keys|run))\b/;

export function isHerdrWrite(command: string): boolean {
  return HERDR_WRITE.test(command);
}

export function a2aConfig(env: NodeJS.ProcessEnv): Config | null {
  const { A2A_PROJECT_ID, A2A_ROLE, A2A_IP, A2A_STATE_DIR, A2A_TOPOLOGY, A2A_PYTHON, A2A_SRC } = env;
  if (!A2A_PROJECT_ID || !A2A_ROLE || !A2A_IP || !A2A_STATE_DIR || !A2A_TOPOLOGY || !A2A_PYTHON || !A2A_SRC) {
    return null;
  }
  const timeoutMs = Number(env.A2A_SEND_TIMEOUT_MS) > 0 ? Number(env.A2A_SEND_TIMEOUT_MS) : TIMEOUT_MS;
  return {
    python: A2A_PYTHON,
    env: { ...env, PYTHONPATH: A2A_SRC, PYTHONDONTWRITEBYTECODE: "1" },
    timeoutMs,
  };
}

export function listEdges(cfg: Config): Edge[] {
  const out = execFileSync(cfg.python, ["-m", "a2a_codex.cli", "edges"],
    { env: cfg.env, encoding: "utf8", timeout: TIMEOUT_MS });
  const value = JSON.parse(out);
  if (!value || !Array.isArray(value.edges)) throw new Error("edges 回执格式非法");
  return value.edges;
}

export function describe(edges: Edge[]): string {
  return [
    "通过 a2a_send 通知同一 IP 的其他智能体。只能选择列出的通信边；目标和消息内容由拓扑固定，不能自定义。",
    "需要通知时调用一次。若发送结果未知，不要重试；停止自动流程并告知用户，由操作员核实队列/审计。",
    ...edges.map((e) => `- ${e.edge_id}: 发给 ${e.dst}，内容「${e.text}」`),
  ].join("\n");
}

function isReceipt(value: any): boolean {
  const nonEmpty = (v: unknown) => typeof v === "string" && v.length > 0;
  return !!value && typeof value === "object" && nonEmpty(value.msg_id) && nonEmpty(value.edge_id)
    && nonEmpty(value.src) && nonEmpty(value.dst) && nonEmpty(value.text)
    && value.state === "QUEUED" && nonEmpty(value.topology_revision);
}

type Outcome =
  | { kind: "sent"; receipt: any }
  | { kind: "rejected"; code: string; reason: string }
  | { kind: "unknown"; reason: string };

export function runSend(cfg: Config, edgeId: string): Promise<Outcome> {
  return new Promise((resolve) => {
    execFile(cfg.python, ["-m", "a2a_codex.cli", "send", edgeId],
      { env: cfg.env, encoding: "utf8", timeout: cfg.timeoutMs }, (error, stdout, stderr) => {
        const errorText = (stderr || "").trim();
        if (!error) {
          try {
            const receipt = JSON.parse(stdout);
            if (isReceipt(receipt) && receipt.edge_id === edgeId) {
              resolve({ kind: "sent", receipt });
              return;
            }
          } catch { /* classify malformed receipt as unknown */ }
          resolve({ kind: "unknown", reason: `a2a 回执无法解析: ${(stdout || "").trim().slice(0, 200)}` });
          return;
        }
        if ((error as any).code === EXIT_REJECTED) {
          try {
            const rejection = JSON.parse(errorText);
            if (rejection && typeof rejection.rejected === "string") {
              resolve({ kind: "rejected", code: rejection.rejected, reason: String(rejection.reason ?? "") });
              return;
            }
          } catch { /* unknown error shape is not proof of rejection */ }
        }
        const cause = (error as any).killed ? `发送进程超过 ${cfg.timeoutMs}ms 被终止` : String(error.message);
        resolve({ kind: "unknown", reason: `${cause}${errorText ? `; ${errorText.split("\n").slice(-3).join(" ")}` : ""}` });
      });
  });
}

export default function (pi: any) {
  const cfg = a2aConfig(process.env);
  if (!cfg) return;
  pi.on("tool_call", async (event: any) => {
    if (event.toolName === "bash" && isHerdrWrite(String(event.input?.command ?? ""))) {
      return { block: true, reason: "不要绕过 Broker 直接向其他 pane 写入；请使用 a2a_send" };
    }
    return undefined;
  });

  let edges: Edge[];
  try { edges = listEdges(cfg); }
  catch (err) {
    console.error(`[a2a_codex] 读取可用边失败，不注册 a2a_send: ${(err as Error).message}`);
    return;
  }
  if (!edges.length) return;

  pi.registerTool({
    name: "a2a_send",
    label: "A2A 发送",
    description: describe(edges),
    parameters: {
      type: "object",
      properties: { edge_id: { type: "string", enum: edges.map((e) => e.edge_id) } },
      required: ["edge_id"],
      additionalProperties: false,
    },
    async execute(_toolCallId: string, params: { edge_id: string }) {
      const selected = edges.find((e) => e.edge_id === params.edge_id);
      if (!selected) throw new Error("所选通信边未注册");
      const startedAt = new Date().toISOString();
      const outcome = await runSend(cfg, selected.edge_id);
      if (outcome.kind === "rejected") {
        throw new Error(`已拒绝，消息没有入队: ${outcome.code}: ${outcome.reason}`);
      }
      if (outcome.kind === "unknown") {
        return {
          content: [{ type: "text", text: `发送结果未知：消息可能已入队。不要重试，停止自动流程并告知用户；由操作员检查 a2a queue ${selected.dst} 和审计记录中 src=${process.env.A2A_ROLE}_${process.env.A2A_IP}、edge_id=${selected.edge_id}、时间 ${startedAt} 之后的记录。原因：${outcome.reason}` }],
          details: { status: "unknown", edge_id: selected.edge_id, dst: selected.dst,
            src: `${process.env.A2A_ROLE}_${process.env.A2A_IP}`, started_at: startedAt },
        };
      }
      const receipt = outcome.receipt;
      const expectedSrc = `${process.env.A2A_ROLE}_${process.env.A2A_IP}`;
      if (receipt.src !== expectedSrc || receipt.dst !== selected.dst || receipt.text !== selected.text) {
        return {
          content: [{ type: "text", text: `发送结果未知：回执与选定边不一致。不要重试，请告知用户并由操作员检查 a2a queue ${selected.dst}。` }],
          details: { status: "unknown", edge_id: selected.edge_id, dst: selected.dst, msg_id: receipt.msg_id },
        };
      }
      return {
        content: [{ type: "text", text: `已入队：${receipt.dst}，msg_id=${receipt.msg_id}，${receipt.text}` }],
        details: { status: "sent", ...receipt },
      };
    },
  });
}
