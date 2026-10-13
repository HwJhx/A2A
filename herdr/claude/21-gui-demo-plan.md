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
