# gui_demo · 测试用 Web GUI demo

用来和负责 web GUI 的同事讨论"GUI + a2a + herdr"怎么对接的 demo。方案与全部实施、审核记录见 `../claude/21-gui-demo-plan.md`。

- **页面一 · 工程列表**:新建工程(名称、项目路径、简介)、删除工程、herdr 会话停了之后"恢复"。一个工程 = 一个 a2a 状态目录 + 一份拓扑 + 一个独占的 herdr 会话 + 一个 broker。
- **页面二 · 智能体页**:左边"验证智能体 / 软件智能体",右边该类下每个 IP 一张卡片:状态、停止 / 重启、删除,卡片下是这个 agent 的真实终端(浏览器 xterm.js ⇄ WebSocket ⇄ 窗口 tmux ⇄ `herdr agent attach`)。

这是 demo,不是产品:只有 dv、sw 两类智能体,没有账号与多用户,样式只是大致仿照截图。

## 运行环境

- 后端跑在 OrbStack 的 Ubuntu VM 上(需要 herdr、tmux、python3、Node 22;VM 上已有),浏览器在 Mac 上。
- 代码在 Mac 上编辑,部署时复制到 VM 本地目录 `~/gui_demo_run`(避开共享目录读到旧文件的问题)。
- a2a 直接用 Mac 仓库里的 `herdr/a2a/src`(VM 经 OrbStack 挂载读取,只读使用)。

## 部署、启动、停止

```bash
# Mac 上:同步源码到 VM、按 lockfile 安装依赖、构建
gui_demo/bin/deploy-vm.sh

# VM 上(orb -m ubuntu 进入):后台启动,打印一次性登录地址
~/gui_demo_run/bin/start
# 停止(停 broker、断开终端;herdr 会话与 agent 保留,下次启动后继续可用)
~/gui_demo_run/bin/stop
```

登录:在 Mac 的浏览器里打开 `bin/start` 打印的地址,形如 `http://192.168.139.41:8765/#token=…`。

- 这个地址**只能用一次**;之后在同一浏览器里刷新仍是登录状态。要新地址(换浏览器、后端重启后):在 VM 上运行 `~/gui_demo_run/bin/login-url`,把打印的地址粘进浏览器(同一标签页里粘也可以)。
- 用 IP 访问:这台 Mac 上 `ubuntu.orb.local` 会被代理软件解析成 198.18.x.x,用不了。VM 的地址是 DHCP 分配的,VM 重启后可能变;`bin/start` 打印的是实际地址。
- 后端只监听 VM 在 OrbStack 内部网络上的地址,局域网访问不到。

数据目录 `~/.a2a_gui_demo/`(权限 0700):`projects.json`(工程列表)、`projects/<id>/state/`(各工程的 a2a 状态)、`server.log`、`server.pid`(内容是"pid 启动时间",`start` / `stop` 核对身份,陈旧的不会被误认)、`admin.sock`(`login-url` 用)。

`bin/start` 默认最多等 120 秒就绪(工程多时启动要逐个拉起 broker),可用 `GUI_DEMO_START_TIMEOUT=秒` 调整;超时会结束后台进程再报错。删除工程会删它的状态目录,**不删项目路径**(里面是 agent 的工作目录)。

## 使用提示

- 新增 IP 会启动真实的 fnx_dv / fnx_sw;**启动本身不调用模型**。只有在终端里输入并按回车,或 agent 之间互发消息时才会调用模型。
- agent 停止后终端变成只读(前端与后端都拦):这时窗口里是 pane 自己的 shell,不能往里输入。
- 同一个 agent 可以在多个浏览器标签页里同时打开;输入都会进同一个终端,终端尺寸跟随最近有操作的那个窗口。
- 删除:同一 IP 的另一类智能体还在时只能"删除整个 IP"(a2a 只有 IP 级的安全删除:先排空在途消息)。队列里还有发给它们的消息时会拒绝删除。
- VM 或 herdr 重启后,工程卡片显示"herdr 会话未运行"和"恢复"按钮:点它会重新起 herdr 会话、`a2a agent recover`、起 broker,已打开的终端会自己接上。broker 没在运行时(比如恢复中途失败)也会显示"恢复",可以再点。
- pi 不用全屏方式绘制,终端尺寸变化时会留下一份旧的状态栏残影;重启 agent 时窗口里会看到 a2a 输入的启动命令。都是 pi / a2a 本身的行为。

## 已知限制

见 `../claude/21-gui-demo-plan.md` §8,主要几条:

- 单独删除一类智能体(同 IP 另一类还在时)不支持;删除 IP 时 GUI 的核对与 `a2a ip remove` 之间没有跨进程锁。
- 从外部用 a2a 命令行停止 agent 时,终端变只读要等下一次状态查询(约 2 秒)。
- 没有账号登录与多用户;只有一次性登录地址 + 会话 cookie。

## 测试

```bash
# VM 上(需要真实的 herdr 与 tmux;用 guidemo_test_ 前缀、临时数据目录、127.0.0.1 随机端口,测完自己清理)
cd ~/gui_demo_run && npm test
```

测试用 a2a 集成测试的假 agent(以 pi 的名字运行,herdr 识别为 agent,不调用模型)。`test/fakes/` 下是注入失败用的 herdr、tmux 包装。

## 代码结构

| 路径 | 内容 |
| --- | --- |
| `server/src/main.ts` | 入口:单实例锁 → admin.sock → HTTP → 恢复工程 → 打印登录地址;退出时回收 |
| `server/src/config.ts` | 配置(环境变量 `GUI_DEMO_*`,大多只给测试用) |
| `server/src/http.ts` | HTTP 路由、静态文件、认证与 Origin 校验、WebSocket 入口 |
| `server/src/auth.ts`、`admin.ts` | 一次性令牌与会话 cookie;单实例锁与 admin.sock |
| `server/src/a2a.ts`、`server/py/topology_tool.py` | 工程环境(`A2A_STATE_DIR` 等)与 a2a 调用;建拓扑 |
| `server/src/projects.ts`、`herdr.ts`、`broker.ts` | 工程新建 / 删除 / 恢复;herdr 会话与 tmux;broker 子进程 |
| `server/src/agents.ts`、`fence.ts` | agent 新增 / 停止 / 重启 / 删除 IP;操作期间撤销终端写权限 |
| `server/src/windows.ts`、`terminal.ts` | 每个 agent 的窗口 tmux;终端桥(node-pty、输入闸门、名额) |
| `web/src/` | 前端:`main.ts`(登录、路由)、`projects.ts`(页面一)、`project.ts`(页面二)、`term.ts`(xterm.js) |
| `bin/` | `deploy-vm.sh`、`sync-to-vm.sh`(Mac 上);`start`、`stop`、`login-url`(VM 上) |
