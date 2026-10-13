// 页面一:工程列表。卡片 = 已有工程(点击进入页面二),最后一张是"新建项目"。每张工程卡片可删除。
import { api, ApiError, Project } from "./api.js";
import { avatar, clear, h } from "./dom.js";
import { modal } from "./modal.js";

let home = "";

export async function renderProjects(root: HTMLElement): Promise<void> {
  clear(root);
  const grid = h("div", { class: "grid" });
  root.append(
    h("header", { class: "brand" }, h("div", { class: "logo" }, "AI"), h("div", {},
      h("h1", {}, "AI原生芯片开发平台"), h("p", { class: "sub" }, "测试用 demo · 工程 = 一个 herdr 会话"))),
    grid,
  );
  if (!home) home = (await api.config()).home;
  await refresh(grid);
}

async function refresh(grid: HTMLElement): Promise<void> {
  let list: Project[];
  try {
    list = await api.projects();
  } catch (e) {
    clear(grid);
    grid.append(h("p", { class: "error" }, `读取工程列表失败:${(e as Error).message}`));
    return;
  }
  clear(grid);
  for (const p of list) grid.append(projectCard(p, grid));
  grid.append(h("button", { class: "card new", type: "button", onclick: () => openCreate(grid) },
    h("div", { class: "plus" }, "+"), h("h2", {}, "新建项目"), h("p", { class: "muted" }, "一个项目,一个目录")));
}

function projectCard(p: Project, grid: HTMLElement): HTMLElement {
  const healthy = p.herdr && p.broker?.running;
  const status = p.herdr === null ? "herdr 状态查询失败" : !p.herdr ? "herdr 会话未运行" : !p.broker?.running ? "broker 未运行" : "运行中";
  return h("div", { class: "card", role: "link", tabindex: "0", "data-id": p.id,
    onclick: () => { location.hash = `#/p/${p.id}`; },
    onkeydown: (e: Event) => { if ((e as KeyboardEvent).key === "Enter") location.hash = `#/p/${p.id}`; } },
  h("div", { class: "card-top" }, avatar(p.name),
    h("button", { class: "icon danger", type: "button", title: "删除工程", "aria-label": `删除工程 ${p.name}`,
      onclick: (e: Event) => { e.stopPropagation(); openDelete(p, grid); } }, "删除")),
  h("h2", {}, p.name),
  h("p", { class: "muted" }, p.description || "还没有简介"),
  h("p", { class: "path", title: p.path }, p.path),
  h("div", { class: "card-foot" }, h("span", { class: healthy ? "dot ok" : "dot bad" }, status),
    p.herdr === null
      // 状态查不清时恢复会被后端拒绝(不在状态不明时做清理):只给"刷新状态"
      ? h("button", { class: "icon", type: "button", title: "herdr 状态查询失败,重新查询",
        onclick: (e: Event) => { e.stopPropagation(); void refresh(grid); } }, "刷新状态")
      : !healthy
      ? h("button", { class: "icon", type: "button", title: "重新启动 herdr 会话(没在运行时)、恢复 agent、启动 broker",
        onclick: (e: Event) => { e.stopPropagation(); void recover(p, grid, e.currentTarget as HTMLButtonElement); } }, "恢复")
      : h("span", { class: "enter" }, "进入")));
}

async function recover(p: Project, grid: HTMLElement, btn: HTMLButtonElement): Promise<void> {
  btn.disabled = true;
  btn.textContent = "恢复中…";
  try {
    const r = await api.recoverProject(p.id);
    if (r.warnings.length) alert(`${r.warnings.join("\n")}\n\n${JSON.stringify(r.result, null, 2)}`);
  } catch (e) {
    alert(`恢复失败:${(e as Error).message}`);
  }
  await refresh(grid);
}

function openCreate(grid: HTMLElement): void {
  const name = h("input", { name: "name", maxlength: "40", required: true, placeholder: "例如 SoC-A", autocomplete: "off" });
  const dir = h("input", { name: "path", required: true, placeholder: `${home}/projects/…`, autocomplete: "off" });
  const desc = h("input", { name: "description", maxlength: "200", placeholder: "可不填", autocomplete: "off" });
  const msg = h("p", { class: "msg" });
  name.addEventListener("input", () => {
    if (!dir.dataset.touched) dir.value = name.value.trim() ? `${home}/projects/${name.value.trim().replace(/[\s/]+/g, "_")}` : "";
  });
  dir.addEventListener("input", () => { dir.dataset.touched = "1"; });
  const ok = h("button", { class: "primary", type: "button" }, "创建");
  const cancel = h("button", { type: "button" }, "取消");
  const dlg = modal("新建项目", [
    h("label", {}, "工程名称(即 herdr workspace 名)", name),
    h("label", {}, "项目路径(VM 上的绝对路径,agent 的工作目录放在这里)", dir),
    h("label", {}, "简介", desc),
    msg,
  ], [cancel, ok]);
  cancel.addEventListener("click", () => dlg.close());
  ok.addEventListener("click", async () => {
    ok.disabled = cancel.disabled = true;
    msg.className = "msg";
    msg.textContent = "创建中:建状态目录、写拓扑、启动 herdr 会话与 broker(约 5 秒)…";
    try {
      await api.createProject({ name: name.value, path: dir.value, description: desc.value });
      dlg.close();
      await refresh(grid);
    } catch (e) {
      msg.className = "msg error";
      const step = e instanceof ApiError && e.step ? `(失败步骤:${e.step})` : "";
      msg.textContent = `创建失败${step}:${(e as Error).message}`;
      ok.disabled = cancel.disabled = false;
    }
  });
  name.focus();
}

function openDelete(p: Project, grid: HTMLElement): void {
  const msg = h("p", { class: "msg" });
  const ok = h("button", { class: "danger", type: "button" }, "确认删除");
  const cancel = h("button", { type: "button" }, "取消");
  const dlg = modal(`删除工程"${p.name}"?`, [
    h("p", {}, "将停止并删除:该工程的所有 agent、herdr 会话 ", h("code", {}, p.session), "、broker,以及工程的 a2a 状态(消息记录、审计日志)。"),
    h("p", {}, "不会删除项目路径 ", h("code", {}, p.path), "(里面是 agent 的工作文件)。"),
    msg,
  ], [cancel, ok]);
  cancel.addEventListener("click", () => dlg.close());
  ok.addEventListener("click", async () => {
    ok.disabled = cancel.disabled = true;
    msg.className = "msg";
    msg.textContent = "删除中…";
    try {
      const r = await api.deleteProject(p.id);
      dlg.close();
      if (r.warnings.length) alert(`已删除,但有提示:\n${r.warnings.join("\n")}`);
      await refresh(grid);
    } catch (e) {
      msg.className = "msg error";
      msg.textContent = `删除失败:${(e as Error).message}`;
      ok.disabled = cancel.disabled = false;
    }
  });
}
