// 页面二:智能体页。左边导航是智能体种类(验证智能体 / 软件智能体),右边是该类下各 IP 的 agent,
// 每个 agent 一张卡片:状态、停止 / 重启、删除,下面是它的终端(窗口 tmux attach 到 herdr pane)。
// 每 3 秒刷新一次状态;已有卡片的终端不重建。地址:#/p/<工程 id>/<dv|sw>。
import { AgentRow, AgentsInfo, api, ApiError, Project, Role } from "./api.js";
import { clear, h } from "./dom.js";
import { modal } from "./modal.js";
import { TermView } from "./term.js";

const LIFECYCLE: Record<string, string> = { running: "运行中", stopped: "已停止", closed: "pane 已关闭", failed: "启动失败" };
const LIVE: Record<string, string> = { idle: "空闲", working: "工作中", blocked: "等待确认", done: "完成" };

interface Card {
  el: HTMLElement;
  term: TermView;
  status: HTMLElement;
  toggle: HTMLButtonElement;
  remove: HTMLButtonElement;
  row: AgentRow;
}

let current: AgentPage | null = null;

export async function renderProject(root: HTMLElement, id: string, role: Role): Promise<void> {
  if (current && current.projectId === id) return current.showRole(role);
  current?.dispose();
  current = null;
  clear(root);
  const back = h("a", { href: "#/", class: "back" }, "← 返回工程列表");
  let project: Project;
  try {
    project = await api.project(id);
  } catch (e) {
    root.append(back, h("p", { class: "error" }, (e as Error).message));
    throw e;
  }
  current = new AgentPage(root, project, back);
  await current.showRole(role);
}

/** 离开页面二时调用:关掉终端连接与定时刷新。 */
export function leaveProject(): void {
  current?.dispose();
  current = null;
}

class AgentPage {
  readonly projectId: string;
  private role: Role = "dv";
  private info: AgentsInfo | null = null;
  private readonly cards = new Map<string, Card>();
  private readonly nav: HTMLElement;
  private readonly title: HTMLElement;
  private readonly list: HTMLElement;
  private readonly banner: HTMLElement;
  private timer: number | undefined;
  private busy = false;

  constructor(root: HTMLElement, private readonly project: Project, back: HTMLElement) {
    this.projectId = project.id;
    this.nav = h("nav", { class: "side" });
    this.title = h("h2", {});
    this.list = h("div", { class: "agents" });
    this.banner = h("p", { class: "banner", hidden: true });
    const add = h("button", { class: "primary", type: "button", onclick: () => this.openAdd() }, "+ 新增 IP");
    root.append(
      back,
      h("header", { class: "page2-head" }, h("h1", {}, project.name),
        h("p", { class: "muted" }, "herdr 会话 ", h("code", {}, project.session), " · 项目路径 ", h("code", {}, project.path),
          " · 拓扑 ", h("code", {}, project.topology ?? "(旧工程:在数据目录的 state/topology.yaml)"))),
      h("div", { class: "page2" }, this.nav,
        h("section", { class: "main" }, h("div", { class: "main-head" }, this.title, add), this.banner, this.list)),
    );
    this.timer = window.setInterval(() => void this.refresh(), 3000);
  }

  async showRole(role: Role): Promise<void> {
    if (role !== this.role) {
      for (const id of [...this.cards.keys()]) this.dropCard(id);
      this.role = role;
    }
    await this.refresh();
  }

  private async refresh(): Promise<void> {
    if (this.busy) return;
    this.busy = true;
    try {
      this.info = await api.agents(this.projectId);
      this.banner.hidden = !this.info.warnings.length;
      this.banner.textContent = this.info.warnings.join(";");
      this.renderNav();
      this.renderCards();
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 401)) {
        this.banner.hidden = false;
        this.banner.textContent = `刷新失败:${(e as Error).message}`;
      }
    } finally {
      this.busy = false;
    }
  }

  private renderNav(): void {
    const info = this.info!;
    clear(this.nav);
    for (const role of Object.keys(info.roles) as Role[]) {
      const n = info.agents.filter((a) => a.role === role).length;
      this.nav.append(h("a", { href: `#/p/${this.projectId}/${role}`, class: role === this.role ? "active" : "" },
        h("span", {}, info.roles[role]), h("span", { class: "count" }, String(n))));
    }
    this.title.textContent = `${info.roles[this.role]}(${this.role})`;
  }

  private renderCards(): void {
    const mine = this.info!.agents.filter((a) => a.role === this.role).sort((a, b) => a.ip.localeCompare(b.ip));
    const ids = new Set(mine.map((a) => a.agentId));
    for (const id of [...this.cards.keys()]) if (!ids.has(id)) this.dropCard(id);
    if (!mine.length) {
      if (!this.list.querySelector(".empty")) {
        clear(this.list);
        this.list.append(h("p", { class: "empty muted" }, "这一类还没有 agent。点右上角\"新增 IP\"。"));
      }
      return;
    }
    this.list.querySelector(".empty")?.remove();
    for (const row of mine) {
      const card = this.cards.get(row.agentId) ?? this.newCard(row);
      this.updateCard(card, row);
    }
  }

  private newCard(row: AgentRow): Card {
    const term = new TermView(this.projectId, row.agentId);
    const status = h("span", { class: "status" });
    const toggle = h("button", { type: "button", class: "icon" });
    const remove = h("button", { type: "button", class: "icon danger" });
    const el = h("article", { class: "agent", "data-agent": row.agentId },
      h("div", { class: "agent-head" }, h("strong", {}, row.agentId), status, h("span", { class: "spacer" }), toggle, remove),
      term.el);
    const card: Card = { el, term, status, toggle, remove, row };
    toggle.addEventListener("click", () => void this.toggle(card));
    remove.addEventListener("click", () => this.openDelete(card));
    this.list.append(el);
    term.start();
    this.cards.set(row.agentId, card);
    return card;
  }

  private updateCard(card: Card, row: AgentRow): void {
    card.row = row;
    const running = row.lifecycle === "running";
    const live = row.live ? LIVE[row.live] ?? row.live : "herdr 未识别";
    card.status.textContent = `${LIFECYCLE[row.lifecycle] ?? row.lifecycle}${running ? ` · ${live}` : ""} · ${row.paneId ?? "无 pane"}`;
    card.status.className = `status ${running && row.live ? "ok" : "bad"}`;
    if (!card.toggle.disabled) card.toggle.textContent = running ? "停止" : "重启";
    const sameIp = this.info!.agents.filter((a) => a.ip === row.ip);
    card.remove.textContent = sameIp.length > 1 ? "删除整个 IP" : "删除";
    card.term.setReadonly(!(running && row.live), running ? "herdr 暂时识别不到这个 agent,终端只读" : "agent 没在运行,终端只读(重启后恢复)");
  }

  private dropCard(id: string): void {
    const card = this.cards.get(id);
    if (!card) return;
    card.term.dispose();
    card.el.remove();
    this.cards.delete(id);
  }

  private async toggle(card: Card): Promise<void> {
    const running = card.row.lifecycle === "running";
    card.toggle.disabled = true;
    card.toggle.textContent = running ? "停止中…" : "重启中…";
    try {
      await (running ? api.stopAgent(this.projectId, card.row.agentId) : api.restoreAgent(this.projectId, card.row.agentId));
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 401)) alert((e as Error).message);
    } finally {
      card.toggle.disabled = false;
      await this.refresh();
    }
  }

  private openAdd(): void {
    const roleName = this.info?.roles[this.role] ?? this.role;
    const ip = h("input", { name: "ip", maxlength: "32", required: true, placeholder: "例如 uart", autocomplete: "off", spellcheck: "false" });
    const hint = h("p", { class: "muted small" }, `agent 名 = ${this.role}_<IP>,最长 32 个字符;IP 名用小写字母开头,只含小写字母、数字、_、-。`);
    const msg = h("p", { class: "msg" });
    ip.addEventListener("input", () => {
      const name = ip.value.trim();
      const agentId = `${this.role}_${name || "<IP>"}`;
      if (this.info?.agents.some((a) => a.agentId === agentId)) hint.textContent = `${agentId} 已存在。`;
      else if (this.info?.ips.includes(name)) hint.textContent = `IP ${name} 已在拓扑里(另一类智能体在用):会为它新增 ${agentId}。`;
      else hint.textContent = `agent 名 = ${agentId}`;
    });
    const ok = h("button", { class: "primary", type: "button" }, "新增");
    const cancel = h("button", { type: "button" }, "取消");
    const dlg = modal(`在"${roleName}"下新增 IP`, [h("label", {}, "IP 名", ip), hint, msg], [cancel, ok]);
    cancel.addEventListener("click", () => dlg.close());
    ok.addEventListener("click", async () => {
      ok.disabled = cancel.disabled = true;
      msg.className = "msg";
      msg.textContent = `启动 ${this.role}_${ip.value.trim()} 中(约 10 秒)…`;
      try {
        const r = await api.addAgent(this.projectId, { role: this.role, ip: ip.value.trim() });
        dlg.close();
        if (r.warnings.length) alert(r.warnings.join("\n"));
        await this.refresh();
      } catch (e) {
        msg.className = "msg error";
        msg.textContent = `新增失败:${(e as Error).message}`;
        ok.disabled = cancel.disabled = false;
      }
    });
    ip.focus();
  }

  private openDelete(card: Card): void {
    const ipName = card.row.ip;
    const sameIp = this.info!.agents.filter((a) => a.ip === ipName).map((a) => a.agentId).sort();
    const msg = h("p", { class: "msg" });
    const ok = h("button", { class: "danger", type: "button" }, "确认删除");
    const cancel = h("button", { type: "button" }, "取消");
    const body: Node[] = sameIp.length > 1
      ? [h("p", {}, `IP ${ipName} 下还有别的智能体。a2a 只能按 IP 安全删除(先排空在途消息),所以会一起删除:`),
        h("p", {}, ...sameIp.flatMap((a, i) => [i ? "、" : "", h("code", {}, a)]))]
      : [h("p", {}, "将删除 ", h("code", {}, card.row.agentId), `,并把 IP ${ipName} 从拓扑里去掉。`)];
    body.push(h("p", { class: "muted small" }, "队列里还有发给它们的消息时会拒绝删除。项目路径下的工作目录不会删除。"), msg);
    const dlg = modal(sameIp.length > 1 ? `删除整个 IP ${ipName}?` : `删除 ${card.row.agentId}?`, body, [cancel, ok]);
    cancel.addEventListener("click", () => dlg.close());
    ok.addEventListener("click", async () => {
      ok.disabled = cancel.disabled = true;
      msg.className = "msg";
      msg.textContent = "删除中(先排空在途消息)…";
      try {
        const r = await api.deleteIp(this.projectId, ipName, sameIp);
        dlg.close();
        if (r.warnings.length) alert(r.warnings.join("\n"));
        await this.refresh();
      } catch (e) {
        msg.className = "msg error";
        msg.textContent = `删除失败:${(e as Error).message}`;
        ok.disabled = cancel.disabled = false;
      }
    });
  }

  dispose(): void {
    window.clearInterval(this.timer);
    for (const id of [...this.cards.keys()]) this.dropCard(id);
  }
}
