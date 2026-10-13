// 页面二:智能体页(第 4 步实现;现在只显示工程信息与返回)。
import { api } from "./api.js";
import { clear, h } from "./dom.js";

export async function renderProject(root: HTMLElement, id: string): Promise<void> {
  clear(root);
  const back = h("a", { href: "#/", class: "back" }, "← 返回工程列表");
  try {
    const p = await api.project(id);
    root.append(back, h("h1", {}, p.name), h("p", { class: "muted" }, `herdr 会话 ${p.session} · 项目路径 ${p.path}`),
      h("p", { class: "placeholder" }, "智能体页(验证智能体 / 软件智能体与各 IP 的终端)在第 4 步实现。"));
  } catch (e) {
    root.append(back, h("p", { class: "error" }, (e as Error).message));
  }
}
