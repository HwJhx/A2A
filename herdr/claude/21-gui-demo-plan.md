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
