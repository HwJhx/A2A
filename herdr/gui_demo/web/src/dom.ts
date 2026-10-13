// 极简的 DOM 构造。文字一律走 textContent,不用 innerHTML(工程名、路径等都是用户输入)。

type Child = Node | string | null | undefined | false;
type Attrs = Record<string, string | boolean | ((e: Event) => void) | undefined>;

export function h<K extends keyof HTMLElementTagNameMap>(tag: K, attrs: Attrs = {}, ...children: Child[]): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === false) continue;
    if (typeof v === "function") el.addEventListener(k.replace(/^on/, ""), v);
    else if (v === true) el.setAttribute(k, "");
    else if (k === "class") el.className = v;
    else el.setAttribute(k, v);
  }
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    el.append(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return el;
}

export function clear(el: Element): void {
  while (el.firstChild) el.removeChild(el.firstChild);
}

/** 卡片头像:取名字开头(英文取前两个字母大写,中文取第一个字),颜色按名字固定。 */
export function avatar(name: string): HTMLElement {
  const chars = [...name.trim()];
  const latin = /^[A-Za-z0-9]/.test(chars[0] ?? "");
  const text = latin ? chars.slice(0, 2).join("").toUpperCase() : (chars[0] ?? "?");
  let hash = 0;
  for (const c of name) hash = (hash * 31 + c.codePointAt(0)!) >>> 0;
  return h("div", { class: `avatar c${hash % 6}` }, text);
}
