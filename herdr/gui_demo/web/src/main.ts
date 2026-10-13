// 前端入口:登录(读 #token=…,先从地址栏去掉,再 POST /api/login 兑换会话 cookie),然后按地址栏 # 路由。
// 页面加载时和地址栏 # 变化时都会检查 #token=(在同一标签页粘贴新登录地址不会重新加载页面)。
//   #/        页面一:工程列表
//   #/p/<id>[/dv|/sw]  页面二:智能体页(默认验证智能体)
import "./main.css";
import { ApiError, setUnauthorizedHandler } from "./api.js";
import { clear, h } from "./dom.js";
import { leaveProject, renderProject } from "./project.js";
import { renderProjects } from "./projects.js";

const app = document.getElementById("app")!;

/** 地址栏里有 #token=… 就兑换:先从地址栏去掉,再 POST /api/login。没有或兑换失败返回 false。 */
async function redeemFromHash(): Promise<boolean> {
  const m = location.hash.match(/^#token=([A-Za-z0-9_-]+)$/);
  if (!m) return false;
  history.replaceState(null, "", location.pathname + "#/");
  const r = await fetch("/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: m[1] }),
  });
  return r.status === 204;
}

async function loggedIn(): Promise<boolean> {
  return (await redeemFromHash()) || (await fetch("/api/me")).ok;
}

function showLoggedOut(): void {
  leaveProject(); // 停掉页面二的终端连接与定时刷新
  // 对话框挂在 body 下,不在 #app 里:会话失效时一并关掉,免得它们留在"未登录"页上面
  for (const dlg of document.querySelectorAll("dialog")) {
    dlg.close();
    dlg.remove();
  }
  clear(app);
  app.append(h("div", { class: "loggedout" }, h("h1", {}, "未登录"),
    h("p", {}, "登录地址只能用一次。请在 VM 上运行 ", h("code", {}, "gui_demo/bin/login-url"), " 获取新的登录地址。")));
}

async function route(): Promise<void> {
  const m = location.hash.match(/^#\/p\/(p[0-9a-f]{8})(?:\/(dv|sw))?$/);
  try {
    if (m) await renderProject(app, m[1], (m[2] as "dv" | "sw" | undefined) ?? "dv");
    else {
      leaveProject();
      await renderProjects(app);
    }
  } catch (e) {
    // 401 已由 api.ts 切到"未登录"页;其他错误照常抛出
    if (!(e instanceof ApiError && e.status === 401)) throw e;
  }
}

/** 地址栏变化:可能是换页(#/、#/p/<id>),也可能是在同一个标签页里粘贴了新的登录地址
 *  (只改 # 后面的部分,页面不会重新加载,所以这里也要兑换令牌)。 */
async function onHashChange(): Promise<void> {
  if (/^#token=/.test(location.hash)) {
    if (!(await loggedIn())) return showLoggedOut();
  }
  await route();
}

async function start(): Promise<void> {
  setUnauthorizedHandler(showLoggedOut);
  window.addEventListener("hashchange", () => void onHashChange());
  if (!(await loggedIn())) return showLoggedOut();
  await route();
}

void start();
