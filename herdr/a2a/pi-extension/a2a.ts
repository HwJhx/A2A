/**
 * a2a 的 pi 插件(阶段 6,设计见 herdr/claude/10-stage6-test-plan.md §2a)。
 *
 * 安装:复制到 fnx 的 agent/extensions/ 目录(例如 ~/.forenyx/fnx_dv/agent/extensions/a2a.ts),
 * fnx 启动时自动加载。只在 a2a 管理的 pane 里生效(由 `a2a agent spawn` 注入 A2A_* 环境变量);
 * 普通使用 fnx 时什么都不做。
 *
 *   a2a_send 工具   参数只有 edge_id,取值限定为本角色在拓扑里的出边。执行时用
 *                   execFile(A2A_PYTHON, ["-m", "a2a.cli", "send", edge_id]) 调用 Router:不经过 shell,
 *                   继承 pane 的身份环境变量。鉴权、填模板、确定目标、入队全部由 Router 完成,
 *                   插件不重复实现。
 *   tool_call 拦截  拦下 agent 通过 bash 直接调用 herdr 写入类命令(绕过 broker)。按命令文本匹配,
 *                   是防止"图省事"的软限制,真正的约束是 Router 鉴权与审计。
 *
 * 只用 Node 内置模块与可擦除的类型标注:pi 用 jiti 加载,单元测试用 node 直接加载。
 */
import { execFile, execFileSync } from "node:child_process";

export type Edge = { edge_id: string; dst: string; text: string };
type Config = { python: string; env: NodeJS.ProcessEnv; sendTimeoutMs: number };

const CLI_TIMEOUT_MS = 30_000;

// herdr 的写入类命令:往别的 pane 里写字或执行命令。读状态(agent get/list/read/wait)不拦。
export const HERDR_WRITE = /\bherdr\b[^\n;|&]*?\b(?:agent\s+prompt|pane\s+(?:send-text|send-keys|run))\b/;

export function isHerdrWrite(command: string): boolean {
  return HERDR_WRITE.test(command);
}

/** 不在 a2a 管理的 pane 里(缺少 spawn 注入的变量)时返回 null。 */
export function a2aConfig(env: NodeJS.ProcessEnv): Config | null {
  const { A2A_ROLE, A2A_IP, A2A_PROJECT_ID, A2A_PYTHON, A2A_SRC } = env;
  if (!A2A_ROLE || !A2A_IP || !A2A_PROJECT_ID || !A2A_PYTHON || !A2A_SRC) return null;
  // A2A_SEND_TIMEOUT_MS 只用于测试缩短超时
  const sendTimeoutMs = Number(env.A2A_SEND_TIMEOUT_MS) > 0 ? Number(env.A2A_SEND_TIMEOUT_MS) : CLI_TIMEOUT_MS;
  return { python: A2A_PYTHON, env: { ...env, PYTHONPATH: A2A_SRC, PYTHONDONTWRITEBYTECODE: "1" }, sendTimeoutMs };
}

export function listEdges(cfg: Config): Edge[] {
  const out = execFileSync(cfg.python, ["-m", "a2a.cli", "edges"],
    { env: cfg.env, encoding: "utf8", timeout: CLI_TIMEOUT_MS });
  return JSON.parse(out).edges as Edge[];
}

export function describe(edges: Edge[]): string {
  const lines = edges.map((e) => `- ${e.edge_id}:发给 ${e.dst},内容是「${e.text}」`);
  return [
    "通过 a2a 向同一 IP 的其他智能体发送一条固定内容的消息。",
    "只能选择下面列出的通信边;消息内容与接收方由边决定,不能自定义。",
    "需要通知对方时调用本工具,不要用 bash 或 herdr 命令直接给其他 pane 发消息。",
    "每次通知只调用一次;如果返回\"发送结果未知\",不要再次调用,把结果告诉用户。",
    "可用的边:",
    ...lines,
  ].join("\n");
}

// a2a.cli 的退出码:Router 明确拒绝(入队之前)
const EXIT_REJECTED = 4;

export type SendOutcome =
  | { kind: "sent"; receipt: { msg_id: string; dst: string; text: string; [k: string]: unknown } }
  | { kind: "rejected"; code: string; reason: string }
  | { kind: "unknown"; reason: string };

/** `a2a send` 的成功回执(见 cli.cmd_send):字段不全或不合法的一律不算发送成功。 */
export function isReceipt(r: any): boolean {
  const nonEmpty = (v: unknown) => typeof v === "string" && v.length > 0;
  return !!r && typeof r === "object" && nonEmpty(r.msg_id) && nonEmpty(r.dst) && nonEmpty(r.text)
    && r.state === "QUEUED" && Number.isInteger(r.queue_seq) && r.queue_seq >= 0;
}

/**
 * 运行 `a2a send`,把结果分成三类。只有"退出码 4 + Router 的拒绝 JSON"能确定消息没有入队;
 * 其他失败(超时、python 出错、输出看不懂……)都可能发生在入队之后(Router 先入队再写审计),归为结果未知。
 *
 * 不把工具的 AbortSignal 传给子进程:发送通常不到 1 秒,中途杀掉只会把确定的结果变成"未知"。
 * 代价是按 Esc 不能取消一次已经开始的发送。超时只作兜底。
 */
export function runSend(cfg: Config, edgeId: string): Promise<SendOutcome> {
  return new Promise((resolve) => {
    execFile(cfg.python, ["-m", "a2a.cli", "send", edgeId],
      { env: cfg.env, encoding: "utf8", timeout: cfg.sendTimeoutMs },
      (error, stdout, stderr) => {
        const errText = (stderr || "").trim();
        if (!error) {
          try {
            const receipt = JSON.parse(stdout);
            if (isReceipt(receipt)) {
              resolve({ kind: "sent", receipt });
              return;
            }
          } catch {
            // 落到下面:退出码 0 但回执看不懂
          }
          resolve({ kind: "unknown", reason: `回执无法解析:${stdout.trim().slice(0, 200)}` });
          return;
        }
        if ((error as any).code === EXIT_REJECTED) {
          try {
            const parsed = JSON.parse(errText);
            if (parsed && typeof parsed.rejected === "string") {
              resolve({ kind: "rejected", code: parsed.rejected, reason: String(parsed.reason ?? "") });
              return;
            }
          } catch {
            // 落到下面
          }
        }
        const how = (error as any).killed ? `超过 ${cfg.sendTimeoutMs} 毫秒被终止` : String(error.message);
        resolve({ kind: "unknown", reason: `${how}${errText ? `;${errText.split("\n").slice(-3).join(" ")}` : ""}` });
      });
  });
}

export default function (pi: any) {
  const cfg = a2aConfig(process.env);
  if (!cfg) return;

  pi.on("tool_call", async (event: any) => {
    if (event.toolName !== "bash") return undefined;
    const command = String(event.input?.command ?? "");
    if (isHerdrWrite(command)) {
      return { block: true, reason: "不允许直接调用 herdr 给其他 pane 写入;请用 a2a_send 工具发送消息" };
    }
    return undefined;
  });

  let edges: Edge[];
  try {
    edges = listEdges(cfg);
  } catch (err) {
    console.error(`[a2a] 读取可用通信边失败,不注册 a2a_send:${(err as Error).message}`);
    return;
  }
  if (edges.length === 0) return;

  pi.registerTool({
    name: "a2a_send",
    label: "A2A 发送",
    description: describe(edges),
    parameters: {
      type: "object",
      properties: {
        edge_id: { type: "string", enum: edges.map((e) => e.edge_id), description: "要使用的通信边 id" },
      },
      required: ["edge_id"],
      additionalProperties: false,
    },
    async execute(_toolCallId: string, params: { edge_id: string }) {
      const startedAt = new Date().toISOString();
      const outcome = await runSend(cfg, params.edge_id);
      if (outcome.kind === "sent") {
        const r = outcome.receipt;
        return {
          content: [{ type: "text", text: `已发送(排队投递中):发给 ${r.dst},msg_id=${r.msg_id},内容「${r.text}」` }],
          details: { status: "sent", ...r },
        };
      }
      if (outcome.kind === "rejected") {
        // 入队之前被拒绝,消息确定没有发出:按工具错误返回
        throw new Error(`已拒绝,消息没有发送:${outcome.code}: ${outcome.reason}`);
      }
      // 结果未知:消息可能已经入队。不按工具错误返回,避免模型把它当成"失败"去重试
      const dst = edges.find((e) => e.edge_id === params.edge_id)?.dst ?? "?";
      const src = `${process.env.A2A_ROLE}_${process.env.A2A_IP}`;
      return {
        content: [{
          type: "text",
          text: `发送结果未知:消息可能已经发出,也可能没有。不要再次调用 a2a_send,停止本次自动流程,`
            + `把这条结果告诉用户,由操作员核实(a2a queue ${dst},或在审计日志中查 src=${src}、edge_id=${params.edge_id}、`
            + `时间 ${startedAt} 之后的记录)。原因:${outcome.reason}`,
        }],
        details: { status: "unknown", edge_id: params.edge_id, src, dst, started_at: startedAt, reason: outcome.reason },
      };
    },
  });
}
