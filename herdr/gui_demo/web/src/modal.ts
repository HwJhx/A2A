// 对话框:挂在 body 下;关闭即移除。会话失效时 main.ts 会关掉所有对话框。
import { h } from "./dom.js";

export function modal(title: string, body: Node[], actions: HTMLElement[]): HTMLDialogElement {
  const dlg = h("dialog", { class: "modal" }, h("h3", {}, title), ...body, h("div", { class: "actions" }, ...actions));
  dlg.addEventListener("close", () => dlg.remove());
  document.body.append(dlg);
  dlg.showModal();
  return dlg;
}
