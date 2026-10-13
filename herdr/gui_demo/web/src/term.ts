// 一个 agent 的终端:xterm.js ⇄ WebSocket(/ws/term)⇄ 后端 node-pty ⇄ 窗口 tmux ⇄ herdr agent attach。
// 断开后 3 秒自动重连(除非已销毁)。agent 没在运行时设为只读:那时窗口里可能是 pane 的 shell(20 号文档 §7)。
import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { h } from "./dom.js";

export class TermView {
  readonly el: HTMLElement;
  private readonly host: HTMLElement;
  private readonly note: HTMLElement;
  private readonly term: Terminal;
  private readonly fit = new FitAddon();
  private ws: WebSocket | null = null;
  private disposed = false;
  private retry: number | undefined;
  private readonly observer: ResizeObserver;
  private readonly: boolean = false;

  constructor(private readonly projectId: string, private readonly agentId: string) {
    this.host = h("div", { class: "term-host" });
    this.note = h("div", { class: "term-note" });
    this.el = h("div", { class: "term" }, this.host, this.note);
    this.term = new Terminal({
      fontSize: 13,
      fontFamily: "ui-monospace, Menlo, 'DejaVu Sans Mono', monospace",
      cursorBlink: true,
      scrollback: 2000,
      theme: { background: "#0b111b", foreground: "#d8e2f0" },
    });
    this.term.loadAddon(this.fit);
    this.term.onData((d) => {
      if (this.readonly || this.ws?.readyState !== WebSocket.OPEN) return;
      // 后端单帧上限 64KB:大段粘贴分块发送;不在代理对(emoji 等)中间切开
      for (let i = 0; i < d.length;) {
        let j = Math.min(i + 8192, d.length);
        if (j < d.length && /[\uD800-\uDBFF]/.test(d[j - 1])) j -= 1;
        this.ws.send(JSON.stringify({ t: "in", d: d.slice(i, j) }));
        i = j;
      }
    });
    this.observer = new ResizeObserver(() => this.resize());
  }

  /** 挂到页面上之后调用(xterm 需要有尺寸的容器)。 */
  start(): void {
    this.term.open(this.host);
    this.observer.observe(this.host);
    this.resize();
    this.connect();
  }

  setReadonly(readonly: boolean, reason = ""): void {
    this.readonly = readonly;
    this.term.options.disableStdin = readonly;
    this.el.classList.toggle("readonly", readonly);
    this.showNote(readonly ? reason : "");
  }

  private showNote(text: string): void {
    this.note.textContent = text;
    this.note.hidden = !text;
  }

  private resize(): void {
    if (this.disposed || !this.host.clientWidth) return;
    try {
      this.fit.fit();
    } catch {
      return;
    }
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ t: "size", c: this.term.cols, r: this.term.rows }));
  }

  private connect(): void {
    if (this.disposed) return;
    const q = new URLSearchParams({ project: this.projectId, agent: this.agentId, cols: String(this.term.cols), rows: String(this.term.rows) });
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/term?${q}`);
    this.ws = ws;
    ws.onopen = () => {
      if (!this.readonly) this.showNote("");
      this.resize();
    };
    ws.onmessage = (e) => this.term.write(typeof e.data === "string" ? e.data : "");
    ws.onclose = () => {
      if (this.disposed || this.ws !== ws) return;
      this.showNote("终端连接已断开,3 秒后重连…");
      this.retry = window.setTimeout(() => this.connect(), 3000);
    };
  }

  focus(): void {
    this.term.focus();
  }

  dispose(): void {
    this.disposed = true;
    window.clearTimeout(this.retry);
    this.observer.disconnect();
    this.ws?.close();
    this.term.dispose();
  }
}
