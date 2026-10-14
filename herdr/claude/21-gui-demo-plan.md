# 21 · 测试用 Web GUI demo:方案与实施计划(2026-10-13)

> 目的:做一个能在 Mac 浏览器里实际操作的 demo,用来和负责 web GUI 的同事讨论"GUI + a2a + herdr"怎么对接。它验证 20 号文档的结论:GUI 把创建交给 a2a、每个 agent 一个 tmux 窗口、窗口里用 `herdr agent attach` 显示 agent。**demo 不是产品**,不做账号登录(只有访问令牌)、多用户、美观细节。
>
> 代码位置:`herdr/gui_demo/`。不修改 `herdr/a2a`(只调用它的命令行,以及在建工程时用它的 `TopologyStore` 写拓扑)。

## 1. 需求(用户 2026-10-13)

参考两张现有 GUI 截图:

- **页面一 · 工程列表**:卡片列出已有工程;最右的"+ 新建项目"弹出对话框,输入工程名称(workspace 名)和项目路径;可以删除工程;点卡片进入页面二。
- **页面二 · 智能体页**:左右布局。左边导航栏是智能体种类(只做"验证智能体""软件智能体");右边显示该种类下已有的 IP agent,每个 agent 一个终端窗口(tmux,attach 到 herdr 的 pane)。可以在某类智能体下点按钮新增、删除 agent。
- 在 VM 上运行,用户在 Mac 浏览器输入地址就能实际操作、验收。

## 2. 界面概念与 a2a / herdr 的对应

| GUI 上的东西 | 对应 | 新建 | 删除 |
| --- | --- | --- | --- |
| 工程卡片 | 一个 a2a 工程:状态目录 + 拓扑(`workspace_label` = 卡片名)+ 一个独占的 herdr 会话(按 20 号文档"每个工程一个 herdr 会话") + 一个 broker | 见 §4.1 | 见 §4.2 |
| 导航栏的"验证智能体""软件智能体" | 拓扑里的角色 `dv`、`sw`;herdr 里各一个 tab(名为角色的 label) | 固定,建工程时写进拓扑 | 不能删 |
| 右侧一个 IP 实例 | 一个 agent `角色_IP`(如 `dv_uart`),herdr 里一个 pane | `a2a ip add <ip> --roles <角色> --cwd <项目路径>/{role}_{ip}` | 见 §4.4 |
| 实例的终端窗口 | 一个 tmux 会话,里面循环运行 `herdr --session <会话> agent attach <agent 名>`;浏览器用 xterm.js 显示这个 tmux | 第一次打开时建 | 删除 agent 或删除工程时清理 |

## 3. 架构

```
Mac 浏览器 ──HTTP / WebSocket──▶ VM 上的 Node 后端(gui_demo/server)
                                    ├─ 调 a2a 命令行(execFile,参数数组,不经 shell)
                                    ├─ 管 tmux:每个工程一个"herdr 宿主"tmux,每个 agent 一个"窗口"tmux
                                    ├─ 每个 broker 一个子进程(a2a broker run --session <会话>)
                                    └─ 终端桥:node-pty 运行 `tmux attach -t <窗口 tmux>`,经 WebSocket 接到 xterm.js
```

- **前端**:TypeScript + CSS,不用框架;xterm.js(`@xterm/xterm` + `@xterm/addon-fit`)显示终端;esbuild 打包。
- **后端**:Node 22(VM 上已有 v22.20.0)+ TypeScript;`ws`(WebSocket)、`node-pty`(伪终端,需要在 VM 上编译;VM 已有 gcc / g++ / make / python3)。HTTP 用 Node 自带的 `http`,不引入 express。依赖版本全部锁定。
- **保留 tmux 这一层**:与同事现在的结构一致;另外 herdr 一个终端同时只允许一个 attach 客户端,而一个 tmux 会话可以被多个浏览器窗口同时打开,不会互相挤掉。共享时要注意:多个窗口的输入都会进同一个 agent 终端(没有"控制权"区分);多个窗口尺寸不同时 tmux 取最小的,agent 终端尺寸也随之变小。demo 不做控制权管理,README 里说明。
- **断开自动重连**:窗口 tmux 里跑的是循环:`herdr agent attach` 退出后显示"agent 未运行 / 已断开,3 秒后重试"再重试。这样 stop / restore / close 后重开 / 会话重启后 recover,窗口都能自己接上(20 号文档 §7 实测过各情况的 attach 行为)。
- **工程状态**:后端在 VM 上的 `~/.a2a_gui_demo/` 里保存:
  - `projects.json`:工程列表(id、名称、项目路径、herdr 会话名、创建时间);
  - `projects/<id>/state/`:该工程的 a2a 状态目录(`A2A_STATE_DIR`)。
  - 用户填的"项目路径"用作 agent 工作目录的父目录(每个 agent 一个子目录 `<项目路径>/<role>_<ip>`),不放 a2a 状态。
- **每个工程的 a2a 环境必须贯穿所有子进程**(Codex 审核阻断 1):a2a 用 `A2A_STATE_DIR` 选注册表 / spool / 审计,用 `A2A_TOPOLOGY` 选拓扑,`--session` 只选 herdr 会话。只传 `--session` 会让多个工程共用默认状态目录和拓扑。所以后端用唯一的一个函数构造工程环境:`A2A_STATE_DIR=<工程状态目录>`、`A2A_TOPOLOGY=<工程状态目录>/topology.yaml`、`HERDR_SESSION=guidemo_<id>`;**只对支持 `--session` 的命令**(`agent …`、`ip …`、`broker run`)再显式传 `--session guidemo_<id>`,`queue`、`resolve`、`status` 等没有这个参数,靠环境变量(第二轮复核应修 1);所有 a2a 命令、Python 辅助脚本、broker 都只通过这个函数启动,不继承后端自己的 `A2A_*` / `HERDR_SESSION`(先从环境里删掉再设)。启动前校验三者一致:状态目录属于这个工程 id、拓扑的 `project_id` 等于工程 id、会话名等于 `guidemo_<id>`。测试里用两个工程交叉核对注册表、拓扑互不串。
- **名字规则**:工程 id 由后端生成(`p` + 短随机串,符合 a2a 的 slug 规则);herdr 会话名 `guidemo_<id>`,与用户已有的会话(default、test1、a2a_verify)和 Codex 的会话不会重名。IP 名由用户输入,按 a2a 规则校验 `^[a-z][a-z0-9_-]{0,31}$`,且 `角色_IP` ≤ 32 个字符。

## 4. 主要流程

### 4.1 新建工程

1. 校验名称(非空、≤ 40 字)、项目路径(绝对路径;不存在就创建;必须在 VM 用户的家目录下)。
2. 生成工程 id、会话名,建状态目录,用 a2a 的 `TopologyStore.create` 写拓扑(由后端调用一段 Python 辅助脚本 `server/py/init_topology.py`,a2a 没有建拓扑的命令):
   - 角色 `dv`(label "验证智能体",launcher `~/.forenyx/fnx_dv/bin/fnx_dv`)、`sw`(label "软件智能体",launcher `~/.forenyx/fnx_sw/bin/fnx_sw`);
   - IP 列表为空;边沿用阶段 8 的 `dv_done`、`sw_test_pass`、`sw_test_fail` 三条(模板里保留"不要真的去做")。
3. 启动 herdr 会话:新建一个隐藏的 tmux 会话运行 `herdr session attach guidemo_<id>`(herdr 0.9.3 没有单独的 start 命令,之前的实测都是这样起服务),等 `pane list` 可用。
4. 启动该工程的 broker 子进程。
5. 写入 `projects.json`。任一步失败就按相反顺序清理已做的部分,并在界面上报出是哪一步失败。

### 4.2 删除工程

界面二次确认。然后:停 broker → 对每个 agent `a2a agent purge` → 关掉该工程的所有窗口 tmux → `herdr session stop`、`session delete` → 关掉宿主 tmux → 删状态目录 → 从 `projects.json` 去掉。**不删项目路径**(里面是 agent 的工作文件),界面上说明这一点。

### 4.3 新增 agent(在某类智能体下)

输入 IP 名 → 后端执行 `a2a ip add <ip> --roles <当前角色> --cwd <项目路径>/{role}_{ip} --session <会话>`(可重复执行;IP 已在拓扑里、只是另一个角色没起时也用它)→ 建该 agent 的窗口 tmux → 界面刷新出新卡片。`ip add` 要等 fnx 启动完,可能十几秒,界面显示"启动中"。

### 4.4 删除 agent

**只用有排空的 `a2a ip remove`,不用 `agent purge`**(Codex 审核应修 3):`agent purge` 不检查队列,如果发给这个 agent 的消息正在排队或投递,消息会变成"目标缺失 / 不确定"并暂停队列,而 demo 没有裁定界面;先检查再 purge 也挡不住检查之后新发来的消息。a2a 的排空只有 IP 级(`ip remove`),没有单个 agent 级的。所以:
- 这是该 IP 的最后一个 agent:按钮"删除",确认后执行 `a2a ip remove <ip> --force`(排空、等在途投递结束、检查队列为空、清除、从拓扑删除);
- 同一个 IP 的另一个角色还有 agent:单独删除这一个**暂不支持**,按钮改为"删除整个 IP",确认框写明会同时删掉 `dv_<ip>` 和 `sw_<ip>`,执行同一条 `ip remove`;
- `ip remove` 失败(超时、队列不空)时,界面显示 a2a 的原话和"在 VM 上用 `a2a queue` / `a2a ip undrain` 处理"的提示,不自动重试;
- 成功后关掉相关 agent 的窗口 tmux。

"只删一类智能体"需要 a2a 提供单个 agent 级的排空删除,这是和前端同事讨论时要提出的缺口,本 demo 不在 a2a 里加。

### 4.5 状态显示

页面二每 3 秒请求一次 `a2a agent list --session <会话>`,每张卡片显示 agent 名、pane 号、生命周期(running / stopped …)和 herdr 状态(idle / working / blocked)。卡片上另有"停止""重启"按钮(`agent stop` / `agent restore`),用来演示窗口在 stop / restore 时的表现。

## 5. 网络与安全

终端窗口能在 agent 里输入,等于一个远程执行入口,只限制网络不够(Codex 审核阻断 2)。三层:

1. **限制网络**:后端只监听 VM 在 OrbStack 内部网络上的地址(当前 `192.168.139.41`,即 `ubuntu.orb.local`),端口 8765。第 0 步实测:Mac 上 `lsof -iTCP:8765 -sTCP:LISTEN` 看 OrbStack 是否在 Mac 上另开了监听(尤其是 `*:8765` 这种对局域网的);如果有,改为只监听 VM 的 127.0.0.1,并在 README 写明从 Mac 访问的方式(OrbStack 转发到 Mac 的 localhost,或 `ssh -L`)。
2. **一次性登录令牌 + 会话 cookie**(第二轮复核应修 2:令牌不能长期出现在 URL 查询参数里,会进浏览器历史、日志、Referer):
   - 后端生成 32 字节随机的**一次性**登录令牌,**只存在内存里,不写文件**,在启动输出里打印 `http://ubuntu.orb.local:8765/#token=…`。令牌放在 URL 的 `#` 后面(fragment),浏览器不会把它发给服务器,也不会出现在请求日志和 Referer 里;
   - 页面脚本读出 fragment 后立刻用 `history.replaceState` 把它从地址栏去掉,再 `POST /api/login`(JSON)兑换;后端用定长比较核对,**兑换一次即作废**,作废后不自动生成新令牌;
   - **再要登录地址**(第四轮复核应修:原设计 `login-url` 读文件副本,写文件失败时会打印已作废的旧令牌)→ 不再用文件:后端在 `~/.a2a_gui_demo/admin.sock` 上开一个 Unix socket(所在目录权限 0700,socket 0600),`gui_demo/bin/login-url` 连上去请求,后端当场生成新的一次性令牌(同时作废之前未用的)并把地址返回给它打印。这样打印出来的永远是后端内存里当前有效的那个;后端没在运行或 socket 连不上时 `login-url` 报错退出,提示"后端未运行,启动后端会打印新地址",不打印任何令牌;
   - **陈旧 socket 的处理**(第五轮复核应修:后端崩溃或 VM 重启后 socket 文件会留在磁盘上,直接监听会失败):后端启动时,如果 `admin.sock` 路径已存在,先 `lstat` 检查——不是 socket、或不属于当前用户,就报错退出,不删;是本用户的 socket 就试着连接:连得上,说明已有一个后端在运行,报错退出(不删、不抢);连接被拒(`ECONNREFUSED`),才判定为陈旧,删除后再监听。后端正常退出时自己删掉它;
   - **单实例锁,串行化启动**(第六轮复核应修:两个后端同时启动时,都可能判定为陈旧,后一个会删掉前一个刚建好的 socket):后端启动的**第一件事**是监听一个 Linux 抽象命名空间的 Unix socket `\0a2a_gui_demo_<uid>` 作为单实例锁——它不在磁盘上,进程退出(包括被 `kill -9`)时内核自动释放,不会留下陈旧锁;监听失败(`EADDRINUSE`)就说明已有后端,报错退出。只有拿到锁的进程才去做上面"检查、回收、监听 `admin.sock`"和监听 HTTP 端口,所以这几步不会有两个进程同时做。
   - **原子消费**(第三轮复核应修 2):兑换处理在**同一段同步代码里**完成"比较 → 置空",中间没有 `await`;`login-url` 的换新也是同一段同步代码。Node 单线程执行同步代码,所以两个并发兑换请求不可能都通过比较;
   - 兑换成功后后端发一个随机会话 id 作为 cookie(`HttpOnly; SameSite=Strict; Path=/`),会话 id 只存在后端内存里,后端重启即失效;
   - **只有两处不要求会话 cookie**(第三轮复核应修 1):静态登录页及其脚本、样式(只读,不含数据),以及 `POST /api/login`;`/api/login` 自己校验 Origin、`Content-Type: application/json` 和一次性令牌。**其余所有 API 和 WebSocket** 都要求有效会话 cookie,没有或不对一律 401,WebSocket 直接拒绝握手;
   - 所有响应带 `Referrer-Policy: no-referrer`。
3. **校验来源**:修改类请求(POST / DELETE)和 WebSocket 握手都检查 `Origin` 必须等于后端自己的地址,挡住别的网页借浏览器 cookie 发请求;修改类接口只收 JSON(`Content-Type: application/json`)。

其他:
- 所有命令用参数数组调用,不拼 shell 字符串;所有名字先按规则校验;项目路径限制在家目录下。
- WebSocket 只接受后端登记过的 agent 窗口(工程 id + agent 名),不能任意 attach 或运行别的命令。
- 同机的其他进程:能连 `admin.sock` 的只有 VM 上同一个用户,与 herdr socket 的保护等级相同(同一用户本来就能直接操作 herdr)。

## 6. 模型调用

- 新建 agent 会启动真实的 fnx_dv / fnx_sw,**启动本身不调用模型**(20 号文档 §7 已验证)。
- 只有在终端里输入并按回车,或 agent 之间互发消息时,才会调用模型。**我在自动测试里不按回车**;验收时是否在终端里和 agent 对话由用户决定。
- 需要演示 dv → sw 通信时,先征得用户同意再做。

## 7. 实施步骤

| 步 | 内容 | 验证 |
| --- | --- | --- |
| 0 | VM 环境:把 npm 加进 PATH(`~/.local/lib/node-v22.20.0-linux-arm64/bin`);确认 npm 源可达;在 VM 本地目录 `~/gui_demo_run/` 安装依赖(node-pty 编译);确认 Mac 能访问 `ubuntu.orb.local:8765` | 一个最小页面能从 Mac 打开 |
| 1 | 后端:工程的新建、列表、删除(§4.1、§4.2),broker 子进程管理,工程环境函数,令牌 / cookie / Origin 校验 | Node 自带测试运行器(`node --test`)在 VM 上测:新建 → herdr 会话存在、拓扑正确 → 删除 → 会话、tmux、状态目录都清干净;中途失败能回滚;两个工程的注册表、拓扑互不串;无 cookie、错 cookie、错 Origin 的请求和 WebSocket 握手都被拒;登录令牌用过一次后再用被拒;并发 20 个请求兑换同一令牌,恰好 1 个成功;`login-url` 拿到的新地址能登录、之前未用的令牌随之失效;后端未运行时 `login-url` 报错且不输出令牌;已有后端在运行时再启动一个会报错退出、不删正在用的 socket;留一个陈旧 socket(杀掉后端 -9)后再启动能正常恢复;socket 路径被普通文件占用时报错不删;两个后端在有陈旧 socket 的情况下同时启动,恰好一个成功、另一个报错退出,且 `admin.sock` 仍能连上、`login-url` 可用;拿锁的后端被 `kill -9` 后立刻能再启动;无 cookie 只能拿到登录页和调 `/api/login`;`queue` 等无 `--session` 的命令能按环境变量读到本工程的数据 |
| 2 | 页面一 | 用 chrome-devtools 从 Mac 浏览器操作:新建、进入、删除 |
| 3 | 后端:agent 新增、删除、列表、停止、重启(§4.3–§4.5) | `node --test`:新增 dv_uart、sw_uart → 注册表与 herdr 一致、各在自己的 tab;两个都在时只能"删除整个 IP"→ 两个 agent 都清除、IP 从拓扑删除;只有 dv_spi 时"删除"→ 同样走 `ip remove`;队列不空时删除被拒并显示原因 |
| 4 | 终端桥:窗口 tmux + node-pty + WebSocket + xterm.js;页面二 | chrome-devtools:打开 agent 窗口能看到 pi 界面;改浏览器窗口大小后 agent 终端尺寸跟着变;stop → 窗口显示"未运行",restore → 自动接回;两个浏览器标签同时看同一个 agent 不互相挤掉 |
| 5 | 整体走查、清理残留(测试用的会话 / tmux / 目录)、写使用说明(`gui_demo/README.md`:启动、停止、访问地址) | 用户在 Mac 浏览器验收 |

- 代码在 Mac 上编辑,**运行在 VM 本地副本** `~/gui_demo_run/`:每次运行前用 rsync 同步源码(不同步 `node_modules`)。这样避开 OrbStack 共享目录的文件状态滞后问题(之前遇到过 VM 读到旧文件),也避免把 Linux 编译出的 node-pty 放进 Mac 仓库。`node_modules/`、构建产物加进 `.gitignore`。
- 测试用的工程用 `guidemo_test_*` 前缀的会话,测完删除;不动用户和 Codex 的 herdr 会话。
- 审核:本计划先交 Codex 审核;实现完成后整体交 Codex 审核一次,再给用户验收、提交。

## 8. 不做 / 已知限制

- 只做 dv、sw 两类智能体;不做 Spec / 架构 / RTL。
- 不做用户账号登录、多用户、权限(只有 §5 的访问令牌);不做截图里的任务卡片、进度、文件树。
- 后端重启后:工程列表从 `projects.json` 恢复;broker 与窗口 tmux 重新拉起;如果 herdr 会话也没了(VM 重启),界面提示并提供"恢复"按钮(调用 `a2a agent recover`)。这一项放在第 5 步,时间不够就先只提示。
- "项目路径"在 demo 里只作 agent 工作目录的父目录;同事的 GUI 里它可能还有别的含义,讨论时再对齐。
- 20 号文档 §8.5 的"登记已有 pane"命令不做:demo 采用"GUI 调 a2a 创建"的做法。
- 单独删除一类智能体(同 IP 另一类还在时)暂不支持,见 §4.4。
- 拓扑文件在项目路径下(§11):agent 的工作目录就在它下面一层,模型用 bash 能读到、改到 `topology.yaml`(改了会被 a2a 自动重新加载,可能加边、改模板;跨 IP 仍不可能)。放在数据目录时同一用户的 agent 也能读写,两种放法都没有真正的隔离。删除工程后项目路径下留一个空的 `topology.yaml.lock`(故意不删,避免锁分裂)。
- 输入闸门:GUI 自己做的停止 / 重启 / 删除有栅栏同步撤销写权限;但**从外部用 a2a 命令行停止 agent** 时,闸门要等下一次状态查询(默认每 2 秒)才知道,这段时间里浏览器的输入可能进到 pane 的 shell(第 4 步 Codex 审核阻断)。
- 删除 IP 时,GUI 先核对"该 IP 下的 agent 与界面看到的一致",再调用 `a2a ip remove`,两步之间没有跨进程锁:若有人在这个空当里从外部对 GUI 的状态目录执行 `a2a ip add`,新加的 agent 也会被一起删掉。彻底解决需要 a2a 提供在 IP 锁内按预期集合删除的接口(第 3 步 Codex 审核应修 1;用户决定不改 a2a,记为已知限制)。

## 9. Codex 审核记录

### 第一轮(2026-10-13)

- 阻断 1:没规定每个工程的 a2a 环境(`A2A_STATE_DIR`、`A2A_TOPOLOGY`、`HERDR_SESSION`)贯穿所有子进程,只传 `--session` 会让工程共用默认状态与拓扑 → 已改 §3,统一由一个函数构造并校验。
- 阻断 2:只限制网络不足以保护终端输入 → 已改 §5,加访问令牌 + cookie + Origin 校验,第 0 步实测 OrbStack 是否另开监听。
- 应修 3:同 IP 另一角色还在时直接 `agent purge`,在途消息会出问题 → 已改 §4.4,只用有排空的 `ip remove`,单独删一类暂不支持。
- 将来考虑 4:多窗口共享 tmux 时输入与尺寸 → 已在 §3 注明,demo 不做控制权管理。

### 第二轮复核(2026-10-13)

阻断无;上轮 §4.4、§3 的修改确认解决。应修 2 项,已改:
- 应修 1:`queue`、`resolve`、`status` 等命令没有 `--session` 参数,不能"每条命令都带" → §3 改为环境变量统一设置,只对支持的命令传 `--session`;
- 应修 2:长期令牌放在查询参数会留在浏览器历史 / 日志 / Referer → §5 改为 fragment 传一次性令牌、POST 兑换成会话 cookie、`Referrer-Policy: no-referrer`。

### 第三轮复核(2026-10-13)

阻断无。应修 2 项,已改:
- 应修 1:"所有 HTTP 接口都要求 cookie"按字面会挡住登录 → §5 写明只有静态登录页和 `POST /api/login` 例外,后者校验 Origin、JSON 和一次性令牌;
- 应修 2:兑换须原子消费 → §5 写明比较、作废、换新在同一段同步代码里完成;§7 第 1 步加并发兑换测试。

### 第四轮复核(2026-10-13)

阻断无;第三轮两项确认解决。应修 1 项,已改:令牌文件写失败时 `login-url` 会打印已作废的旧令牌 → 去掉令牌文件,`login-url` 改为通过 Unix socket(仅本用户可连)向后端当场要新令牌;后端不在时报错、不打印令牌。§7 第 1 步加相应测试。

### 第五轮复核(2026-10-13)

阻断无;第四轮确认解决。应修 1 项,已改:`admin.sock` 缺陈旧 socket 的启动恢复规则 → §5 写明启动时检查类型与属主、试连接,只有本用户的、连不上的 socket 才删;§7 第 1 步加活跃不误删、陈旧可恢复、普通文件不删三个测试。

### 第六轮复核(2026-10-13)

阻断无;第五轮确认解决。应修 1 项,已改:两个后端同时启动时陈旧 socket 回收有竞态 → §5 加单实例锁(Linux 抽象命名空间 Unix socket,进程退出内核自动释放),拿到锁才做检查、回收、监听;§7 第 1 步加并发启动测试和 kill -9 后可再启动测试。

## 10. 实施记录

### 第 0 步 · 环境(2026-10-13)

- 依赖(版本锁定在 `gui_demo/package.json`):`ws 8.18.0`、`node-pty 1.0.0`;开发依赖 `typescript 5.6.3`、`esbuild 0.24.0`、`@xterm/xterm 5.5.0`、`@xterm/addon-fit 0.10.0`、`@types/node 22.7.5`、`@types/ws 8.5.12`。在 VM 的 `~/gui_demo_run/` 用 `npm install` 安装,24 秒,node-pty 在 Node 22.20.0 上编译通过(`build/Release/pty.node`)。npm 不在默认 PATH,运行时用 `~/.local/lib/node-v22.20.0-linux-arm64/bin`。
- 同步:VM 上没有 rsync,改用 `gui_demo/bin/sync-to-vm.sh`(Mac 上 tar 打包、`orb` 管道到 VM 解包;排除 `node_modules`、构建产物;关掉 macOS 扩展属性)。
- 最小服务(`server/src/main.ts`,只回一行文字并用 node-pty 跑一次 `echo`)监听 `192.168.139.41:8765`:
  - Mac 上 `curl http://192.168.139.41:8765/` 和 Chrome 打开都正常,页面显示"服务可达。node-pty ok";
  - **`ubuntu.orb.local` 在这台 Mac 上不可用**:它被解析成 `198.18.0.81`(代理软件的 fake-ip 网段),请求不通。所以访问地址改用 `http://192.168.139.41:8765`。这个地址是 VM 在 OrbStack 网络里的 DHCP 地址,VM 重启后可能变;后端启动时会打印实际地址;
  - Mac 的 `127.0.0.1:8765` 不可达,`lsof -iTCP:8765 -sTCP:LISTEN` 在 Mac 上没有任何监听:OrbStack **没有**把这个端口转发到 Mac 的 localhost 或局域网(Mac 局域网地址 192.168.0.187 上没有 8765)。§5 第 1 层成立,不用改监听地址。
- codex-test 交叉验证第 0 步时发现两处问题,已在第 1 步修正:
  - 它那边 `sync-to-vm.sh` 卡住("start VM: timed out waiting for VM to start"),而普通 `orb` 命令正常。判断是经标准输入向 `orb` 传 tar 流在非交互环境里挂起 → 改为先在 Mac 的 `/private/tmp` 打包,VM 经 `/mnt/mac` 读文件,读前核对大小;
  - VM 副本里有 `._*`(macOS AppleDouble)文件,是第一次同步(还没关掉扩展属性时)留下的,之后的同步只解包不删除旧文件 → 打包时排除 `._*`、`.DS_Store`,解包前清空目标目录(保留 `node_modules`)。
  - 用户同意第 0 步并入第 1 步一起审核、交叉验证;第 0 步的交叉验证中止。

### 第 1 步 · 工程后端、认证、admin.sock(2026-10-13)

代码(`herdr/gui_demo/`):

| 文件 | 内容 |
| --- | --- |
| `server/src/config.ts` | 配置;监听地址默认取 VM eth0 的 IPv4(DHCP 变了也跟着变),可用环境变量覆盖(测试用) |
| `server/src/a2a.ts` | `projectEnv`(唯一的工程环境构造:删继承的 `A2A_*`/`HERDR_*`,设三个变量)、`a2aArgs`(只给 agent / ip / broker run 加 `--session`)、`checkProject`(会话名、拓扑 project_id、拓扑路径三者一致) |
| `server/py/topology_tool.py` | 用 a2a 的 `TopologyStore` 建拓扑 / 显示 |
| `server/src/herdr.ts` | 宿主 tmux 起 herdr 会话、停删会话 |
| `server/src/broker.ts` | 每工程一个 broker 子进程,意外退出 3 秒后重启;后端退出时一起停 |
| `server/src/projects.ts` | 新建(失败按相反顺序回滚,`GUI_DEMO_FAIL_AT` 仅测试用)、删除、列表、后端重启时恢复;修改操作串行 |
| `server/src/auth.ts` | 一次性令牌(同步兑换)、会话 cookie(后端只存 SHA-256)、Origin 校验 |
| `server/src/admin.ts` | 单实例锁(抽象 Unix socket,名字按数据目录**真实路径**取摘要)、admin.sock 陈旧回收 |
| `server/src/http.ts` | 静态页面与 `/api/login` 免 cookie,其余 401;修改类要 Origin + JSON;WebSocket 握手先查 Origin 与 cookie(终端第 4 步做) |
| `web/src/main.ts` | 只有登录:读 `#token=`、`replaceState` 去掉、POST 兑换 |
| `bin/login-url`、`bin/sync-to-vm.sh` | 见 §5;同步脚本见上 |

与方案的差别:
- 单实例锁名带数据目录真实路径的摘要(方案写的是只按 uid):这样测试可以用各自的临时数据目录并行起后端;同一数据目录换软链接写法也是同一把锁(有测试)。
- 删除工程时 herdr 会话删不掉就报错并保留工程登记,便于重试。

测试(VM,`npm test`,真实 herdr 0.9.3 与 tmux;测试用 `guidemo_test_` 前缀、127.0.0.1 随机端口、临时数据目录):**21 个全部通过**,约 20 秒;跑完无残留的 herdr 会话、tmux、broker、临时目录。
- `test/auth.test.mjs`(13 个):免 cookie 范围;错令牌 / 令牌重用 401;无 / 错 Origin 403、非 JSON 415、`/api/login` 无 Origin 403;WebSocket 无 cookie / 错 cookie 401、错 Origin 403;并发 20 个兑换恰好 1 个成功;`login-url` 新地址可用且旧令牌失效、后端不在时报错不输出令牌、正常退出删 admin.sock;已有后端时再启动报错且不删 socket;`kill -9` 后可再启动;有陈旧 socket 时两个后端同时启动恰好一个成功;路径被普通文件占用不删;别的进程在 admin.sock 上监听时不删;软链接指向同一数据目录被锁挡住;权限 0600 / 0700。
- `test/projects.test.mjs`(8 个):两个工程各自的 herdr 会话、宿主 tmux、broker(核对进程命令行与环境变量)、拓扑互不串;删除后会话 / tmux / 状态目录 / broker 都没了、项目路径保留;后端重启后工程恢复、broker 换新进程;在 topology / herdr / broker / save 四处注入失败都完整回滚;参数校验(相对路径、家目录外、`..`、软链接逃出、空名、超长、重名);工程环境函数与 `queue` 等命令只靠环境变量读到本工程。
- 变异检查(只改 VM 上编译出的副本,不碰 Mac 源码):兑换不作废令牌、不校验 Origin、去掉单实例锁、活的 admin.sock 也删、不清继承的 `A2A_*`、`queue` 也传 `--session`、失败不回滚,7 个变异**都被测试抓到**。第一次跑时"活的 admin.sock 也删"没被抓到(被单实例锁挡在前面),补了"别的进程在 admin.sock 上监听"的测试后抓到。
- 第一次全量跑有 1 个失败,是测试本身的问题(`/proc/<pid>/cmdline` 末尾的 `\0` 产生空元素),已改。变异检查里"失败不回滚"会故意留下 herdr 会话,当时留下 3 个,已清理并让变异脚本跑完自己清。

#### Codex 审核第 0 + 1 步代码(2026-10-13)

阻断 2、应修 4、将来考虑 1。逐条处理:

| # | 问题 | 复现 | 修改与验证 |
| --- | --- | --- | --- |
| 阻断 1 | `sync-to-vm.sh` 的目标名校验放过 `.`、`./`:VM 上会清空家目录(除 node_modules);目标是软链接时会写进链接目标 | 只跑校验逻辑(没跑删除):`.`、`./`、`foo/.` 都放行 | 目标名必须匹配 `^gui_demo_[A-Za-z0-9_-]+$`;VM 端拒绝软链接,核对真实路径是家目录下的一级目录且不是家目录本身。验证:7 个不安全的名字都退出码 2;软链接目标被拒,链接指向的目录里没写进任何文件 |
| 阻断 2 | herdr `session list` 失败被当成"会话不存在":删除工程会跳过停会话直接删状态目录;新建回滚时停不掉会话仍继续删状态目录 | 用注入失败的 herdr 包装(`test/fakes/herdr`)在真实 herdr 上复现:回滚那条确实继续删了;删除那条见下方说明 | `herdr.ts` 查询失败一律抛错,停 / 删后再确认会话已不在;删除工程任何一步确认不了就报错并保留工程与状态目录;回滚某步失败即停止,保留状态目录并报告"回滚未完成"。新增测试 3 个 |
| 应修 1 | `checkPath` 先建目录后核验真实路径:经软链接会先在家目录外建出目录再拒绝 | 复现:`~/…/link/newdir`(link 指向 /tmp 下)在 /tmp 下建出了 newdir | 先找最近的已存在祖先、核验其真实路径,再建目录,建完再核验一次。新增测试 |
| 应修 2 | tmux 查询失败被当成空列表、`kill-session` 不查结果,删除工程可能留下窗口 tmux | 用 tmux 包装注入失败复现(见下方说明) | 区分"没有 tmux 服务"(空)与其他错误(抛错);关掉后再确认已不在。新增测试 |
| 应修 3 | 先拉 broker 再绑 HTTP,端口被占时退出不停 broker;后端被 kill -9 后遗留的 broker 占着锁,新 broker 起不来 | 两个都复现:端口被占后留下 broker 进程;kill -9 后重启,新 broker 不在运行 | 改为先绑 HTTP 再恢复工程,启动失败时按相反顺序回收;启动 broker 前先按命令行与 `A2A_STATE_DIR` 找出本工程遗留的 broker 并停掉。新增测试 2 个 |
| 应修 4 | `queue` 的环境断言太弱 | 是(原断言只看退出码与输出非空) | 在本工程状态目录放带工程 id 的 `dispatch.halted`,断言 `queue` 读到它,另一个工程读到 null |
| 将来考虑 | 不可信网络下应上 HTTPS / Secure cookie | — | 不做,demo 依赖 VM 内网边界(§5) |

说明:第一次复现时,"`session list` 失败"和"tmux 查询失败"两条测试的失败**不能算复现**——测试包装读控制文件时漏掉了没有换行结尾的最后一行,失败根本没注入。修好包装后,用变异检查确认:把这 5 处修复在 VM 编译副本上改回旧行为(会话列表失败当作不存在、tmux 查询失败当作空、回滚失败后继续、先建目录再核验、不接管遗留 broker),对应测试**全部失败**,说明测试能抓到这些问题。

测试:VM 上 28 个全部通过(新增 `test/failclosed.test.mjs` 7 个),跑完无残留。

#### Codex 复核第 0 + 1 步修改(2026-10-13)

上轮 2 阻断 + 4 应修确认解决,无新阻断。应修 1 项、将来考虑 1 项:
- 应修:`reapOrphans` 只按命令行与 `A2A_STATE_DIR` 匹配,会把同工程手动启动的 broker 也当孤儿停掉 → 问题成立(要手动指向 GUI 的私有状态目录才会发生,概率低)。改为:GUI 启动 broker 时注入随机身份 `GUI_DEMO_BROKER_ID`,把 pid、进程启动时间(`/proc/<pid>/stat` 第 22 项)、身份写进 `<工程目录>/broker.json`;只回收三者都对得上的遗留进程;有身份不明的匹配进程就不启动 GUI 的 broker、不碰它,并报告"身份不明的 broker"。新增测试"同工程有手动启动的 broker";变异"身份不明也回收""不核对身份标记"都被抓到。
- 将来考虑:扫描到发信号之间的 PID 复用窗口 → 发信号前再核对一次身份与启动时间,窗口已缩小;没有用 pidfd(Node 没有直接接口)。
- 测试:VM 上 29 个全部通过,无残留。

#### Codex 第二轮复核(2026-10-13)

阻断无、应修无,第 0 + 1 步审核通过。将来考虑:`broker.json` 直接写入,写到一半被强杀会留下不完整记录(后果只是拒绝自动回收、需人工处理)→ 已改为先写临时文件再改名;崩溃窗口测试没有补。改后 VM 上 29 个测试全部通过。

#### Codex 独立交叉验证第 0 + 1 步(2026-10-13)

- 同步脚本:本轮执行 `sync-to-vm.sh gui_demo_xcheck` 两次,均报 `start VM: timed out waiting for VM to start`;新版 `/mnt/mac` 归档读取路径没有通过脚本完成同步。随后用临时 tar 手动同步到此前不存在的 `~/gui_demo_xcheck`;排除 `node_modules`、`server/dist`、`web/dist` 后,Mac/VM 文件清单及 SHA-256 一致,VM 中没有 `._*`。`sync-to-vm.sh .` 与 `sync-to-vm.sh foo` 均退出码 2,目标校验在 VM 写入前拒绝。手动同步验证了载荷与排除规则,但不能替代同步脚本成功记录。
- 构建:Ubuntu VM 使用 Node v22.20.0、npm 10.9.3。`npm install`、`npm run build`、`npx tsc -p server` 成功;依赖实际版本与锁定版本一致,`node-pty/build/Release/pty.node` 存在。npm 报告 2 个依赖漏洞(1 moderate、1 high)。
- 测试:`node --test --test-concurrency=1 test/*.test.mjs` 实际 29/29 通过,0 失败、0 跳过。结束后无 `guidemo_test_`/`guidemo_xc_` 会话或 tmux、无测试 broker、无 `/tmp/gd-*` 和 `~/gd-test-proj-*` 遗留。
- 网络与登录:后端监听 `192.168.139.41:8765`;Mac 直连主页 200、`/api/projects` 401。Mac loopback 与局域网地址 `192.168.0.187` 不可达,Mac 无 8765 监听。`ubuntu.orb.local` 解析为 `198.18.0.81`,curl 得到空响应。Mac 使用 `login-url` 新 token 首次 POST 得到 204 和 cookie,重复 token 得到 401。后端停止后 `login-url` 返回错误且不泄露 token。
- 清理:服务停止后 VM 无 8765 监听;临时数据目录及 `~/gui_demo_xcheck` 已删除。未操作非 `guidemo_xc_`/`guidemo_test_` 会话或 tmux。
- 对照结论:构建、29 项测试、网络隔离和一次性登录结果与本节最新记录一致。新增偏差是新版同步脚本本轮仍遇到 VM 启动超时;这次现象不能证明旧版标准输入 tar 管道问题仍存在。`ubuntu.orb.local` 不可访问与记录一致,本轮具体表现为解析到 fake-IP 后返回空响应。

#### 交叉验证结果对比与跟进(2026-10-13)

| 项目 | 我的记录 | codex-test | 结论 |
| --- | --- | --- | --- |
| 构建、依赖版本、node-pty | 通过 | 通过 | 一致 |
| 测试 | 29 / 29,无残留 | 29 / 29,无残留 | 一致 |
| 网络隔离(VM 地址可达、Mac 回环与局域网不可达、Mac 无监听) | 如 §10 | 相同 | 一致 |
| `ubuntu.orb.local` | 解析到 198.18.0.81,不通 | 相同(curl 空响应) | 一致 |
| 一次性登录、`login-url` | 如 §10 | 相同 | 一致 |
| 不安全的同步目标被拒 | 退出码 2 | 退出码 2 | 一致 |
| 同步脚本 | 我这边正常 | 两次超时 "start VM: timed out waiting for VM to start" | **不一致**,见下 |
| npm audit | 没跑 | 2 项(1 moderate、1 high) | 我漏了,见下 |

跟进:
- **npm audit**:`ws 8.18.0` 有两项服务端漏洞(未初始化内存泄露、碎片帧耗尽内存),第 4 步后端要收 WebSocket,必须升;`esbuild 0.24.0` 的漏洞在它的开发服务器(`--serve`),本项目只打包不受影响,顺手升。改为 `ws 8.21.3`(2026-08-07 发布)、`esbuild 0.28.2`(2026-08-08 发布),`npm audit` 0 项;测试 29 / 29 通过。另外发现仓库里**没有 `package-lock.json`**,间接依赖其实没锁住 → 已生成并放进 `gui_demo/`,以后安装用 `npm ci`。
- **同步脚本在 codex-test 那边超时**:旧版(标准输入传 tar)和新版(经 `/mnt/mac` 读归档)都超时,说明我原先"标准输入管道挂起"的判断不对,根因未明。已请 codex-test 用几条最小的 `orb` 命令定位。
  - 定位结果:5 条最小 `orb` 命令(`sh -lc`、`bash -c`、带位置参数、读 `/mnt/mac/private/tmp`、列 `gui_demo` 目录)在 codex-test 的**沙箱内**批量运行超过 90 秒没有返回;获准**在沙箱外**重跑,每条约 0.1 秒成功。脚本用到的每种调用方式在沙箱外都正常。结论:超时来自 codex-test 的 workspace-write 沙箱对 OrbStack 调用的限制,不是脚本本身的问题(没有拿到沙箱内逐条的报错,所以沙箱里具体卡在哪一步未确认)。处理:脚本不改;codex-test 以后在沙箱外运行同步脚本。

### 第 2 步 · 页面一:工程列表(2026-10-13)

代码:
- 前端 `web/src/`:`main.ts`(登录后按地址栏 `#` 路由:`#/` 页面一、`#/p/<id>` 页面二)、`api.ts`(接口封装,401 时切到"未登录"页)、`dom.ts`(构造 DOM,文字一律 `textContent`,不用 `innerHTML`)、`projects.ts`(页面一)、`project.ts`(页面二占位,第 4 步实现)、`main.css`(仿截图一的深色卡片风格,手机宽度单列)。新增 `web/tsconfig.json`,构建时先对前端做类型检查(esbuild 只去类型不检查)。
- 后端:工程可带"简介"(可选,≤ 200 字);新增 `GET /api/config`(返回家目录,用来给项目路径填默认值)和 `GET /api/projects/<id>`。

页面一:卡片列出工程(头像取名字开头、名称、简介或"还没有简介"、项目路径、运行状态"运行中 / herdr 会话未运行 / broker 未运行"、"进入");最后一张"+ 新建项目"弹出对话框(工程名称、项目路径——按名称自动填 `<家目录>/projects/<名称>`、可改——、简介);创建中显示进度,失败时在对话框里显示原因和失败步骤。卡片右上角"删除"弹出确认框,写明会删除的 agent、herdr 会话、broker、a2a 状态,以及**不删**项目路径。点卡片进入页面二。

验证:
- 测试:VM 上 30 个全部通过(新增 1 个:简介校验、按 id 取工程、`/api/config`),无残留。
- 浏览器(Mac 上 Chrome 访问 `http://192.168.139.41:8765`,后端用临时数据目录和 `guidemo_dev_` 前缀):
  - 带令牌打开后地址栏变成 `#/`,令牌已去掉;刷新后仍是登录状态;另一个没有 cookie 的浏览器上下文打开显示"未登录"页;
  - 新建 `test-activate`(简介 smoke。)和名为 `<img src=x onerror=alert(1)>` 的工程:都显示"运行中";后者名字按原样显示为文字,页面里没有生成任何 `<img>` 元素;
  - 重名、相对路径:对话框里显示"已有同名工程""项目路径必须是绝对路径",重名时没有建出目录;
  - 点卡片进入 `#/p/<id>` 占位页,返回正常;点"删除"只弹确认框、不会同时进入工程;确认删除后卡片消失,VM 上该工程的 herdr 会话、broker、状态目录都没了,项目路径保留;
  - 窗口改到手机宽度时单列显示、无横向滚动;刷新后控制台无错误。
  - 截图与用户给的截图一的布局一致(标题、横排卡片、虚线的"新建项目"卡片)。
- 清理:开发用后端已停,临时数据目录与测试项目路径已删,VM 上无 `guidemo_` 会话、tmux、broker,8765 无监听。

#### Codex 审核第 2 步(2026-10-13)

阻断无;XSS 检查无问题。应修 1 项:会话失效(401)时只清空了 `#app`,挂在 `body` 下的新建 / 删除对话框还开着,压在"未登录"页上面,失败处理又把按钮重新启用。
- 复现:浏览器里打开"新建项目"对话框 → 重启后端(会话只在内存里,随之失效)→ 点"创建":对话框仍开着、显示"创建失败:未登录"、按钮可再点,背后是"未登录"页。
- 修改:`showLoggedOut()` 先关闭并移除所有对话框。用同样步骤复验(确认浏览器加载的是新包):401 后页面上没有对话框,只剩"未登录"页。
- 第一次复验无效:同一地址只换了 `#` 后的部分,页面没有重新加载,跑的还是旧代码;改为先跳到 about:blank 再打开才算数。

#### Codex 独立交叉验证第 2 步(2026-10-13)

- 同步与构建:在沙箱外运行 `sync-to-vm.sh gui_demo_xcheck` 成功。VM 上用 Node v22.20.0/npm 10.9.3 执行 `npm ci`、`npm run build`(含 `tsc -p web`)成功;`npm audit` 发现 0 vulnerabilities。
- 测试:VM 上 `node --test --test-concurrency=1 test/*.test.mjs` 实际 30/30 通过,0 失败、0 跳过。结束时无 `guidemo_test_`/`guidemo_xc_` 会话、tmux、测试 broker、`/tmp/gd-*` 或 `~/gd-test-proj-*`。
- 登录与页面:Chrome 隔离上下文中,带 token 打开后地址成为 `#/`,刷新后仍登录;无 cookie 的独立上下文显示"未登录"。Mac 浏览器中创建 `gd-xc-alpha` 与 `<img src=x onerror=alert(1)>`,两者均为"运行中";恶意名称显示为文本,DOM 没有 `<img>`。重名报"已有同名工程",其目标目录不存在;相对路径报"项目路径必须是绝对路径"。卡片导航到 `#/p/pcaafb958` 占位页并可返回。
- 删除与会话失效:点 alpha 的"删除"仅打开确认框,仍留在列表路由;确认后卡片消失,会话 `guidemo_xc_pcaafb958`、tmux、broker 与该项目状态目录消失,`~/gd-xc-proj/alpha` 保留。新建对话框打开并填写后,同一临时数据目录的后端重启;点击创建收到 401 后只剩"未登录"页,对话框消失,`after-restart` 路径未创建。
- 手机与控制台:Emulated 375×812 手机视口下两个项目卡片同列宽、同一 x 坐标纵向排列,网格为单列 343px;document/body scrollWidth 均为 375,无横向滚动。正常登录页控制台无错误;无 cookie/失效 cookie 页会显示预期 401 网络错误。
- 后端中途出现 SIGTERM,浏览器短暂显示 `ERR_CONNECTION_REFUSED`;我在同一临时 `GUI_DEMO_HOME` 重启后继续并完成剩余检查。清理时通过 GUI 删除了其余测试工程;随后停止后端,删除临时数据目录、`~/gd-xc-proj`、`~/gui_demo_xcheck`。最终 VM 无前缀会话、tmux、broker、8765 监听或测试临时目录;Mac 无 8765 监听。未调用模型,未改 `herdr/gui_demo` 文件。
- 对照:30/30、构建、登录、XSS、路径校验、路由、删除、会话失效和手机布局结果与第 2 步及审核记录一致。本轮没有独立对比用户截图一,不对截图视觉一致性作结论。重新登录清理时,同文档审核记录描述的 hash-only 导航现象也出现:仅在同页改 fragment 未触发 token 兑换;显式 reload 后登录成功。

#### 交叉验证第 2 步结果对比与跟进(2026-10-13)

- 对比:codex-test 测过的各项(30 / 30、构建与 audit、登录、XSS、路径校验、路由、删除、会话失效后对话框关闭、手机布局)与我的记录**一致**。它没有比对用户截图一,视觉是否一致不下结论。
- 跟进 1(真问题,已改):**在同一标签页粘贴新的登录地址没反应**。两边都遇到。原因:只改 `#` 后面的部分,页面不会重新加载;而页面在未登录状态加载时根本没有注册地址栏变化的监听,已登录时的监听也只做换页、不兑换令牌。用户在"未登录"页按提示运行 `login-url` 后把新地址粘进同一标签页,会一直停在"未登录"。修改 `web/src/main.ts`:先注册地址栏变化的监听,页面加载时和 `#` 变化时只要是 `#token=…` 都兑换;另外路由时遇到 401 不再留下未处理的 Promise 错误。浏览器验证:
  - 未登录页 → 同一标签页把 `#` 换成新令牌 → 进入工程列表,地址栏变为 `#/`;
  - 已登录时再粘一个用过的令牌 → 仍在工程列表(会话有效);
  - 未登录时粘用过的令牌、再换到 `#/` → 都停在"未登录"页;
  - 控制台只有预期的网络 401 / 404,没有未处理的错误。
- 跟进 2:codex-test 运行中后端收到一次 SIGTERM,原因未确认。我之前清理开发后端用的 `pkill -f "^node server/dist/main.js"` 会误杀 VM 上**任何** demo 后端(包括 codex-test 的、以后用户验收用的),按时间核对我在它运行期间没有执行过,但这个做法有隐患 → 改为按 pid 文件与进程组只停自己启动的那个。
- 浏览器占用:codex-test 的 chrome-devtools 浏览器结束后没有退出,占着共享的配置目录,我的浏览器工具起不来;用户手动关掉后恢复。
- 清理:开发后端已停,临时目录已删;VM 上无 `guidemo_` 会话、tmux、broker,无 demo 后端进程,8765 无监听。

### 第 3 步 · agent 后端(2026-10-13)

代码:`server/src/agents.ts`(新增)、`server/src/http.ts`(接路由)。接口:

| 接口 | 做什么 |
| --- | --- |
| `GET /api/projects/<id>/agents` | 角色(dv 验证智能体、sw 软件智能体)、拓扑里的 IP 列表、各 agent(`a2a agent list`:生命周期、pane、herdr 实时状态) |
| `POST /api/projects/<id>/agents` `{role, ip}` | `a2a ip add <ip> --roles <role> --cwd <项目路径>/{role}_{ip}`;校验种类只能 dv / sw、IP 名规则、`角色_IP` ≤ 32 字、不能重复;herdr 会话不在时 409 |
| `POST /api/projects/<id>/agents/<agent>/stop`、`…/restore` | `a2a agent stop` / `agent restore` |
| `DELETE /api/projects/<id>/ips/<ip>` `{expect: [...]}` | 删除该 IP 的全部 agent 并从拓扑删除,只用有排空的 `a2a ip remove --force`(§4.4)。`expect` 是界面上看到的该 IP 下的 agent,和实际不一致就 409,防止删掉界面上没显示的 agent;成功后关掉这些 agent 的窗口 tmux(第 4 步才会建) |

修改操作与工程的新建、删除共用一个串行队列。

验证:
- 测试(`test/agents.test.mjs`,5 个,用 a2a 集成测试的假 agent:以 pi 的名字运行、herdr 识别为 agent、不调模型):新增 dv_uart、sw_uart 在不同 tab、同一 workspace,工作目录在项目路径下;参数校验(重复 409;种类、IP 名、超 32 字 400;工程不存在 404);stop 后 herdr 里没有该 agent、restore 后回来;`expect` 不全 409 且两个都还在、缺 `expect` 400、一致时两个都删且 IP 从拓扑删除;只有一类时单独删除;删除工程时还在的 agent 一起清掉。全部 35 个测试通过,无残留。
- 变异检查:不核对 `expect`、不限 32 字、不查重复、删除改用 `agent purge`(不排空)——4 个都被抓到。注意最后一个是因为 IP 没从拓扑删掉才被抓到;"队列里有消息时 `ip remove` 拒绝删除"这条路径 GUI 测试没有覆盖,依赖 a2a 自己的测试。
- 真实 fnx(开发用后端,经接口操作,不往终端提交任何输入,没有模型调用):新增 dv_uart、sw_uart 后 herdr 识别为 pi、idle,分别在 w2:t1、w2:t2,工作目录正确;stop 后 pane w2:p1 保留、herdr 里找不到 agent;restore 在原 pane 里回来、idle;`expect` 不全被拒;删除 IP 后两个都没了、VM 上没有 pi 进程。之后删除工程、停后端、清理,无残留。

#### Codex 审核第 3 步(2026-10-13)

阻断无。应修 3 项、将来考虑 1 项:

| # | 问题 | 判断 | 处理 |
| --- | --- | --- | --- |
| 应修 1 | `expect` 核对与 `ip remove` 之间不共享跨进程锁:外部 CLI 可在两步之间新增 agent,GUI 仍会删掉整个 IP | 真问题,但要有人在毫秒级窗口里对 GUI 的私有状态目录手动执行 `a2a ip add` 才会发生。`ip remove` 自己拿 IP 的文件锁,GUI 不能在外面先拿(会自锁),彻底解决要改 a2a(给 `ip remove` 加"预期 agent 集合"参数,在锁内核对) | **用户决定记为 demo 的已知限制**(2026-10-13),见 §8;讨论时作为"a2a 缺一个原子的按预期删除接口"提出 |
| 应修 2 | 删除 IP 成功后关窗口 tmux 失败,重试时 agent 已没了,`expect` 对不上 409,残留窗口清不掉 | 真问题 | agent 删掉后关窗口失败不再算删除失败,返回 200 + 提示"部分完成";新增"回收残留窗口":注册表里没有对应 agent 的窗口 tmux 在刷新列表时关掉。回收放在串行队列里异步执行(避免和新增 agent 时建窗口抢,把新窗口当残留关掉),列表接口不等它 |
| 应修 3 | GUI 层没测"队列不空拒删" | 是 | 新增测试:冻住 broker(SIGSTOP,进程不退出所以不会被重启),以 sw_q 的身份 `a2a send` 一条发给 dv_q 的消息,删除 IP → 500,agent 和窗口都在;恢复 broker、消息投递后再删 → 成功 |
| 将来考虑 | 全局串行队列让一个工程的长操作(最长 180–240 秒)挡住其他工程 | 是 | demo 不改,多工程并行时可按工程串行 |

验证:`agents.test.mjs` 新增 2 个,共 7 个通过;全部 37 个通过,无残留。变异检查:关窗口失败当删除失败、不回收残留窗口、先关窗口再删,3 个都被抓到。

#### Codex 复核第 3 步修改(2026-10-13)

阻断无、应修无,应修 2、3 确认解决。将来考虑 2 项,都不改:① 外部 CLI 竞态,按用户决定记为已知限制(§8);② 后端退出时不等异步回收,残留窗口留到下次刷新列表再回收,不影响消息与 agent 状态。

#### Codex 独立交叉验证第 3 步(2026-10-13)

- 同步/构建/审计:`sync-to-vm.sh gui_demo_xcheck` 成功;Ubuntu 22.04.5、Node v22.20.0、npm 10.9.3。`npm ci` 成功,`npm run build` 的 server/web TypeScript 检查与 esbuild 均通过;`npm audit` 为 0 vulnerabilities。
- 自动测试:`node --test --test-concurrency=1 test/*.test.mjs` 共 37/37 通过,0 失败、0 跳过。结束时测试创建的资源已清理。
- 真实 fnx 接口(不向终端提交输入,未调用模型):临时工程 `pec42fb04` 下经 API 新增 `dv_uart`、`sw_uart`;API 显示均 running/idle,Herdr 均识别为 `pi`,分别在 `w2:t1`/`w2:t2`、`w2:p1`/`w2:p2`,工作目录分别为 `<项目路径>/dv_uart` 与 `<项目路径>/sw_uart`。stop 后 `dv_uart` lifecycle 为 stopped、pane `w2:p1` 保留,Herdr agent list/get 中找不到该 agent;restore 后回到原 pane、running/idle。只提交 `expect:["dv_uart"]` 删除返回 409 且两者仍在;提交完整列表后删除返回 200,API agents 与 ips 均为空,Herdr agent list 为空且 VM 无 `pi` 进程。重复创建返回 409;`role:rtl`、`ip:"Uart"`、超长 IP 分别返回 400。
- 浏览器:同标签页粘贴登录地址未验证。当前隔离 IAB 不可用,Chrome 浏览器通道报 request-header policy 错误,本机 Chrome UI 权限未授予;没有启动或留下浏览器窗口。
- 清理:删除临时工程后停止本轮后端并移除临时 `GUI_DEMO_HOME`、`~/gd-xc-proj`、`~/gui_demo_xcheck` 和 Mac cookie 临时文件。VM 无 `guidemo_xc_`/`guidemo_test_` session、tmux、broker 或 `pi` 进程,无 8765 监听或 `/tmp/gd-*`;Mac 无 8765 监听。既有 `a2a_codex_verify` 仍 running,未操作其他 session。
- 对照结论:构建、37 项测试、agent 创建/状态/工作目录、stop/restore、参数校验、`expect` 防误删和清理结果与本节第 3 步记录一致。浏览器同标签登录项因工具权限未能独立验证,不作通过判断。

#### 交叉验证第 3 步结果对比(2026-10-13)

| 项目 | 我 | codex-test | 结论 |
| --- | --- | --- | --- |
| 构建、`npm ci`、audit | 通过、0 项 | 通过、0 项 | 一致 |
| 测试 | 37 / 37,无残留 | 37 / 37,无残留 | 一致 |
| 真实 fnx 新增(pi、idle、不同 tab、工作目录) | 符合 | 符合 | 一致 |
| 停止(pane 保留、agent 查不到)/ 重启(原 pane、idle) | 符合 | 符合 | 一致 |
| `expect` 不全 409、写全删除、拓扑与 pi 进程清空 | 符合 | 符合 | 一致 |
| 参数校验(重复 409;种类、大写、超长 400) | 符合 | 符合 | 一致 |
| 同一标签页粘贴新登录地址 | 我在浏览器里验证通过 | **未验证**(它那边浏览器工具不可用) | 只有我一方验证 |

结论:codex-test 能测的项全部一致。"同一标签页粘贴新登录地址"只有我一方的浏览器验证,没有交叉验证;第 4 步页面二做完后,用户验收时可顺带确认。

### 第 4 步 · 终端桥与页面二(2026-10-13)

代码:
- `server/src/windows.ts`(新增):每个 agent 一个窗口 tmux,里面循环 `herdr --session <会话> agent attach <agent>`,退出后提示并 3 秒后重试;循环忽略 Ctrl-C 等信号,窗口里不会落到可用的 shell;herdr 用绝对路径(tmux 服务的 PATH 里不一定有 `~/.local/bin`);会话名、agent 名作为位置参数传入,不拼进脚本。每次确保窗口时都设选项:状态栏关、`window-size latest`(尺寸跟随最近有操作的浏览器窗口)、`destroy-unattached off`。
- `server/src/terminal.ts`(新增):`/ws/term?project=&agent=&cols=&rows=`。握手前已校验 Origin 与会话 cookie;再校验工程存在、agent 已登记,然后确保窗口、用 node-pty 运行 `tmux attach-session -t =<窗口>`。协议:客户端文本帧 `{"t":"in"}` / `{"t":"size"}`,服务端发终端输出。同时最多 32 个终端;有背压(浏览器读得慢时暂停读 pty);连接关闭只结束这个 tmux 客户端,窗口与 agent 不受影响。**输入闸门**:每个有终端打开的工程每 2 秒查一次 `agent list`,只有"运行中且 herdr 识别得到"的 agent 才转发输入——agent 停止后窗口里是 pane 自己的 shell(20 号文档 §7),不能只靠前端只读。
- `agents.ts`:新增 agent 后建窗口(建不成只提示,打开终端时会再建)。`http.ts`:WebSocket 交给终端桥;CSP 加 `style-src 'unsafe-inline'`(xterm.js 会动态插入 `<style>`,脚本仍只允许本站文件)。`main.ts`:退出时关闭所有终端连接。
- 前端:`web/src/term.ts`(xterm.js + fit;断开 3 秒自动重连;只读时显示原因)、`web/src/project.ts`(页面二)、`web/src/modal.ts`(对话框抽出来共用)、`api.ts` 补接口、`main.ts` 路由加 `#/p/<id>/<dv|sw>`、离开页面二时关掉终端连接和定时刷新、`main.css` 页面二样式(窄屏时导航横排、卡片单列)。

页面二:左边导航"验证智能体 / 软件智能体"(带数量);右边是该类下各 IP 的卡片——agent 名、状态(运行中 / 已停止 · 空闲 / 工作中 · pane 号)、"停止 / 重启"、"删除"(同 IP 另一类还在时为"删除整个 IP",确认框列出会一起删的 agent);卡片下是终端。右上"+ 新增 IP"。每 3 秒刷新状态,已有卡片的终端不重建。

实施中发现并修正:
- tmux 3.2a 的 `set-option -t` 不认 `=会话名` 写法(`kill-session`、`attach-session` 认),关状态栏一步其实失败了,浏览器里看到了 tmux 绿色状态栏。改为 `=会话名:`(带冒号仍是精确匹配,前缀不全会报错),并检查返回码,失败就报错;每次打开终端都重设选项,已存在的窗口也会被纠正。补了测试。
- 输入闸门的定时器里 `getProject` 在工程删除后会同步抛错、逃出定时器而让后端崩掉 → 放进 Promise 里。
- 新增 IP 对话框里输入已存在的 agent 时提示文字不对 → 改为"dv_uart 已存在"。

验证:
- 测试(`test/terminal.test.mjs`,6 个,假 agent):窗口选项正确;握手校验(参数 400、agent / 工程不存在 404、未登录 401);输入只到对应 agent、不串到别的;改尺寸后 agent 终端尺寸变为 33×111;两个连接同时打开同一 agent 都不被挤掉;agent 停止后输入被丢弃(窗口里没出现输入的命令)、重启后同一连接自动接回且输入恢复;删除 IP 后窗口关掉、连接关闭。全部 43 个测试通过,无残留。
- 变异检查:去掉输入闸门、忽略改尺寸、不核对 agent 是否登记、`set-option` 目标不加冒号——4 个都被抓到。
- 浏览器 + 真实 fnx(开发用后端;只打字不回车,没有模型调用):
  - 页面二显示真实 pi 界面;在 sw_uart 终端里打字只到 sw_uart、不到 dv_uart,agent 仍空闲;
  - 点"停止":卡片"已停止",终端只读并提示,窗口里是 pane 的 shell(和 20 号文档一致);点"重启":在原 pane 里回来,同一个窗口不用重连就接上,恢复可输入;
  - "+ 新增 IP":非法名字在对话框里提示;新增 dv_spi 后出现新卡片(w2:p3)、导航计数更新;
  - 第二个标签页看同一工程:每个窗口 tmux 有 2 个客户端,都正常;在第二个标签页删除 dv_spi,确认框文字正确,窗口与 agent 都没了,第一个标签页下次刷新也去掉了卡片;"删除整个 IP"的确认框列出 dv_uart、sw_uart;
  - 手机宽度:导航横排、单列、无横向滚动,pi 按新宽度重排;
  - 回到工程列表后,所有窗口 tmux 的客户端数为 0,没有遗留的 `tmux attach` 进程。
- 已知现象:pi 不用全屏模式绘制,终端尺寸变化时会留下一份旧的状态栏残影;重启时窗口里能看到 a2a `agent restore` 在 shell 里输入的启动命令。都是 pi / a2a 本身的行为,不是终端桥的问题。
- 清理:开发用工程已删、后端已停,VM 上无 `guidemo_` 会话、tmux、broker、pi 进程,8765 无监听。

#### Codex 审核第 4 步(2026-10-13)

阻断 1、应修 3、将来考虑 1:

| # | 问题 | 判断与复现 | 处理与验证 |
| --- | --- | --- | --- |
| 阻断 | 输入闸门每 2 秒刷新:`agent stop` 返回后、下一次查询前,旧的"可写"结果仍有效,输入会进到 pane 的 shell 当命令执行;原测试先等 2.6 秒,没覆盖这段 | **真问题,已在真实 herdr 上复现**:去掉栅栏后,停止请求一返回就发的 `echo …` 回车后进到了 shell(两个测试都失败) | 新增 `fence.ts`:GUI 停止 / 重启 / 删除前**同步**撤销写权限;完成后只认序号更大(完成之后才开始)的状态查询,并立刻触发一次查询(查询进行中就查完再查一次)。握手时 agent 正被操作就 409;建窗口后再核对一次 agent 还在不在,不在就关窗口、拒绝。外部 CLI 停止仍有一个查询周期的延迟,记为已知限制(§8) |
| 应修 1 | `MAX_TERMINALS` 在 `await` 前只检查不占名额,并发握手可一起越过上限 | 是 | 握手一开始就占名额,失败 / 断开归还。测试:上限 3、并发 8 个握手 → 恰好 3 个连上、其余 503,关掉后能再连 |
| 应修 2 | `pty.spawn` 失败时(WebSocket 已升级、已登记)没有清理 | 是(代码路径) | 捕获失败:释放闸门与名额,以 1011 关闭连接。测试:注入 pty 失败,上限 2 下连续 5 次握手都以 1011 结束(名额没漏) |
| 应修 3 | 后端退出只发 `ws.close`,`pty.kill` 在 close 事件里,可能来不及就 `process.exit` | **按实测不会残留**:进程退出时 pty 主端关闭,`tmux attach` 收到挂断自行退出;"退出时不杀 pty"的变异下测试仍通过,说明残留不会发生 | 仍改为退出时直接 kill 所有 pty 并 `terminate` 连接(双保险)。测试:SIGTERM 后无 `tmux attach` 进程 |
| 将来考虑 | 64KB 单帧上限会挡住大段粘贴 | 是 | 前端按 8192 字符分块发送,不在代理对中间切开 |

测试中自己的问题:
- 第一次写的"停止后立刻输入"测试抓不到去掉栅栏的变异,原因是只检查"最后一个 shell 提示符之后"——命令一旦被执行,后面会冒出新的提示符,把泄漏跳过了。改为从 pi 退出后的**第一个**提示符开始检查后,两种查询间隔下的变异都被抓到。
- 并发握手很难稳定卡进"核对后、建窗口前"的空档,所以加了仅测试用的 `GUI_DEMO_HANDSHAKE_DELAY_MS`(握手在第一次核对后停一会儿)和 `GUI_DEMO_GATE_POLL_MS`(把查询间隔调成 60 秒,让旧结果不过期),这两个测试是确定性的。

变异检查:不设栅栏(两种查询间隔)、栅栏解除不重查、握手后不复核、名额在 await 之后才占、pty 失败不释放名额——都被抓到;"退出时不杀 pty"没被抓到,原因见应修 3。全部 50 个测试通过,无残留。

#### Codex 复核第 4 步修改(2026-10-13)

阻断 0,上轮阻断与应修确认解决。应修 1、将来考虑 2:
- 应修:握手占名额后异步查 agent、建窗口,期间不管客户端是否已断开;`ws` 的 `handleUpgrade` 遇到已断开的 socket 会直接销毁、**不调回调**,而 `handedOver` 已置 true,名额不归还,反复切页 / 断网会耗尽 32 个名额。→ **真问题,已复现**:新测试(上限 2、握手停顿 1 秒,停顿期间断开 3 次)在旧代码下随后的正常连接返回 503。修改:名额只在 `handleUpgrade` 的回调里交给 bridge,其余一切情况(拒绝、出错、等待中断开、回调没被调用)都由握手代码归还且只归还一次;监听握手 socket 的关闭,每个 `await` 之后检查客户端是否还在、不在就停止后续工作。改后该测试通过。
- 将来考虑 1:外部 CLI 停止 agent 仍靠轮询关闸,查询卡住时延迟可能超过 2 秒 → 已在 §8 披露;主动状态通知不在 demo 范围。
- 将来考虑 2:`MAX_TERMINALS` 等直接 `Number()` 转换,非法配置会让上限失效 → 顺手改了:整数配置(端口、终端上限、查询间隔、握手延时)不合法时启动报错。新增测试:`abc`、`0`、`-1`、`1.5` 都拒绝启动。
- 测试:全部 52 个通过,无残留。

#### Codex 第二轮复核第 4 步(2026-10-13)

阻断 0,上轮名额泄漏与配置校验确认解决。应修 1、将来考虑 1:
- 应修:关服只清理已进入 `terms` 的连接;还在等 `agentRows` / `ensureWindow` 的握手没登记,关服期间(要等 broker 停下)可能完成并启动 `tmux attach`。→ **逻辑上成立,但没复现**:新测试(握手停顿 1.5 秒、停顿中发 SIGTERM、每 100 毫秒检查有无 `tmux attach` 进程)在旧代码下也通过——broker 很快停下,进程在握手继续之前就退出了;即使溜过去,进程退出时 pty 被挂断,`attach` 也会结束(上一轮已实测)。要稳定复现得人为拖慢关服,没有为此再加测试钩子。仍按建议改了:关服一开始就拒绝新握手(503)、销毁所有还在等待的握手 socket,握手每步检查"是否已在关服"。该测试保留作回归检查,并注明旧代码也能通过。
- 将来考虑:外部 CLI 停止的轮询延迟,已在 §8 披露。
- 测试:全部 53 个通过,无残留。

#### Codex 第三轮复核第 4 步(2026-10-13)

阻断 0,关服代码路径确认已处理。应修 1、将来考虑 1:
- 应修:SIGTERM 测试不能确定发信号时握手已在等待,旧代码也能通过,防不住回归 → 成立。加了两个仅测试用的钩子:握手进入等待时打印一行(测试看到后再发信号)、`GUI_DEMO_SHUTDOWN_DELAY_MS`(关服时在退出前停一会儿,让待完成的握手一定在进程还活着时醒来)。新测试:握手停 1 秒、关服停 3 秒,确认握手已在等待后发 SIGTERM,检查关服期间没有升级、没有启动 `tmux attach`。
- **更正上一轮的结论**:上一轮说"逻辑成立但没复现",只是时机没卡准。用新测试做变异(把"关服时拒绝新握手、销毁待完成握手、握手每步检查关服"三处一起还原成旧行为),测试失败,报"关服期间不应启动 tmux 客户端"——即旧代码确实会在关服期间启动 `tmux attach`,上一轮那条应修是真问题。(只还原其中一处时测试仍通过,因为另外两处单独也能挡住。)
- 将来考虑:配置校验只测了终端上限 → 补了端口(0、65536、80a)、查询间隔(199、600001)、握手延时(-5、60001)、关服停顿(x)都拒绝启动,合法边界值(查询间隔 200、握手延时 0、终端上限 1)能启动。
- 测试:全部 54 个通过,无残留。

#### Codex 第四轮复核第 4 步(2026-10-13)

阻断、应修、将来考虑均无,第 4 步审核通过。

#### Codex 独立交叉验证第 4 步(2026-10-13)

- 同步/构建/审计:`sync-to-vm.sh gui_demo_xcheck` 成功;OrbStack Ubuntu 22.04.5、Node v22.20.0、npm 10.9.3。`npm ci`、`npm run build`(server/web TypeScript 与 esbuild)通过,`npm audit` 为 0 vulnerabilities。
- 自动测试:`node --test --test-concurrency=1 test/*.test.mjs` 实际 54/54 通过,0 失败、0 跳过,约 201 秒。会话、tmux、broker、pi、attach、`/tmp/gd-*` 和 `~/gd-test-proj-*` 均无测试残留。
- 真实 fnx/终端桥:临时工程 `p60e056d0` 的窗口为 `guidemo_xc_win_p60e056d0_dv_uart` 和 `guidemo_xc_win_p60e056d0_sw_uart`;两者 `status off`、`window-size latest`。WebSocket 探针经 `node_modules/ws` 连接真实后端:
  - 等到终端首屏输出后输入未提交标记,`herdr agent read sw_uart --source recent-unwrapped` 可见,`dv_uart` 不可见;发送退格清除标记。首次探针在握手刚完成时立即输入,输入被初始可写闸门丢弃;等待首屏后复测通过,未发送回车。
  - 发 resize `111×33` 后,通过 Herdr `pane process-info` 定位到 sw 的 pi 进程,其 `/dev/pts/8` 上 `stty size` 为 `33 111`。
  - 两个 sw WebSocket 同时打开且保持连接,`tmux list-clients` 显示 2 个客户端。
  - dv 终端连接保持打开时,stop API 返回后立即发送 `echo XCLEAK` 和回车;pane shell 提示符之后没有 `XCLEAK`,连接未断。restore 返回后同一连接重新可写,未提交标记出现在 dv 的 Herdr read 中,随后退格清除。
  - 保持三个 WebSocket 连接时删除 IP `uart`(`expect` 为两个 agent),返回 200;两个窗口 tmux 消失,三个连接均关闭。
- 浏览器:未使用浏览器,按用户给定备选路径用 WebSocket 验证终端桥。因此页面二路由/视觉呈现以及离开页面二后由前端关闭客户端这一项未独立验证;删除 IP 后服务端关闭连接已验证。此前浏览器通道不可用,本轮未打开浏览器。
- 清理:删除临时工程并停止本轮后端,移除临时 `GUI_DEMO_HOME`、`~/gd-xc-proj`、`~/gui_demo_xcheck`、cookie 和探针脚本。VM 无 `guidemo_xc_`/`guidemo_test_` session、tmux、broker、pi 或 `tmux attach-session -t =guidemo_` 进程,无 `/tmp/gd-*` / `~/gd-test-proj-*` 或 8765 监听;Mac 也无 8765 监听。`a2a_codex_verify` 仍 running,其他既有 session 未操作;未调用模型。
- 对照结论:窗口选项、终端输入隔离、尺寸同步、多客户端、stop 输入栅栏/restore、删除时关闭窗口和连接的实测结果与本节第 4 步记录一致。浏览器页面二与前端离页清理未验证,不作通过判断。

#### 交叉验证第 4 步结果对比(2026-10-13)

| 项目 | 我 | codex-test | 结论 |
| --- | --- | --- | --- |
| 构建、`npm ci`、audit | 通过、0 项 | 通过、0 项 | 一致 |
| 测试 | 54 / 54,无残留 | 54 / 54(约 201 秒),无残留 | 一致 |
| 窗口选项(状态栏关、`window-size latest`) | 符合 | 符合 | 一致 |
| 输入只到对应 agent | 符合(浏览器) | 符合(ws 探针) | 一致 |
| 改尺寸后 agent 终端跟着变 | 33×111 | 33×111 | 一致 |
| 两个连接同时打开同一 agent | 2 个 tmux 客户端,都在 | 同 | 一致 |
| GUI 停止后立刻输入不进 shell;重启后同一连接恢复可写 | 符合 | 符合 | 一致 |
| 删除 IP 后窗口消失、连接关闭 | 符合 | 符合 | 一致 |
| 页面二外观、路由、离开页面后客户端为 0 | 我在浏览器里验证 | **未验证**(它的浏览器工具不可用) | 只有我一方 |

说明:codex-test 的第一个探针在连接刚建立时就发输入,没送到;它判断为"被初始闸门丢弃"。按代码,握手时闸门已按"运行中且识别得到"给了写权限、且该 agent 从未加过栅栏,不应丢弃;更可能是连接刚建立时 `tmux attach` 客户端还在启动,这时写入的按键被 tmux 当作启动阶段的终端应答吞掉。两种原因都只发生在连接后的极短时间,页面上人工操作碰不到;等首屏输出后复测通过。确切原因没有进一步证实。

### 第 5 步 · 恢复、部署脚本、README、整体走查(2026-10-13)

代码:
- 恢复:`POST /api/projects/<id>/recover`(`projects.ts` 的 `recoverProject`):会话在运行时 409;否则停 broker → 关旧宿主 tmux → 重新起 herdr 会话 → `a2a agent recover`(它要求 broker 已停)→ 起 broker。窗口 tmux 里的 attach 循环自己接上恢复后的 agent。页面一的卡片在"herdr 会话未运行"时显示"恢复"按钮。
- 脚本:`bin/deploy-vm.sh`(Mac 上:同步到 `~/gui_demo_run`、`npm ci`、构建)、`bin/start`(VM 上:后台启动,pid 与日志在数据目录,打印登录地址;已在运行时拒绝)、`bin/stop`(按 pid 停止,等待退出)。
- `gui_demo/README.md`:用途、运行环境、部署 / 启动 / 停止 / 登录、数据目录、使用提示、已知限制、测试、代码结构。

验证:
- 测试:新增 `test/recover.test.mjs`(停掉工程的 herdr 会话 → 列表显示未运行、不能新增 agent → 恢复 → agent 运行且空闲、broker 换了新进程、原来的终端连接没断且能把输入送到新的 agent;会话在运行时恢复返回 409)。全部 55 个通过,无残留。
- 脚本:临时数据目录、8767 端口下:启动成功并打印地址;再次启动被拒;`login-url` 可用;停止后端口释放;再次停止提示"没在运行"。
- 正式部署走查(`deploy-vm.sh` → `bin/start`,默认数据目录、8765;自动化浏览器只点界面,真实 fnx,只打字不回车):新建工程 → 进入页面二 → 两类各新增 uart → 在 sw_uart 终端键入文字(herdr 里只在 sw_uart 看到,agent 空闲)→ 退格删掉 → 停止(只读)/ 重启(恢复)→ 命令行停掉工程的 herdr 会话模拟重启 → 页面一卡片显示"herdr 会话未运行 / 恢复",点恢复后"运行中",页面二 dv_uart 运行且空闲、终端正常 → 删除整个 IP → 删除工程。结束后 `projects.json` 为空,无 `guidemo_` 会话、tmux、broker、pi、`tmux attach` 进程;走查用的项目路径已手动删除。验收实例保持运行,等用户验收。

#### Codex 审核第 5 步(2026-10-13)

阻断 1、应修 2:

| # | 问题 | 判断与复现 | 处理与验证 |
| --- | --- | --- | --- |
| 阻断 | `bin/stop` 只用 `kill -0` 判断 pid 存活就发 SIGTERM;后端异常退出后 pid 文件残留、pid 被复用时会给无关进程发信号;`bin/start` 也会把无关进程当成后端 | 真问题。变异(改回只用 `kill -0`)下,新测试里的无关进程被杀 | 新增 `bin/_pidfile.sh`:pid 文件记"pid 启动时间",认作后端要求启动时间一致**且**命令行含 `server/dist/main.js`;否则当陈旧文件,不发信号、删掉。测试:陈旧 pid 文件指向仍在运行的 `sleep`(启动时间对得上 / 对不上两种),`stop` 不动它,`start` 照常启动,`sleep` 始终活着 |
| 应修 1 | 恢复时 `agent recover` 失败(无 JSON):新会话在、broker 没起,页面因 herdr 在运行而隐藏"恢复",没法重试;强杀留下的孤儿 broker 占着锁会让恢复失败 | 真问题。变异:herdr 在运行就拒绝恢复 → 重试测试失败;恢复前不清孤儿 → 孤儿测试失败 | herdr 没在运行**或** broker 没在运行时都可以恢复(页面同样显示按钮);herdr 已在运行就不重起;恢复前先 `reapOrphans`(只停身份对得上的孤儿,身份不明的报错不碰)。测试:注入一次 recover 失败 → 500,列表 herdr 在、broker 不在 → 再点成功;kill -9 留孤儿 broker + 会话停掉 → 新后端上恢复先停掉孤儿再恢复,broker 换新 |
| 应修 2 | `bin/start` 固定约 20 秒超时后删 pid 文件,但不结束后台进程;工程多时启动可能超过 20 秒,之后无法可靠 stop | 真问题。变异(超时不结束后台进程)下测试失败 | 就绪等待可配置(`GUI_DEMO_START_TIMEOUT`,默认 120 秒);超时先结束进程组(SIGTERM,30 秒后 SIGKILL)并确认退出,再删 pid 文件;结束不了就保留 pid 文件并报错。测试:有工程要拉 broker 时把等待设成 1 秒 → 脚本报"没有就绪",pid 文件、后端进程、broker 都没了 |

另外:测试辅助函数第一次只收了标准输出,而"发现陈旧的 pid 文件"写在标准错误里,导致 1 个测试误报失败,改为两路都收。新增仅测试用的 `GUI_DEMO_FAIL_ONCE`(某步只失败一次)。README 补了 pid 文件格式、就绪超时、broker 没运行时也能恢复。全部 59 个测试通过,无残留。

验收实例重新部署:旧实例的 pid 文件是旧格式(只有 pid),先核对其命令行确是 `gui_demo_run` 的后端再手动 SIGTERM,然后 `deploy-vm.sh` → `bin/start`;新 pid 文件为"pid 启动时间"格式。实例里没有工程。

#### Codex 复核第 5 步修改(2026-10-13)

上轮 1 阻断 + 2 应修确认解决。新增应修 1:`herdrRunning()` 把任何非零退出码都当"会话未运行",查询临时出错时恢复会关掉宿主 tmux、试图重起仍在运行的会话。
- 判断:真问题。herdr 在服务没在运行时会输出 `{"error":{"code":"server_not_running",…}}`,可以和其他失败区分。变异(改回"非零即未运行")下新测试失败。
- 修改:`herdrRunning` 只在明确 `server_not_running` 时返回 false,其他失败抛错;恢复遇到查询失败返回 503"会话状态不明,不做恢复",不关宿主 tmux、不重起会话、不停 broker;工程列表里该工程显示"herdr 状态查询失败";删除工程、新增 agent 遇到查询失败直接报错;只有起会话后等待就绪的循环把查询报错当"还没起来"继续等。
- 测试:用 herdr 包装让 `workspace list` 报错(会话实际在运行)→ 恢复 503,列表 herdr 为 null;恢复正常后宿主 tmux 的创建时间不变、broker 进程不变、herdr 为 true。全部 60 个测试通过,无残留。
- 验收实例用 `bin/stop`(新格式 pid 文件,核对身份后停止)→ `deploy-vm.sh` → `bin/start` 重新部署,里面没有工程。

#### Codex 第二轮复核第 5 步(2026-10-13)

阻断、应修均无。Codex 认为"删除工程先停 broker 再查 herdr,查询失败时删除中止、broker 暂停"作为 fail-closed 取舍对 demo 可接受(暂时影响可用性,卡片会显示"恢复"把 broker 拉回)。将来考虑:herdr 状态为 null 时卡片仍显示"恢复",点了必然 503 → 已改:状态查询失败时按钮改为"刷新状态"(只重新查询)。浏览器验证(8767 开发后端,herdr 包装注入 `workspace list` 失败):卡片显示"herdr 状态查询失败 / 刷新状态",去掉注入后点击变回"运行中"。只改前端,`deploy-vm.sh` 更新验收实例的前端文件(后端每次请求从磁盘读),验收后端未重启,登录地址仍有效。

#### Codex 独立交叉验证第 5 步(2026-10-13)

VM: OrbStack `ubuntu`, Ubuntu 22.04.5, Node v22.20.0, npm 10.9.3。验收实例检查时仍由原 PID 716648 监听 `192.168.139.41:8765`;整个验证没有访问默认数据目录、没有向验收实例建工程,结束后 `curl http://192.168.139.41:8765/` 为 200、监听 PID 仍为 716648。

- 同步与构建:`herdr/gui_demo/bin/sync-to-vm.sh gui_demo_xcheck`;VM `cd ~/gui_demo_xcheck && npm ci && npm run build && npm audit && node --test --test-concurrency=1 test/*.test.mjs`。`npm ci` 成功,构建成功,`npm audit` 为 0 vulnerabilities,测试 60/60 通过、0 失败、0 跳过(252.2 秒)。
- 脚本走查:均使用 `GUI_DEMO_HOME=/tmp/gui-demo-s5-home.dXO2NZ`、`GUI_DEMO_PORT=8768`、`GUI_DEMO_SESSION_PREFIX=guidemo_xc_`。`bin/start` 启动并打印 8768 登录地址;第二次启动以状态码 1 拒绝;`bin/login-url` 返回新地址;`bin/stop` 停止并释放 8768;第二次 stop 提示后端未运行。随后以仍存活的 `sleep` PID 和准确启动时间伪造陈旧 `server.pid`;stop 报告陈旧文件、删掉 pid 文件,该无关进程在 stop 后仍存活,由测试者结束。
- 恢复走查:隔离工程 `p6168ce93` 位于 `~/gd-xc-proj/restore-step5`,会话 `guidemo_xc_p6168ce93`;用真实 fnx 启动 `dv_uart` 后,`herdr agent list` 为 `pi/idle`,工作目录正确。`herdr session stop` 后 `/api/projects` 显示 `herdr:false`;`POST /api/projects/p6168ce93/recover` 返回 200,之后 `herdr:true`、broker 从 PID 726223 换为 726608,`dv_uart` 恢复为 `pi/idle`;重复 recover 返回 409。只启动 agent,没有向终端输入,没有调用模型。
- README 对照:部署/启动/停止/登录、一次性令牌、VM 内网地址、默认数据目录、PID 身份核验、恢复行为、测试命令与当前脚本/接口一致。README 的 `gui_demo_run`、默认目录和 8765 是用户部署默认值;本轮按隔离要求用 `gui_demo_xcheck`、临时目录和 8768,没有运行会覆盖验收目录的 `deploy-vm.sh`。
- 清理:工程删除 API 返回 200 且无 warnings;验证删除会保留项目路径后由测试者删除。临时数据目录、项目路径、`~/gui_demo_xcheck` 均清除;无 `guidemo_xc_`/`guidemo_test_` 会话、tmux、broker、pi、attach 进程,无 `/tmp/gd-*`、`~/gd-test-proj-*`,8768 无监听。已有 `a2a_codex_verify` 会话保持运行;未触碰其他会话。

与本节历史记录一致:60 项全套通过、启动脚本拒绝重复启动、PID 防误杀、停止后可恢复会话/agent/broker均得到独立复核。此前记录的正式浏览器页面二、终端交互完整走查本轮没有重做;本轮恢复检查通过 API 与 CLI 验证,不把未重做的浏览器检查计入本轮证据。

#### 交叉验证第 5 步结果对比(2026-10-13)

| 项目 | 我 | codex-test | 结论 |
| --- | --- | --- | --- |
| 构建、`npm ci`、audit | 通过、0 项 | 通过、0 项 | 一致 |
| 测试 | 60 / 60,无残留 | 60 / 60(约 252 秒),无残留 | 一致 |
| 脚本:启动、重复启动被拒、`login-url`、停止释放端口、重复停止 | 符合 | 符合 | 一致 |
| 陈旧 pid 文件指向仍在运行的无关进程,`stop` 不发信号 | 符合(测试) | 符合(手动) | 一致 |
| 真实 fnx 恢复:herdr 停 → false → 恢复 200、herdr true、broker 换新、agent pi / idle;再恢复 409 | 符合(浏览器走查) | 符合(接口) | 一致 |
| 验收实例不受影响 | — | 8765 仍由原 pid 监听、返回 200 | 一致 |
| 浏览器里的恢复与终端重连 | 我在浏览器里走查过 | 未重做 | 只有我一方 |

结论:能交叉验证的项全部一致。第 5 步完成,demo 交给用户验收。

## 11. 设计变更:拓扑文件放到项目路径下(2026-10-14,待审核)

### 11.1 用户的要求与决定

用户验收时提出:`topology.yaml` 应该在工程的项目路径下(比如 `~/projects/a2a_test`),而不是藏在 demo 的数据目录 `~/.a2a_gui_demo/projects/<id>/state/` 里。用户的决定(2026-10-14):

| 问题 | 决定 |
| --- | --- |
| 挪哪些 | **只挪 `topology.yaml`**;注册表、消息队列、审计、锁等运行状态仍留在 `~/.a2a_gui_demo/projects/<id>/state/` |
| 放在哪 | **项目根目录**:`<项目路径>/topology.yaml`(a2a 会在旁边建 `topology.yaml.lock`、`topology.yaml.bak`) |
| 新建时路径下已有 `topology.yaml` | **报错**,不覆盖、不沿用 |

我补充的一条(已告知用户,可推翻):**删除工程时一并删掉项目路径下的 `topology.yaml` 和 `topology.yaml.bak`**,agent 的工作目录仍保留;**`topology.yaml.lock` 保留不删**(原因见 §11.2,避免锁分裂)。理由:否则删工程后在同一路径新建永远报错;这份配置属于被删的工程,它在数据目录里的运行状态也一起删了。

### 11.2 改法

a2a 本来就把拓扑和状态分开:`A2A_TOPOLOGY` 与 `A2A_STATE_DIR` 是两个独立的环境变量。所以**只改 GUI,a2a 不动**。

- **工程记录**:`projects.json` 每条增加 `topology` 字段(拓扑文件的绝对路径)。新建的工程是 `<项目路径>/topology.yaml`。**旧记录没有这个字段时,仍用原来的 `state/topology.yaml`**(向后兼容,验收实例里已有的工程不用迁移)。
- **工程环境** `projectEnv`:`A2A_TOPOLOGY` 取工程记录里的拓扑路径;`checkProject` 核对拓扑文件路径与记录一致、`project_id` 等于工程 id。`ProjectRef` 增加拓扑路径。
- **新建**:校验项目路径之后、建任何东西之前,检查 `<项目路径>/topology.yaml` **和** `topology.yaml.bak` 是否存在(用 `lstat`,软链接也算存在),任一存在就报错 409:"项目路径下已有 topology.yaml / topology.yaml.bak(可能属于别的工程或删除时没清掉),请删掉或换一个路径",不碰它们。这同时挡住两个工程用同一个项目路径。写拓扑用 a2a 的 `TopologyStore.create`(它本身也拒绝覆盖已存在的文件)。
- **`topology.yaml.lock` 的处理(第一轮审核应修 1、2)**:GUI **从不删除** `.lock`。它只是 a2a 加 `flock` 用的空文件,内容无意义:创建前已经存在、且 `lstat` 确认是**普通文件**时直接沿用(是软链接、目录等就报错 409——a2a 用 `open("a+b")` 加锁会跟随软链接,可能锁到别的文件;第二轮审核将来考虑)(不算"别人的文件"被我们接管——我们既不改它也不删它);删除工程后留下一个空的 `.lock` 无害,之后在同一路径新建也不受影响。不删它是为了避免"有进程正持有或等待旧 inode 上的锁,文件被删后新来的进程在新 inode 上加锁"造成的锁分裂。
- **删除拓扑文件的方法(回滚与删除工程共用)**:由一个 Python 辅助命令在 a2a 的**同一把锁**(`exclusive_lock(<项目路径>/topology.yaml.lock)`,与 `TopologyStore.update` / `create` 所用相同)下进行:核对 `topology.yaml` 是普通文件、能解析、`project_id` 等于本工程 id → 删除 `topology.yaml`;`.bak` 能解析且 `project_id` 相同才删,否则留下、记一条**警告**(非致命,见下面删除工程第 3 步);`.lock` 不删。核对不通过就什么都不删、返回失败。持锁期间,外部 `a2a topology …` 写入会在锁上等待;删除完成、放锁后,它读文件时发现文件不在,报"无法读取拓扑文件"而**不写任何东西**(`topology.py` 的 `update()` 在锁内从磁盘重读,读不到即抛错),不会把拓扑重新写出来。
- **新建失败回滚**:用上面的方法删除本次建的 `topology.yaml`(创建前已核对不存在,`project_id` 是本次随机生成的新 id);核对不通过就保留并在错误里报告"回滚未完成"。
- **删除工程的顺序(第一轮审核应修 2)**:
  1. **先做只读的归属检查,不做任何改动**:`topology.yaml` 存在、是普通文件、能解析、`project_id` 等于本工程 id;`topology.yaml.bak` 若存在,也必须是普通文件、能解析、`project_id` 相同。任一不满足就返回 409("项目路径下的 topology.yaml / .bak 不属于这个工程或读不了,工程未删除"),broker、会话、状态目录、工程记录都不动。
  2. 停 broker、purge agent、关窗口 tmux、停删 herdr 会话(原有逻辑;任何一步确认不了就报错,状态目录与工程记录保留)。
  3. 按上面的方法在锁内**再核对一次并删除拓扑文件**:
     - `topology.yaml` 核对不通过(第 1 步之后被人替换)→ 什么都不删、报错,**状态目录与工程记录保留**(会话已停,卡片显示"恢复",可把 agent 拉回来,或人工处理拓扑文件后再删);
     - `topology.yaml` 已删、但 `.bak` 此时核对不通过(第 1 步之后才被换掉)→ **非致命**:保留 `.bak`,记一条警告,继续第 4 步把删除做完(避免主拓扑已删、状态和记录却留着的半删除);警告随删除结果返回、界面上提示。
  4. 最后删状态目录、去掉工程记录。
  删除确认框的文字改为"会删除项目路径下的 topology.yaml 及其备份,保留各 agent 的工作目录"。
- **恢复、后端重启**:不变(拓扑路径从工程记录里来)。

### 11.3 影响与风险

- **agent 看得到也改得了拓扑**:agent 的工作目录就在项目路径下(`<项目路径>/<role>_<ip>`),模型用 bash 很容易读到、改到父目录里的 `topology.yaml`。改了会被 a2a 自动重新加载。能做的事:改模板、加 / 删边(比如给自己加一条发往别的角色的边);**做不到跨 IP**(目标 IP 永远等于发送方自己的 IP)。原来放在 `~/.a2a_gui_demo` 下时,同一 Unix 用户的 agent 其实也能读写,只是不显眼;两者都没有真正的隔离。在 §8 已知限制里写明。
- **项目根目录多出 `topology.yaml.lock`、`topology.yaml.bak`** 两个文件。
- **两个工程不能再共用同一个项目路径**(之前允许,但会共用 agent 工作目录的父目录,本来也不合理)。
- **删除工程后项目路径下会留下一个空的 `topology.yaml.lock`**(见上,故意不删)。
- **版本库**:项目路径若是 git 仓库,`topology.yaml`、`.bak`、`.lock` 会出现在未跟踪文件里。README 提示:想纳入版本管理就提交 `topology.yaml`,`.bak`、`.lock` 加进 `.gitignore`(第一轮审核将来考虑)。
- 人工编辑写错格式会让拓扑加载失败;a2a 的行为不变(用户已在验收步骤 §8 看到提示)。

### 11.4 测试

- 新建后拓扑在 `<项目路径>/topology.yaml`,内容正确;`a2a topology show`(带工程环境)读到的是它;数据目录里没有 `state/topology.yaml`。
- 路径下已有 `topology.yaml`(普通文件 / 软链接)或 `topology.yaml.bak` → 新建 409,原文件内容不变,没有建出会话 / 状态目录 / 登记。
- 路径下预先只有 `topology.yaml.lock`(普通文件,放一段内容作标记)→ 新建成功;删除工程后 `.lock` 仍在、内容不变。
- 路径下的 `topology.yaml.lock` 是软链接或目录 → 新建 409,软链接指向的文件不被改动。
- 两个工程用同一个项目路径 → 第二个 409。
- 在 topology / herdr / broker / save 各步注入失败 → 回滚后 `topology.yaml` 与本次产生的 `.bak` 都不在,**`.lock` 仍在**;若创建前就预存了 `.lock`(放一段内容作标记),其内容不变。
- 删除工程 → `topology.yaml`、`.bak` 删除,**`.lock` 保留且内容不变**,agent 工作目录保留;之后同一路径可以再新建(沿用这个 `.lock`)。
- 删除前拓扑文件的 `project_id` 不是本工程 / 文件读不了 → 409,broker、会话、状态目录、工程记录、拓扑文件全都不动。
- 删除过程中(归属检查之后、删拓扑之前)拓扑文件被替换成别的 `project_id` → 报错,状态目录与工程记录保留,替换后的文件不被删(用仅测试用的停顿钩子稳定复现)。
- 删除与外部拓扑写入并发:外部进程持有 `topology.yaml.lock` 时删除会等待;删除完成后外部的 `a2a topology add-edge` 报"无法读取拓扑文件",项目路径下没有被重新写出 `topology.yaml`。
- 删除前 `.bak` 的 `project_id` 不是本工程 / 读不了 → 409,什么都不动(与主拓扑不符同样处理)。
- 删除过程中(归属检查之后)`.bak` 被换成别的 `project_id` → 删除照常完成(主拓扑、状态目录、工程记录都删掉),`.bak` 保留,返回结果里有警告(用测试停顿钩子稳定复现)。
- 旧格式的工程记录(没有 `topology` 字段)在后端重启后仍能列出、恢复、新增 agent、删除。
- 动态配置:用命令行改模板 / 加边后,`a2a edges` 立刻看到(与 §10、验收步骤 §8 一致)。
- 改动后全部测试、变异检查、浏览器走查、codex-test 交叉验证,按既定流程。

### 11.5 文档

- 更新 `gui_demo/README.md`(数据目录与项目路径的说明)、`gui_demo/验收步骤.md` §0、§8(拓扑路径)、本文 §8 已知限制。

### 11.6 Codex 审核 §11(2026-10-14)

阻断无,应修 2、将来考虑 1,已改进 §11.2–§11.4:
- 应修 1:只检查 `topology.yaml` 不足以证明 `.lock`、`.bak` 属于本工程,删除 / 回滚可能误删预先存在的 sidecar → 新建时 `topology.yaml.bak` 存在也报错;`.lock` 改为 GUI 从不删除(它是空的 flock 文件,沿用无害)。补测预存 `.lock`、`.bak`。
- 应修 2:先删状态目录再核对 `project_id` 会造成半删除;核对到删除之间外部写入不受 GUI 串行队列约束;删 `.lock` 会造成锁分裂 → 删除工程第一步做只读归属检查(失败什么都不动);删拓扑放在删状态目录之前,在 a2a 同一把锁内再核对并删除,失败保留状态目录与工程记录;`.lock` 不删。补测核对失败、过程中被替换、与外部写入并发。
- 将来考虑:`topology.yaml` 可能进入项目版本库 → README 提示提交 `topology.yaml`、忽略 `.bak` / `.lock`。

### 11.7 Codex 第二轮复核 §11(2026-10-14)

阻断无;上一轮问题(锁分裂、删除后外部写入重建拓扑)确认解决。应修 2、将来考虑 1,已改:
- 应修 1:回滚测试仍写 `.lock` 不存在,与"GUI 从不删 `.lock`"矛盾 → 改为断言主拓扑与本次 `.bak` 清掉、`.lock` 保留、预存 `.lock` 内容不变。
- 应修 2:`.bak` 不符时是警告还是失败没定义 → 删除前的只读检查同时核对 `.bak`(不符 409、什么都不动);若检查之后才被换掉,则作为非致命警告:保留 `.bak`,删除照常完成。测试分别断言。
- 将来考虑:预存 `.lock` 用 `lstat` 确认是普通文件再沿用(a2a 加锁会跟随软链接)→ 已采纳:不是普通文件就 409,补测试。

### 11.8 Codex 第三轮复核 §11(2026-10-14)

阻断无;上一轮两项应修确认解决。应修 1:§11.1 与 §11.4 正常删除的测试仍写"删除 `.lock`",与"GUI 从不删 `.lock`"冲突 → 两处都改为 `.lock` 保留、内容不变。设计至此定稿,开始实现。

### 11.9 实施记录(2026-10-14)

代码(`herdr/gui_demo/`):
- `server/src/a2a.ts`:`ProjectRef` 加 `topology`;`topologyOf(p)`(记录里有就用,没有就回退到旧位置 `state/topology.yaml`);`projectEnv` 的 `A2A_TOPOLOGY` 与 `checkProject` 的路径核对都用它。
- `server/py/topology_tool.py`:新增 `check <project_id>`(只读:`topology.yaml` 与存在时的 `.bak` 都是普通文件、能解析、`project_id` 相符)与 `remove <project_id>`(在 a2a 的 `exclusive_lock(topology.yaml.lock)` 内再核对并删 `topology.yaml`;`.bak` 相符才删、不符记警告;`.lock` 不删;锁文件存在但不是普通文件就拒绝加锁)。主拓扑不符退出码 3、什么都不删。
- `server/src/projects.ts`:`checkTopologySlot`(`topology.yaml`、`.bak` 已存在 409;`.lock` 存在须是普通文件);工程记录写 `topology`;回滚加"删本次建的拓扑";删除按 §11.2 四步(先 `check`,失败 409 什么都不动;停会话后 `remove`,失败保留状态与记录;`.bak` 警告随结果返回;最后删状态目录与记录)。仅测试用的 `GUI_DEMO_DELETE_PAUSE_MS`(删拓扑前停顿并打印一行,作测试屏障)。
- 前端:删除确认框写明删除 `topology.yaml` 与 `.bak`、保留 `.lock` 和 agent 工作目录;页面二标题下显示拓扑路径(旧工程显示"在数据目录的 state/topology.yaml")。
- 文档:`README.md`、`验收步骤.md` §5、§8(拓扑路径、取路径的命令兼容新旧工程、git 提示)、§9;本文 §8 已知限制。

验证:
- 测试:新增 `test/topology_location.test.mjs`(11 个):已有 `topology.yaml`(普通文件 / 软链接)或 `.bak` 新建 409 且原文件不变、不建会话与状态;两个工程同路径第二个 409;`.lock` 是软链接 / 目录 409 且目标不变;预存普通 `.lock` 新建成功、删除后 `topology.yaml` 与 `.bak` 删除而 `.lock` 内容不变、同一路径可再建;herdr / broker / save 三处注入失败回滚后主拓扑与 `.bak` 不在、预存 `.lock` 不变;删除前主拓扑或 `.bak` 不属于本工程 → 409 且 broker、会话、状态目录、记录、文件都不动;删除过程中主拓扑被替换 → 409、状态与记录保留、替换后的文件不删,`.bak` 被替换 → 删除完成、警告、`.bak` 保留;外部进程持锁时删除等待 ≥1.5 秒、删除后外部 `add-edge` 报读不到文件且没重新写出;旧格式记录(无 `topology` 字段、拓扑在状态目录)重启后能列出、broker 拉起、读 agent、删除。原有测试里写死 `state/topology.yaml` 的 3 处改为用工程记录里的路径(另 1 个失败是被它们连带的)。**全部 71 个通过**,无残留。
- 测试自己的问题:两处断言用 `registry.json` 判断状态目录是否保留,但没建 agent 时它不存在,改为 `audit.jsonl`;旧格式测试里 `rename` 跨文件系统(家目录 / `/tmp`)失败,改为复制后删除。另外一次后台测试被我自己的清理命令打断(`pkill -f` 的模式匹配到了清理命令自身),已改用 `[b]roker` 这类不匹配自身的写法。
- 变异检查(8 个,都被抓到):不查已有 `topology.yaml` / `.bak`、不查 `.lock` 是否普通文件、删除时连 `.lock` 一起删、删除前不做归属检查、锁内不再核对 `project_id`、删拓扑不加锁、`.bak` 不符当致命、旧记录不回退到状态目录。
- 浏览器(8767 开发后端,不建 agent,不调模型):新建后拓扑在 `<项目路径>/topology.yaml`、状态目录里没有;同一路径再建,对话框显示"项目路径下已有 topology.yaml…";页面二标题显示拓扑路径;按 `验收步骤.md` §8 的命令(含兼容新旧工程的取路径命令)改模板、加边成功并生成 `.bak`;界面删除,确认框文字正确,删后项目路径下只剩空的 `topology.yaml.lock`,记录与会话清空。
- 验收实例(8765)仍运行旧代码,未动;实现审核、交叉验证通过后再部署(后端要重启,登录地址会变;用户已建的 `a2a_test` 是旧格式,照常可用)。

### 11.10 Codex 审核实现(2026-10-14)

阻断无。应修 2、将来考虑 1,都已改:

| # | 问题 | 判断与复现 | 处理与验证 |
| --- | --- | --- | --- |
| 应修 1 | 回滚只在 `init` 成功后才登记"删本次建的拓扑";`TopologyStore.create` 先原子替换文件、再 fsync 目录,fsync 失败时文件已写入却报错 → 状态目录被清、项目根留下 `topology.yaml`,同一路径再也建不了 | 真问题(对照 `_fsutil.atomic_write_text` 的顺序)。用新增的注入钩子(`topology_tool.py init` 在写文件后失败)复现:旧顺序下回滚后 `topology.yaml` 仍在 | 先登记回滚再 `init`;删除命令加 `--missing-ok`(文件不存在也算成功,供回滚用),仍在锁内核对 `project_id` 是本次的新 id 才删。测试:注入"已写入后失败"→ 500、`topology.yaml` 不在、状态目录与记录不在、同一路径可重建 |
| 应修 2 | 删 `.bak` 时 `unlink` 抛 `OSError` 没捕获:主拓扑已删、命令却失败,GUI 保留记录与状态 → 不可恢复的半删除 | 真问题 | `.bak` 删不掉也转为警告、删除照常完成。测试:注入 `.bak` 删除失败 → 200、警告、主拓扑 / 状态目录 / 记录都删、`.bak` 保留 |
| 将来考虑 | TS 的 `lexists` 与 Python 的 `os.path.lexists` 把任何检查错误都当"不存在" | 是 | 两边都改为只有 ENOENT 才算不存在,其他错误报错(fail-closed) |

变异检查:"先 init 后登记回滚"、"`.bak` 删不掉不转警告"都被抓到。过程中我的变异脚本出过两次错,当时的"没抓到"不算数:① 挪代码的正则只截到 `if (init.code !== 0)`、没带下一行 `throw`,变异后语义全乱;② 按测试名过滤用了工程名"写后失败",测试标题里没有这几个字,测试根本没跑。修正后才得到上面的结果。全部 73 个测试通过,无残留。

### 11.11 Codex 复核实现修改(2026-10-14)

阻断无;上轮"先登记回滚"、"只认 ENOENT"确认解决。应修 1:删掉 `topology.yaml` 之后检查 `.bak` 是否存在的那一步在 `try` 外,遇到权限 / I/O 错误时命令失败 → 主拓扑已删、GUI 保留记录与状态,再删又会因预检失败而做不下去。→ 真问题。把这一步挪进警告处理范围:删掉主拓扑之后,对 `.bak` 的任何检查或删除出错都只记警告、删除照常完成。新增注入 `bak_lstat` 的测试(主拓扑、状态目录、记录都删,`.bak` 保留,有警告);变异"检查放回 try 外"被抓到。全部 74 个测试通过,无残留。

### 11.12 Codex 第二轮复核实现修改(2026-10-14)

阻断无;上轮 `.bak` 存在检查在 `try` 外的问题确认解决。应修 1:`.bak` 不是合法 UTF-8 时 `_owner()` 的 `read_text` 抛 `UnicodeDecodeError`,既没转成 `NotOurs`,也不在 `NotOurs / OSError` 的捕获范围里;若在删除前检查之后被换成这类内容,主拓扑已删、命令仍失败 → 半删除。→ 真问题。修改:`_owner()` 把 `UnicodeDecodeError` 也转成 `NotOurs`(主拓扑、`.bak` 预检遇到都是 409、什么都不动);删掉主拓扑之后对 `.bak` 的收尾改为捕获所有异常、一律记警告。新增测试:删除前 `.bak` 非法 UTF-8 → 409、主拓扑与记录都在;删除过程中才被换成非法 UTF-8 → 200、警告、主拓扑 / 状态 / 记录删除、`.bak` 保留。变异"解码错误不处理"被抓到。
- 全量测试:75 个。第一次全量有 2 个终端测试失败(`terminal.test.mjs` 停止 dv_uart 的接口在 227 毫秒内返回 500,后一个测试被连带);单独重跑终端测试 8 / 8 通过,再跑一次全量 75 / 75 通过。**偶发、原因未明**:当时断言没带出接口的错误信息;已把 stop / restore 的断言改为失败时打印接口返回内容,再出现就能直接看到原因。从第 4 步以来的全量测试里这是第一次出现,终端测试不经过拓扑的新建 / 删除路径,判断与本次改动关系不大,但没有证实。列为待观察项,交叉验证时请 codex-test 留意。
- Codex 第三轮复核:阻断、应修、将来考虑均无,§11 实现审核通过。

### 11.13 Codex 独立交叉验证(2026-10-14)

环境:OrbStack VM `ubuntu`,Ubuntu 22.04.5,Node v22.20.0/npm 10.9.3。开始前验收实例 `192.168.139.41:8765` 由 PID 716648 监听且返回 200;`~/gui_demo_xcheck`、`~/gd-xc-proj`、8768 均空闲。另有已有会话 `guidemo_pec3f31e1` 正在运行,本轮未触碰。

- 同步/安装/构建/审计/测试:`herdr/gui_demo/bin/sync-to-vm.sh gui_demo_xcheck`;VM 在 `~/gui_demo_xcheck` 执行 `npm ci && npm run build && npm audit && node --test --test-concurrency=1 test/*.test.mjs`。依赖安装、构建通过,audit 为 0 vulnerabilities;**75/75 通过、0 失败、0 跳过**,耗时 310.1 秒。`terminal.test.mjs` 的停止 dv_uart 用例通过,本轮没有复现此前偶发 500。
- 隔离手动验证使用 `GUI_DEMO_HOME=/tmp/gui-demo-topology-home.NgeB56`、`GUI_DEMO_PORT=8768`、`GUI_DEMO_SESSION_PREFIX=guidemo_xc_`。创建 `~/gd-xc-proj/a` 得到工程 `p25469347`;拓扑位于 `a/topology.yaml`,其中 `project_id` 匹配;工程记录的 `topology` 字段指向该绝对路径,`projects/<id>/state/topology.yaml` 不存在。预置的普通 `.lock` 内容 `LOCK_MARKER_TOPOLOGY_XC` 在创建后不变。
- 路径占用:同一路径再次创建返回 409;另一路径预存 `topology.yaml.bak` 后创建返回 409,其 `BAK_PROTECTED` 内容不变。
- 动态拓扑:只启动真实 `dv_uart`、`sw_uart`(两者均 idle),没有发送消息。按验收步骤 §8 执行 `set-template`、`add-edge sw_question`、`remove-edge sw_question`;`a2a edges` 立即反映模板和边变化,边只出现在 sw_uart 的可用边中,目标为 dv_uart;删除后边消失。生成了 `topology.yaml.bak`,`audit.jsonl` 有对应 `TOPOLOGY_CHANGED` 记录。
- 删除与重建:删除工程返回 200,`topology.yaml` 与 `.bak` 删除,`.lock` 保留且标记内容不变,状态目录、记录、会话删除,项目路径保留;之后同一路径创建成功并得到新工程 `p589fe87e`。
- 删除归属保护:把新工程的拓扑 `project_id` 改为 `p00000000` 后,DELETE 返回 409,错误指出归属不符;删除前后文件 SHA-256 相同,状态目录、工程记录、Herdr 会话及运行中的 broker 均保留。把 `project_id` 恢复为正确值后 DELETE 返回 200。
- 探针说明:首次校验误从 `GET /api/projects/<id>` 单项元数据响应读取 `herdr`/broker 状态,该接口不返回运行状态;改用 `GET /api/projects` 列表核实后通过。这是探针字段选择错误,不是服务端失败。
- 清理:本次工程、临时数据目录、`~/gd-xc-proj`、`~/gui_demo_xcheck` 已清除;无本轮 `guidemo_xc_`/`guidemo_test_` 会话、tmux、broker,8768 无监听。VM 上仍可见既有 `guidemo_pec3f31e1` 会话及 `guidemo_*pec3f31e1*` tmux,均非本轮创建、未操作。验收实例结束后仍为 PID 716648,`curl http://192.168.139.41:8765/` 返回 200。未调用模型。

与 §11.9–§11.12 的设计和实施记录一致:新拓扑在项目根目录、冲突路径 fail-closed、普通 `.lock` 沿用且永不删除、模板与边动态更新、删除检查归属并只清理所属拓扑、错 `project_id` 时保留工程全部运行状态。全量测试通过,本轮未发现实现问题;此前记录的停止接口偶发 500 未复现。

### 11.14 交叉验证结果对比(2026-10-14)

| 项目 | 我 | codex-test | 结论 |
| --- | --- | --- | --- |
| 构建、`npm ci`、audit | 通过、0 项 | 通过、0 项 | 一致 |
| 全量测试 | 75 / 75(此前一次偶发 2 个终端测试失败,重跑通过) | 75 / 75(约 310 秒),终端测试的 stop 用例通过,未复现偶发 500 | 一致;偶发项仍列待观察 |
| 拓扑落在项目根目录、`project_id` 正确、状态目录里没有、记录里有 `topology` 字段 | 符合 | 符合 | 一致 |
| 同路径重建 409;预存 `.bak` 409 且内容保留;预存普通 `.lock` 能建且内容不变 | 符合(测试) | 符合(手动) | 一致 |
| 按验收步骤 §8 改模板、加 / 删边,`a2a edges` 立即反映,生成 `.bak` 与审计 | 符合 | 符合(真实 dv_uart / sw_uart,未输入) | 一致 |
| 删除后 `topology.yaml` / `.bak` 删除、`.lock` 保留、状态 / 记录 / 会话删除、同路径可重建 | 符合 | 符合 | 一致 |
| 删除前 `project_id` 不符 → 409,文件(SHA-256 不变)、状态、记录、会话、broker 都保留;改回可删 | 符合(测试) | 符合(手动) | 一致 |
| 验收实例不受影响 | — | 原 pid 仍监听 8765、返回 200 | 一致 |

结论:全部一致。部署到验收实例。

## 12. a2a 插件改为在线安装(2026-10-14)

### 12.1 背景

验收时发现 VM 上的 fnx_dv、fnx_sw 没装 a2a 插件:自动测试按"不改用户环境"的规矩,开始时用 `a2a plugin install`(软链接)临时装上、结束时卸载;GUI 建 agent 也不装插件。没有插件时 fnx 没有 `a2a_send` 工具,用户输入 `/a2a dv_done`(fnx 里没有这个斜杠命令,pi 把它当普通提示词交给模型)后,模型自己翻 VM 上的文件、用 bash 调 `python3 -m a2a.cli send` 发了出去(pane 环境变量里有 agent 身份,Router 照样放行),约 45 秒、多次模型调用;sw_spi 的模型还用到了我测试时留下的旧代码快照 `~/a2a_snap`(已按用户同意删除)。

用户要求:正式部署按 pi 生态的方式**在线安装**插件;仓库是公开的 `github.com/HwJhx/A2A`,版本标签 `a2a-plugin-v0.1.0`。

### 12.2 改动

- 仓库根目录新增 `package.json`(pi 包清单):`keywords: ["pi-package"]`、`"pi": {"extensions": ["./herdr/a2a/pi-extension/a2a.ts"]}`、`private: true`、无依赖(插件只用 Node 自带模块)。
- 安装方式(fnx 把 pi 的包管理命令原样暴露:`fnx_dv install / remove / list / update`):每个 fnx 有自己的 agent 目录,**分别安装** `fnx_dv install git:github.com/HwJhx/A2A@a2a-plugin-v0.1.0`、`fnx_sw install …`。
- `a2a plugin install`(软链接)保留,用于开发与自动测试;两种方式不能同时用在同一个 fnx 上(会加载两份插件)。
- 文档:`herdr/a2a/README.md` 的"pi 插件"一节、`gui_demo/README.md` 新增"前提:fnx 装好 a2a 插件"、`gui_demo/验收步骤.md` §0 第 4 条。

### 12.3 验证

- VM 能访问 GitHub(`git ls-remote https://github.com/HwJhx/A2A.git` 成功);仓库里没有大文件(视频、录屏未纳入 git)。
- 本地路径验证(不联网、不调用模型):备份 `~/.forenyx/fnx_dv/agent/settings.json` → `fnx_dv install /Users/…/A2A`:settings 的 `packages` 多一条,`fnx_dv list` 列出;用 8767 开发后端建工程、新增真实 fnx_dv 的 `dv_uart`,启动画面 `[Extensions]` 里显示 `a2a.ts` → 删工程、停后端 → `fnx_dv remove /Users/…/A2A`(之后 settings 留下空的 `"packages": []`)→ 用备份还原,与安装前逐字节一致。
- **未验证**:从 GitHub 按标签在线安装(要等用户提交、打标签、推送);带标签时 `fnx_dv remove` 的确切写法;插件版本与后端版本不一致时的表现。

### 12.4 已知限制

- 在线安装只装插件;Python 后端位置来自 pane 环境变量(`a2a agent spawn` 注入),版本要对应。
- (已改,见 §12.5)`a2a plugin status` 原先只检查 `extensions/` 目录,看不到在线安装的那份。
- 模型仍可用 bash 绕过插件直接调 a2a 命令行(只要启动时没关内置工具);Router 只核对发送方身份,跨 IP 仍不可能。

### 12.5 Codex 审核在线安装(2026-10-14)

阻断无。确认:根目录 `package.json` 的单文件 `pi.extensions` 清单符合 pi 0.79.10 规则、只加载 A2A 插件;git 安装时 `npm install` 在克隆目录运行,当前无依赖、脚本、workspace,不会装嵌套的 GUI 包;`private` / `UNLICENSED` 不影响;fnx 命令与升级说明准确;pi 按仓库身份匹配 remove 来源,带标签的来源可用于 remove(推送后仍实测)。

| # | 问题 | 判断 | 处理与验证 |
| --- | --- | --- | --- |
| 应修 | `plugin.py` 的 status / install 只检查 `extensions/`,不看 settings 的 `packages`:先在线安装再查 status 显示 `not_installed`,随后软链接 install 成功 → 两份插件同时加载 | 真问题 | 新增 `online_packages()`:读 settings 的 `packages`(字符串或带 `source` 的对象),识别指向本仓库的来源——各种 git 写法统一成"主机/路径"比较,本地路径按 settings 所在目录**按字面**拼接规整后取真实路径与仓库根目录 / 插件文件比较;settings 读不了或格式不对抛错(fail-closed)。status 报 `online_package`、两种都装时报 `duplicate`;已有在线包时 install 拒绝。反方向(先软链接后在线安装)挡不住,README 写明。单元测试新增 6 个(git 各种写法与对象形式、本地绝对 / 相对路径、无关包不拦、两份并存报 duplicate、settings 坏了 fail-closed);Mac 629 个通过(51 个需要 herdr 的跳过);VM 上插件测试 + 真实 fnx 的 `RealFnxPlugin` 19 个通过。真实 fnx_dv 实测:在线安装后 status 为 `online_package`、软链接 install 被拒(退出码 6、没建 `extensions/`),之后 settings 逐字节还原。 |
| 将来考虑 | 标签可以被移动 | 是 | README 写明:严格可复现可用提交号固定,或保护 release 标签 |

测试自己的问题:第一次相对路径用例失败——测试临时目录在 `/var`(指向 `/private/var` 的软链接)下,我用 `Path.resolve()` 先展开软链接再处理 `..`,层数算错;改为与 pi 一致的"按字面拼接规整、再取真实路径"。

### 12.6 Codex 复核在线安装修改(2026-10-14)

阻断无。应修 2 项,都是真问题:

| # | 问题 | 处理与验证 |
| --- | --- | --- |
| 1 | `_git_identity` 手工归一化不覆盖 pi 接受的全部写法(HTTPS 的 `#tag`、含斜杠的 `@branch/name`、带端口的 SSH、大写协议),会漏检 | 查了 pi 0.79.10 的实现(`parseGitUrl` 用 hosted-git-info,还处理 scp 写法、`#ref`、`@ref` 等),决定**不再追写法**:主要改为**按安装结果识别**——扫描 pi 实际的安装位置 `<agent>/git/**`(克隆)与 `<agent>/npm/node_modules/*`,`package.json` 的 `name` 等于本仓库的 `a2a-pi-plugin` 就算已在线安装(跳过克隆里的 `node_modules`、`.git`),与来源写法无关。按写法识别保留作补充,并补上:协议不分大小写、去掉 `#ref`、`@ref` 只截仓库名之后(分支里的斜杠在后面的段)、端口、路径只取"用户/仓库"两段。 |
| 2 | 所有 `scheme://` 都当远程,pi 把 `file://` 当本地路径,会漏检;本地路径 `resolve` 抛 `OSError` 时 `continue` | `file://` 先转成本地路径再按本地规则判断;本地路径改用 `os.path.realpath`(不会因路径不存在而抛错),不再有"出错就跳过"的分支;目标目录的 `package.json` name 为 `a2a-pi-plugin` 也算本仓库。settings 读不了仍拒绝(fail-closed)。 |

测试:单元测试改 / 增 4 个(Codex 列的各种写法、`file://` 指向仓库与插件文件、按安装副本识别——git 克隆、别的主机上的镜像、npm 安装,来源写成解析不了的怪写法也能认出;别的包及其依赖里的同名包不算;`package.json` 的 name、`pi.extensions`、关键字与代码里的常量一致)。Mac 632 个通过(51 个需 herdr 的跳过);VM 插件测试 + `RealFnxPlugin` 22 个通过。变异检查 4 个都被抓到:不扫描已安装副本、git 识别不去 `#标签`、不处理 `file://`、协议区分大小写。真实 fnx_dv:`fnx_dv install file:///Users/…/A2A`(pi 在 settings 里存成相对路径)→ `a2a plugin status` 为 `online_package`、软链接 install 被拒 → settings 逐字节还原。

### 12.7 Codex 第三轮复核(2026-10-14)

阻断无。确认扫描深度:正式 GitHub 路径 `<agent>/git/github.com/HwJhx/A2A` 深 3,npm 普通 / scoped 包深 1 / 2,`max_depth` 都覆盖。

| # | 问题 | 判断 | 处理与验证 |
| --- | --- | --- | --- |
| 应修 | 只查 agent 级;pi 还加载项目级 `<cwd>/<configDir>/settings.json` 的 packages 与 `.../git`、`.../npm` 里的副本,项目级已在线安装时再装 agent 级软链接,该项目里仍加载两份 | 真问题(查了 pi 0.79.10:`projectSettingsPath = join(cwd, CONFIG_DIR_NAME, "settings.json")`,`getGitInstallRoot / getNpmInstallRoot("project") = join(cwd, CONFIG_DIR_NAME, …)`;fnx_dv、fnx_sw 的 configDir 都是 `.forenyx`,只看 cwd、不向上找) | 软链接在 agent 级、对所有项目生效,a2a 不知道有哪些项目,所以:`status` / `install` 新增可重复的 `--project-dir`,按 pi 同样的布局检查 `<项目>/<configDir>/`(configDir 取 fnx `libexec/package.json` 的 `piConfig.configDir`,缺省 `.pi`)——settings 的 packages、`git/`、`npm/` 副本、`extensions/` 里的 `a2a.ts` 与注册 `a2a_send` 的扩展;项目级有本插件时 install 拒绝,status 列 `project_packages`、两级同时存在报 `duplicate`;项目目录不存在或 settings 读不了时拒绝(fail-closed)。不带 `--project-dir` 时 status 输出 `project_scope` 提示"未检查项目级",README 写明只保证 agent 级。单元测试新增 4 个、CLI 测试加 `--project-dir` 用例;Mac 全套通过(51 个需 herdr 的跳过);VM 插件测试 + `RealFnxPlugin` 26 个通过;变异 3 个都被抓到(install 不查项目级、项目级不看 extensions、configDir 固定 `.pi`)。真实 fnx_dv:在临时目录 `fnx_dv install -l <仓库>`(写到 `<临时目录>/.forenyx/settings.json`)→ `status --project-dir` 列出、`install --project-dir` 被拒;agent 级 settings 未变,临时目录已删。 |
| 将来考虑 | `max_depth=4` 不覆盖任意深的 git 命名空间(如 GitLab 子组);按 `package.json` name 识别可能把无关同名包误判、保守拒绝 | 是,影响小 | 记录;需要时再核对 `repository` 字段 / `pi.extensions` 清单 |

### 12.8 Codex 第四轮复核(2026-10-14)

阻断无;认可"不带 `--project-dir` 只保证 agent 级"的边界。

| # | 问题 | 判断 | 处理 |
| --- | --- | --- | --- |
| 应修 | 项目级相对来源 pi 按项目 cwd 解析,代码按 `<项目>/<configDir>` 解析,`../A2A` 会漏检 | **不成立**(按 fnx 自带的 pi 0.79.10 核实):`getBaseDirForScope(scope)` 对 `"project"` 返回 `join(this.cwd, CONFIG_DIR_NAME)`、对 `"user"` 返回 `agentDir`;写 settings(`normalizePackageSourceForSettings`:`relative(baseDir, …)`)与加载(`resolveLocalExtensionSource(…, baseDir)`、已安装路径查找 `resolvePathFromBase(parsed.path, baseDir)`)都用它。§12.7 的真实实测也对得上:在 `/tmp/a2a_projlvl_xc` 项目级安装后 settings 存的是 `../../../Users/…`(3 层,从 `<项目>/.forenyx` 算;从项目根目录算只要 2 层)。 | 不改代码;补回归测试固定这一规则:settings 写 `../A2A_link`,只在 `<项目>/../A2A_link` 建指向本仓库的软链接时不算(那是"按项目根目录"的解释),在 `<项目>/A2A_link` 建时才算。 |
| 将来考虑 | 同 §12.7 | — | 已记录 |

### 12.9 Codex 第五轮复核(2026-10-14)

上一轮"项目级相对路径基准"一项 Codex 确认关闭(它看错了返回值;基准确为 `<项目>/<configDir>`)。不带 `--project-dir` 只保证 agent 级的边界认可。

| # | 问题 | 判断 | 处理与验证 |
| --- | --- | --- | --- |
| 应修 | `_conflicts` 只扫 `extensions/` 顶层 `.ts/.js`;pi 还加载 `extensions/<子目录>/index.ts`、子目录 `package.json` 的 `pi.extensions` 入口,漏检后仍可装软链接造成重复加载 | 真问题,且 agent 级与项目级共用此函数,两处都漏(核对 pi 0.79.10 `discoverExtensionsInDir` / `resolveExtensionEntries`:顶层 `.ts/.js` 文件含软链接;子目录含目录软链接,按 清单入口 → `index.ts` → `index.js`;`extensions/` 本身有清单或 index 时只用它们) | 新增 `_dir_entries`、`_extension_entries` 按上述规则枚举入口;`_conflicts` 对每个入口判断"真实路径是本仓库插件"或"文本注册 `a2a_send`";宁多勿少(不跳过点开头条目)。`a2a.ts` 本身仍由调用方按 state 判断。单元测试新增 2 个(子目录 index.ts / index.js / 子目录清单 / extensions 本身清单 × agent 级与项目级各一遍;子目录里软链接到本仓库插件的 index.ts;pi 不加载的嵌套文件与 .md 不算)。Mac 639 个通过(51 个需 herdr 的跳过);VM 插件测试 + `RealFnxPlugin` 29 个通过;变异 4 个都被抓到(不看子目录、不看 extensions 本身清单、不看 index.js、不认本仓库插件真实路径)。 |

### 12.10 Codex 第六轮复核(2026-10-14)

Codex 确认项目级相对路径基准一项关闭。

| # | 问题 | 判断 | 处理与验证 |
| --- | --- | --- | --- |
| 应修 | `_extension_entries` 对 `extensions/` 根目录有 index / 清单时提前返回;pi 的 `discoverExtensionsInDir` 不看根目录清单、总是加载顶层全部 `.ts/.js` → `index.ts` + `other.ts` 并存时漏检 `other.ts`;根清单测试可能误报 | **部分成立**。核对 fnx 内 pi 0.79.10:fnx CLI 实际走包管理器的 `collectAutoExtensionEntries`(agent 级与项目级 `extensions/` 都是,见 `addResources("extensions", collectAutoExtensionEntries(…))`),它**先**看根目录清单 / index、有则只用它们——即原实现与实际运行路径一致,`other.ts` 实际不会加载,根清单也不是误报;Codex 引用的 `discoverExtensionsInDir` 只被 `discoverAndLoadExtensions` 调用,而后者在 fnx 里没有调用方(SDK 导出接口)。但两套规则都是 pi 的正式行为,取并集只会多拒绝、不会漏检。 | 改为两套规则的并集:根目录清单 / index 入口 + 顶层全部 `.ts/.js` + 各子目录入口。根清单测试保留(对实际路径成立)。新增回归 `index.ts` + `other.ts` 并存时 `other.ts` 被检出。Mac 全套通过(51 个需 herdr 的跳过);VM 插件测试 + `RealFnxPlugin` 30 个通过;变异 2 个都被抓到(恢复"根有 index 就提前返回"、不看根目录清单)。 |

### 12.11 Codex 第七轮复核(2026-10-14):可以提交

阻断无、应修无。Codex 按本机 pi 0.79.10 源码确认:fnx 包管理器的 `collectAutoExtensionEntries` 先解析 `extensions/` 根目录清单或 index,有入口就返回,否则扫描顶层 `.ts/.js` 与一级子目录入口,agent / project 两级都调用它;并集实现与之一致,也覆盖 SDK `discoverExtensionsInDir` 的规则。前提"接受并记录保守误报边界"已写入 `herdr/a2a/README.md`:并集扫描会把 fnx 忽略、SDK loader 可能发现的入口(点开头、忽略规则排除的文件)也算冲突;按 `package.json` name 识别可能误判无关同名包;`git/` 只扫 4 层。这些都只会保守地拒绝安装,不会造成重复加载。

### 12.12 codex-test 交叉验证(2026-10-14)

独立复测环境:OrbStack VM `ubuntu` / Ubuntu 22.04.5 LTS / Python 3.10.12。运行前 Mac 与 VM 的 `plugin.py`、`cli.py`、`test_plugin.py`、`test_integration_lifecycle.py`、根 `package.json` SHA-256 一致；未修改工作区实现或测试文件。

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| VM 插件单元 + 真实 fnx 生命周期 | 通过，30/30，失败 0、跳过 0 | 从 `/mnt/mac/Users/jhx/Documents/code/personal/pi_agent/A2A/herdr/a2a` 清除项目下 `__pycache__` 后运行 `PYTHONPATH=/mnt/mac/Users/jhx/Documents/code/personal/pi_agent/A2A/herdr/a2a/src PYTHONDONTWRITEBYTECODE=1 A2A_INTEGRATION=1 A2A_INTEGRATION_FNX=1 python3 -m unittest -v tests.test_plugin tests.test_integration_lifecycle.RealFnxPlugin`；实际输出 `Ran 30 tests in 9.911s`、`OK`。真实 fnx 用例通过；没有发送 prompt、没有调用模型。 |
| fnx_dv agent 级、本地路径 | 通过 | `fnx_dv install <仓库路径>` 后 `a2a plugin status --agent-dir ~/.forenyx/fnx_dv` 为 `online_package`；`a2a plugin install` 退出码 6 并拒绝重复安装，`agent/extensions/` 未创建；用同一路径 `fnx_dv remove` 后还原 settings，`cmp` 逐字节一致。 |
| fnx_dv agent 级、`file://` 来源 | 通过 | `fnx_dv install file://<仓库绝对路径>` 后 status 为 `online_package`；软链接 install 退出码 6；用同一 file URL remove，settings 经 `cmp` 逐字节还原。 |
| fnx_dv 项目级 package | 通过 | 在 `/tmp/a2a_xc_proj` 中 `fnx_dv install <仓库路径> -l --approve` 写入 `.forenyx/settings.json`；status `--project-dir` 列出 `project_packages`，重复软链接 install 退出码 6；不带 `--project-dir` 的 status 输出 `project_scope` 提示。临时项目首次未被信任时，未加 `--approve` 的安装被 fnx 明确拒绝；加该显式选项后才写入。agent 级 settings 未变。 |
| 嵌套扩展冲突 | 通过 | 删除临时项目的 package 项后创建 `.forenyx/extensions/x/index.ts`，内容注册 `name: "a2a_send"`；status 将其列入 `project_packages`，install 以退出码 6 拒绝。 |
| 变异检查 | 通过，2 处均被目标测试捕获 | 在 `/tmp/a2a_xc_mutation_repo` 隔离源码副本里临时关闭 online package guard，`test_install_refuses_when_already_installed_online` 的 9 个子用例均因未抛 `PluginError` 失败；关闭 project guard 后，`test_project_level_package_blocks_install` 出现 3 个“未拒绝”断言失败。后者在首个失败后继续跑后续子场景，额外出现 1 个夹具目录已存在错误；目标防护失败已明确捕获。两处修改均用 trap 恢复并与备份 `cmp` 一致，隔离副本最终与原 `plugin.py` `cmp` 一致。 |
| 清理与边界 | 完成 | fnx_dv / fnx_sw 两份 agent settings 均与测试前备份逐字节一致；fnx_dv agent `extensions/` 不存在；`/tmp/a2a_xc_proj`、变异副本、VM 备份和 Mac `/private/tmp/a2a_xc_mutation_*.sh` 已删除。未触碰禁用 Herdr 会话、未读取 `auth.json` 或密钥、未调用模型。 |

结论:§12 所述在线安装防重、`file://` 来源、项目级作用域和嵌套扩展冲突在 VM 的独立复测中均符合预期；防护测试能检出 agent 级与项目级 guard 被移除的变异。
