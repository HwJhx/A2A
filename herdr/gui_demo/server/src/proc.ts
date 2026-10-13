// 子进程:一律参数数组调用,不经 shell。
import { execFile } from "node:child_process";

export interface RunResult {
  code: number;
  stdout: string;
  stderr: string;
}

export function run(cmd: string, args: string[], opts: { env?: NodeJS.ProcessEnv; cwd?: string; timeoutMs?: number } = {}): Promise<RunResult> {
  return new Promise((resolve) => {
    execFile(cmd, args, { env: opts.env, cwd: opts.cwd, timeout: opts.timeoutMs ?? 60_000, maxBuffer: 16 * 1024 * 1024 },
      (err, stdout, stderr) => {
        if (!err) return resolve({ code: 0, stdout: String(stdout), stderr: String(stderr) });
        // 退出码是数字;启动失败(ENOENT)或超时被杀时记为 1,并把原因放进 stderr
        const code = typeof err.code === "number" ? err.code : 1;
        resolve({ code, stdout: String(stdout), stderr: String(stderr) || err.message });
      });
  });
}

export const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
