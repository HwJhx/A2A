// 前端入口。第 1 步只有登录:读 #token=…,先从地址栏去掉,再 POST /api/login 兑换会话 cookie。
import "./main.css";

const app = document.getElementById("app")!;

async function login(): Promise<boolean> {
  const m = location.hash.match(/^#token=([A-Za-z0-9_-]+)$/);
  if (m) {
    history.replaceState(null, "", location.pathname);
    const r = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: m[1] }),
    });
    if (r.status === 204) return true;
  }
  return (await fetch("/api/me")).ok;
}

async function start(): Promise<void> {
  if (!(await login())) {
    app.textContent = "未登录或登录地址已用过:请在 VM 上运行 gui_demo/bin/login-url 获取新的登录地址。";
    return;
  }
  app.textContent = "已登录(页面在第 2 步实现)";
}

void start();
