# Web GUI(每个 agent 一个 tmux)与 herdr 方案的冲突分析

> 2026-10-13。起因:用户说明 web GUI 由另一位同事负责,GUI 为每个 agent 创建一个 tmux,agent 之间相互独立;担心采用 herdr 方案会冲击 GUI。用户就此问了 DeepWiki(对话:https://deepwiki.com/search/tabpane_2e4f4c70-a847-41f2-8f04-d4e13fe679f4),要求:①实事求是地重新整理担心的描述;②判断担心是否合理;③分析 DeepWiki 的答复是否合理。
> 本文的实测在 VM(herdr 0.9.3)上进行,用假 agent,不调用模型。

## 1. 担心的重新整理

- **同事的 GUI 现状**:网页上每个终端小窗口背后是一个 tmux 会话,agent 在这个 tmux 里由 GUI 启动、运行,各 agent 相互独立。
- **a2a 的前提**:agent 必须运行在 **herdr 的 pane 里**,由 `a2a agent spawn` / `restore` 启动。broker 投递(`agent prompt`)、判断忙闲(herdr 识别 `pi` 进程与状态上报)、停止(`pane process-info` 拿进程组)都依赖 herdr 能"看到"这个进程。
- **冲突点**:一个 agent 进程只能挂在一个终端上——要么在 tmux 里,要么在 herdr 的 pane 里,不能两边都在。
- **所以担心**:采用 herdr 方案后,GUI 原来"开 tmux、启动 agent、显示它"这一套是否要推倒重来或大改。
- **"独立"指什么需要先确认**:herdr 的 pane 不等于一个独立的 tmux server。同一个 herdr 会话里的所有 pane 由同一个 herdr server 持有,server 出问题时这些 agent 一起受影响。如果"agent 之间相互独立"指的是故障隔离或权限隔离,那么 attach 方案并不等价;若改成每个 agent 一个 herdr 会话来求隔离,a2a 目前不支持跨会话通信(Router 拒绝发往另一个会话的目标),需要另外设计和验证。(Codex 审核补充)

## 2. 担心是否合理

**冲突是真的,但影响可以很小。** 关键是 herdr 0.9.3 自带的 `herdr agent attach <agent 名> [--takeover]`(帮助说明:"Attach directly to an agent terminal")。

### 2.1 实测(VM,herdr 0.9.3,假 agent,不调用模型)

做法:起一个 herdr 会话,在其 pane 里以 `pi` 的名字运行假 agent(`tests/fake_agent.py`,收到的字节写日志)并命名为 `probe_agent`;另开一个**独立的 tmux 会话**模拟 GUI 的一个窗口,在里面运行 `herdr --session <会话> agent attach probe_agent`。

| 测试 | 结果 |
| --- | --- |
| 独立 tmux 会话里 `herdr agent attach probe_agent` | 该 tmux 窗口里**只显示这一个 agent 的终端**(没有 herdr 自己的边框、标签页) |
| 在这个 tmux 窗口里打字 | agent 收到 `hello-from-gui` |
| attach 期间用 herdr API 发 prompt(broker 投递的方式) | agent 收到 `prompt-from-api`;herdr 仍能识别它的状态(idle) |
| 第二个窗口同时 attach 同一个 agent(不加 `--takeover`) | **失败**:`terminal attach failed: terminal ... already has an attached client; retry with --takeover`——同一个 agent 同一时刻只能有一个窗口连着 |
| herdr 自己的界面(`herdr session attach`)同时开着 | 不影响上面的 attach |

### 2.2 据此:前端布局可以不变,但后端的 agent 生命周期要改

实测只证明了三件事:终端能显示、能输入、attach 期间 API 投递照常可用。它**不能**说明 GUI 只需小改。要区分两部分:

- **前端布局(可以不变)**:每个 tmux 窗口里原来运行"启动 agent"的命令,改为运行 `herdr --session <会话> agent attach <角色_IP>`;网页布局、tmux、显示终端的方式不变。
- **后端生命周期(需要改造)**:
  - agent 不再由 GUI 启动,改为由 GUI 后端调用 a2a(`a2a ip add`、`agent spawn` / `restore` / `stop` / `purge` 等)启动和管理;
  - GUI 要维护"窗口 ↔ agent(`角色_IP`)"的对应关系;
  - attach 何时断开、何时要重连,见 §7 实测:同一 pane 里 stop / restore 不断开;close、purge、restore 到新 pane、herdr 会话重启都会断开,需要重新 attach(agent 不在运行时 attach 会失败);
  - 一个 agent 同一时刻只能有一个写控制端(见 §2.1),多窗口或多人查看要另行设计。

(Codex 审核:原稿"GUI 只需一处改动"的说法过于乐观,已改。)

### 2.3 还没验证、需要与同事确认

1. 真实 fnx(pi 的界面)经 attach 显示是否正常(本次用的是假 agent)。
2. GUI 窗口改变大小时,agent 终端会不会跟着变,和 herdr 自己界面里的尺寸是否冲突。
3. GUI 窗口关闭再打开、断线重连时,agent 是否照常运行、能否重新 attach。
4. 同一个 agent 只能一个窗口看;GUI 若需要多人同时看同一个 agent,只能 `--takeover`(会把前一个挤掉)或另想办法。
5. 同事 GUI 的具体技术(例如 ttyd / xterm.js + WebSocket 连 tmux)尚不清楚。
6. **attach 后可以直接往 agent 里打字,这部分输入不经过 a2a**:不受通信边、模板和 Router 鉴权的约束(a2a 只约束 agent 之间经 `a2a_send` 发的消息)。如果 GUI 有多个用户,需要在 GUI 服务端做授权(谁能看、谁能操作哪个 agent);herdr 的本机 socket 不能暴露出去。(Codex 审核补充)
7. 不同 agent 同时被多个窗口 attach、长时间实时渲染的表现也还没测。

## 3. DeepWiki 的答复是否合理

| DeepWiki 的说法 | 评价 |
| --- | --- |
| herdr 不能接管已有的 tmux 会话,agent 必须真正跑在 herdr 的 pane 里 | **正确**,这正是冲突所在。要区分两件事:herdr **不能接管任意已有的 tmux 会话**;但 herdr 有**自己的**终端直连能力(attach / 观察 / 控制自己 pane 里的终端),这不是接管 tmux |
| pane 不必在同一个 tab;前端可以不管 tab,按 pane 平铺 | 正确,但与本问题关系不大 |
| "不改前端布局,能实现" | **结论勉强成立,路线偏了**。它的方案是把 tmux 换成 herdr,再让网页前端直接对接 herdr:要么用 `pane.read` 读快照(不是实时画面,它自己也承认没找到输出流接口),要么用 herdr 内部的二进制客户端协议(`wire.rs`,PROTOCOL_VERSION = 22)。这要重写 GUI 的终端后端,而且该二进制协议不在 herdr 的公开文档里,herdr 升级就可能对不上 |
| "herdr 本身就是为这个形态设计的" | **说过头了**。herdr 公开的定位是带自动化 API 的终端工作区,嵌入网页不是它文档里写的用法 |
| socket 是本机 Unix socket,浏览器连不上,需要后端中转 | **正确**,也符合"不把 herdr socket 暴露到局域网"的约束 |
| **上下文不完整** | 它在 codemap 里**提到了**单终端直连(`ObserveTerminal` / `ControlTerminal`,"一个 web 小窗口绑定一个 agent 终端"),方向是对的;但没有指出 0.9.3 已有现成的命令行入口 `herdr agent attach`(实测可用,可直接放进现有 tmux 窗口),而是引向在内部二进制协议上自建渲染。(原稿说它"遗漏了 attach",不准确,已按 Codex 审核更正。)另外它读的是 herdr 仓库 `docs/next`(未发布)的文档,`pane.move`、`events.subscribe` 等在 0.9.3 上不一定都有 |

## 4. 结论与建议

- 担心合理,但不必推翻 GUI:**前端布局不变总体可行**,GUI 保留"每个 agent 一个 tmux 窗口",窗口里改为 attach 到 a2a 管理的 agent。
- 但**后端的 agent 生命周期必须改造**:启动、停止、恢复改由 a2a 负责,GUI 后端调用 a2a、维护窗口与 agent 的对应、在 agent 重启后重新 attach,并负责多用户时的授权。
- 先要和同事确认"独立 tmux"在他那里的实际含义:如果只是界面上互不干扰,attach 方案满足;如果要求故障或权限隔离,attach 方案不等价,需要另外讨论(见 §1)。
- 与同事沟通前,建议先补做 §2.3 的第 1–3 项验证(真实 fnx 经 attach 的画面、窗口改大小、断开重连),不调用模型,约 10 分钟。

## 5. 用户补充的三个问题(2026-10-13)

> 依据:代码(`herdr/a2a/src/a2a/` 的 lifecycle、topology、ipops、cli)与 §2.1 的实测;未实测的明确标出。

### 5.1 两个 tmux 分别 attach 两个 herdr pane,这两个 pane 在同一个 tab、不同 tab 都可以吗?

**可以,但有两点:**
- **attach 的对象是 agent,不是任意 pane**:`herdr agent attach <agent 名或 pane 号>`,要求 pane 里正在运行被 herdr 识别出的 agent(pi)。**已实测**(§7 第 4b 项):pane 里只剩 shell 时按名字或 pane 号 attach 都失败(`agent_not_found`);但已经连着的窗口在 agent 停止后不会断开(§7 第 4a、5 项)。
- **与 tab、workspace 无关**:attach 直接连到 agent 的终端,不经过 herdr 自己的布局。**已实测**:不同 tab 的 dv、sw 各被一个窗口 attach,打字互不串扰(§7 第 3 项);两个工程的 agent 分别在不同 workspace(w2、w3),各被一个窗口 attach,同样互不串扰(§7 第 9 项)。

### 5.2 工程 / 智能体种类 / IP 都由前端创建,元数据能否完全由用户操作确定?

**层次划分与 a2a 现有布局完全对得上**(a2a 本来就这么布局,见 `lifecycle._new_pane`):

| GUI 操作 | herdr 层级 | a2a 里对应 |
| --- | --- | --- |
| 点 + 号,声明工程 | workspace | 拓扑的 `project_id` 与 workspace 名称 `workspace_label` |
| 进入"验证智能体""软件智能体"等界面 | tab | 角色;tab 名 = 该角色的 `label`(如"dv智能体") |
| 新增一个 IP 并命名 | pane | 节点(角色, IP);pane 名与 agent 名都是 `角色_IP`(如 `dv_uart`) |

**前提:**
- **前端不能直接调 herdr 建 workspace / tab / pane**,必须调 a2a(例如 `a2a ip add uart --roles dv`),由 a2a 在 herdr 里建 pane、启动 agent、写注册表和拓扑。绕过 a2a 自己建,a2a 就不知道这些 pane,消息发不过去。
- **herdr 的内部编号(如 `w1:t2:p3`)由 herdr 分配**,a2a 记在注册表里;用户决定的是名字。
- **名字有格式限制**:`project_id`、角色、IP 必须是小写字母开头,只含小写字母、数字、`_`、`-`,最长 32 个字符(`^[a-z][a-z0-9_-]{0,31}$`),例如 `uart`、`spi0`;`UART`、中文、带空格的都会被拒绝。**组合起来的 agent 名 `角色_IP` 也不能超过 32 个字符**,即 角色长度 + 1 + IP 长度 ≤ 32;这一条在启动 agent 时(`AgentIdentity.create`)才检查,拓扑本身接受超长组合,所以 GUI 最好在用户输入时就校验(Codex 审核补充)。workspace 名称不受此限,可以写中文。
- **除了名字还需要**:每种智能体的启动脚本(launcher,在拓扑里按角色配置一次)、每个 agent 的工作目录(`--cwd`,可用 `{ip}`、`{role}` 占位)、所用的 herdr 会话。这些要么由前端提供,要么预先配置。
- **"在验证智能体界面里新增 IP"的语义**:a2a 拓扑里 IP 是所有角色共用的。新增 uart 时如果只启动 dv(`--roles dv`),uart 在拓扑里就对所有角色都存在了,只是其他角色还没 agent;这时 dv 发给 sw_uart 的消息会被拒(目标没登记)。需要与同事约定:新增 IP 时是一次启动各角色,还是各界面分别启动。

### 5.3 智能体种类与通信边固定;现有接口能否被前端调用?

**种类与边**:都在拓扑配置里,5 种智能体和固定的边可以预先定好。补充:a2a 也提供运行中修改边的命令(`topology add-edge / remove-edge`),用不用由你们决定。

**接口现状**:目前只有**命令行 + Python 库**;**没有 HTTP / WebSocket 接口**。请求类命令的结果输出 JSON(成功在 stdout,出错在 stderr,带固定退出码);但参数写错时 argparse 输出普通文字(退出码 2),`a2a broker run` 是常驻进程,输出的是日志。(原稿"所有命令输出 JSON"不准确,已按 Codex 审核更正。)前端(浏览器)不能直接调,要由同一台机器上的后端(例如同事 GUI 已有的服务端)以子进程方式执行 `a2a ...`,或直接 import Python 模块。这个后端必须与 herdr 在同一台机器上:herdr 的 socket 只在本机,也约定过不暴露到局域网。

GUI 操作可用的现有命令:

| 用途 | 命令 |
| --- | --- |
| 新增 / 删除 IP(连带启动或清除各角色的 agent) | `a2a ip add/remove/undrain` |
| 单个 agent 的启动 / 停止 / 恢复 / 关闭 / 注销 | `a2a agent spawn/stop/restore/close/purge` |
| agent 列表与实时状态 | `a2a agent list` |
| 会话重启后恢复 agent | `a2a agent recover` |
| 查看拓扑、改边或模板 | `a2a topology show/add-edge/remove-edge/set-template` |
| 队列、单条消息、存储用量 | `a2a queue`、`a2a status [msg_id]` |
| 人工裁定、作废裁定、恢复投递 | `a2a resolve`、`a2a ruling void`、`a2a dispatch resume` |
| 消息投递进程(需常驻) | `a2a broker run` |

**GUI 可能需要但目前没有的**:重命名(agent 名固定为 `角色_IP`,没有改名命令);状态主动推送(只能轮询 `agent list` / `queue`,没有事件推送);一个后端同时管多个工程(现在一个状态目录对应一套拓扑,即一个工程,多工程需要多个状态目录);权限控制(a2a 没有用户概念;多用户 GUI 的授权要在 GUI 服务端做,herdr 的本机 socket 不能暴露)。

**之前的验证都没有经过 GUI**:单元测试直接调 Python;VM 集成测试和真实 fnx 测试是在命令行执行 `a2a` 或由脚本调用。"用户在 GUI 里点击 → 配置 / 启动 / 通信"这条链路**从来没测过**,GUI 与 a2a 的对接方式也还没有定义。

### 5.4 建议的下一步

1. **补做 attach 验证**(不调用模型,约 10–15 分钟):真实 fnx 的画面、窗口改大小、断开重连、agent stop → restore / purge 之后重新 attach,加上 §5.1 的"两个 tmux 分别 attach 不同 tab 的 agent"与多个 agent 同时被 attach 时的实时渲染。
2. **写一份给同事的 GUI 对接说明**:每个 GUI 操作对应哪条 `a2a` 命令、参数与返回的 JSON、名字格式限制、IP 跨角色的语义、目前缺少的几项,便于决定由谁补什么(例如要不要加一个 HTTP 包装层)。

## 6. Codex 审核记录(2026-10-13)

结论:无阻断,核心结论成立;应修 6 项已按上文修改(§1 独立性、§2.2 前端布局与后端生命周期分开说、§2.3 attach 输入不经 a2a 鉴权与授权、§3 DeepWiki 评价更正、§5.2 agent 名总长度、§5.3 输出格式)。将来考虑(并入 §5.4 第 1 项):窗口改大小、stop → restore / purge 后重连、不同 agent 并发 attach 与实时渲染;每个终端同一时刻只有一个写控制端。

## 7. attach 补充验证(2026-10-13,VM,herdr 0.9.3,真实 fnx_dv / fnx_sw,不调用模型)

做法:用 `a2a agent spawn` 启动真实的 fnx_dv、fnx_sw(`--no-builtin-tools`,各在自己角色的 tab 里——a2a 按角色分 tab,集成测试 `test_spawn_layout_identity_and_naming` 有断言);每个"GUI 窗口"是一个独立的 tmux 会话,里面运行 `herdr --session <会话> agent attach <agent>`。全程不按回车提交,所以没有模型调用。agent 终端尺寸通过 pi 进程的 pts 设备读取(TIOCGWINSZ)。

| # | 测试 | 结果 |
| --- | --- | --- |
| 1 | 真实 fnx 经 attach 显示 | **正常**:窗口里是完整的 pi 界面(标题、快捷键提示、Skills 列表、状态栏、模型名),没有 herdr 自己的边框 |
| 2 | GUI 窗口改大小 | agent 终端**跟随 attach 的窗口**:attach 前 49×173(herdr 自己界面里 pane 的大小)→ attach 120×30 的窗口后变 30×120 → 把窗口改成 90×24 后变 24×90,pi 按新宽度重新排版。也就是说 attach 期间 agent 终端的尺寸由 GUI 窗口决定,herdr 自己界面里的尺寸被覆盖(两者同时开着时,herdr 界面里那个 pane 的显示会与它的格子不一致) |
| 3 | 不同 tab 的两个 agent 各被一个窗口 attach,各自打字(不回车) | **互不串扰**:dv 只收到 dv 窗口打的字,sw 只收到 sw 窗口的;两个 agent 状态保持 idle |
| 4a | agent 被 `a2a agent stop` 之后,原来的窗口 | **窗口不断开**:attach 连的是 pane 的终端,不是 agent 进程;pi 退出后窗口里显示的是该 pane 的 shell 提示符(在 agent 的工作目录里) |
| 4b | 此时新开窗口 attach 这个 agent(按名字或按 pane 号) | **失败**:`agent_not_found`(pane 里没有 agent 时不能 attach) |
| 5 | `a2a agent restore`(在原 pane 里重启) | 原来的窗口**自动显示重启后的 pi**,在窗口里打字能送到新的 agent;但新开窗口 attach 会失败("已有客户端",原窗口还连着) |
| 6 | `a2a agent close` 之后 | 原窗口的 attach **结束**:`terminal attach ended: terminal ... not found`,退出码 1 |
| 7 | close 之后 `restore`(新建 pane,w2:p1 → w2:p3) | 必须**重新 attach**(新的终端);新窗口 attach 正常显示 pi |
| 8 | `a2a agent purge` 之后 | 窗口的 attach 结束(同 6) |
| 9 | 跨 workspace(补测):两个工程(两套状态目录,`workspace_label` 不同)在同一个 herdr 会话里各启动一个 agent(w2、w3),各被一个窗口 attach,各自打字 | **互不串扰** |
| 10 | herdr 会话重启(补测):`herdr session stop` 后已连的窗口 | attach **结束**:`server shut down: server is shutting down`,退出码 1 |
| 11 | 重新 attach 会话 + `a2a agent recover` 之后 | agent 回到原 pane(w2:p1);旧窗口**不会自动恢复**;新开窗口 attach 正常显示 pi |

**对 GUI 的含义**:
- 同一个 pane 里的 stop / restore,GUI 窗口可以一直连着,不用重连;但 stop 期间窗口里是一个**普通 shell**——用户在里面敲命令会在 agent 的工作目录里执行,而且如果此时前台有别的进程,`a2a agent restore` 会因为"pane 不是只剩 shell"拒绝重启(这是 a2a 的保护)。GUI 最好在 agent 停止期间不允许输入,或给出提示。
- close、purge、restore 到新 pane、herdr 会话重启(§7 第 10、11 项)时,attach 会结束,GUI 要能发现(attach 进程退出)并在 agent 恢复后重新 attach。
- attach 期间 agent 终端尺寸由 GUI 窗口决定。
- 同一个终端同时只能一个客户端连着;GUI 不能对同一个 agent 开两个窗口(除非 `--takeover` 挤掉前一个)。
- **多个工程共用一个 herdr 会话时,agent 名会冲突**:agent 名是 `角色_IP`,在同一个 herdr 会话里必须唯一(herdr 的 `agent_name_taken`),两个工程如果都有 `dv_uart` 会冲突。第 9 项是用不同 IP(uart、spi)绕开的,**冲突本身没有实测,依据是代码里的 `HerdrAgentNameTaken`**。多工程要么各用一个 herdr 会话(但 a2a 不支持跨会话通信,见 §1),要么约定 IP 名在会话内不重复。
- 本次没有测:长时间实时渲染(只截了静态画面)、GUI 网页层(ttyd / xterm.js 等)的表现、多个用户同时操作。

### 用户决定:每个工程各用一个 herdr 会话(2026-10-13)

据此,多工程时的对应关系是:**一个工程 = 一个 herdr 会话 + 一个 a2a 状态目录(一套拓扑、注册表、spool、审计) + 一个常驻 broker**。
- agent 名只需在工程内唯一,不同工程都可以有 `dv_uart`,不会冲突(上面的撞名问题不再出现)。
- a2a 不支持跨会话通信,但 a2a 的消息本来只在同一工程内流动(同 IP、按拓扑的边),不受影响;工程之间不能互发消息。
- 工程之间有**会话 / server 级**的故障隔离:一个工程的 herdr server 出问题,不影响其他工程。但这**不是**主机、资源或 fnx 配置的隔离:各工程仍在同一台机器上、共用同一份 fnx 配置与插件软链接;每多一个工程就多一个常驻的 herdr server 和 broker。(Codex 审核限定)
- GUI 的每个窗口 attach 时要带上工程对应的会话:`herdr --session <工程会话> agent attach <角色_IP>`;GUI 后端调用 a2a 时要带对应的状态目录(`A2A_STATE_DIR`)和 `--session`。
- **这是部署约定,当前实现不强制绑定**(Codex 审核):broker 的单实例锁按状态目录加(`<状态目录>/broker.lock`),与 herdr 会话无关;状态目录、会话、拓扑文件之间没有任何校验。如果 GUI 把工程 A 的状态目录配给工程 B 的 `--session`,agent 会在错误的会话里启动,broker 也可能跳过会话不匹配的消息。所以 GUI 必须为每个工程**固定一组(状态目录, herdr 会话, 拓扑文件)并每次一致地传**;如果显式设置了 `A2A_TOPOLOGY`,它也必须指向该工程自己的拓扑,不能几个工程共用。(将来可以考虑在 a2a 里加校验,例如在状态目录里记下所属会话,不一致时拒绝。)
- 本节是按代码与 §7 实测推导的配置方式,"多个工程各一个会话同时运行"本身还没有实测。

### Codex 审核 §7(2026-10-13)

阻断无;应修 2 项:①"herdr 会话重启后 attach 会断开"原是推断 → 已补测(第 10、11 项);② §5.1"不同 workspace 也一样"原无依据 → 已补测(第 9 项)。补测中发现多工程共用一个 herdr 会话时 agent 名会冲突(未实测,见上)。将来考虑:真实网页 GUI 的长时间渲染、断线重连、多窗口并发。

复核(补测与用户决定):阻断无;补测足以解决上一轮两条应修,"每工程一个会话 + 状态目录 + broker"方案成立。应修 1 项:写明这是部署约定、实现不强制绑定,GUI 必须固定并一致传递(状态目录, 会话, 拓扑文件)→ 已补进上一节。将来考虑:隔离只限会话 / server 级(已限定措辞);每工程的常驻开销;多个会话同时运行尚未实测。

## 8. 通信如何寻址:拓扑名字、agent 名与 herdr 的 w / t / p(2026-10-13)

> 用户的问题:①是否测过"两个 tab(验证 / 软件智能体)× 两个 IP(uart、spi),验证智能体的 uart 只能发给软件智能体的 uart";②是不是通过 pane 名通信、每个 pane 叫什么;③GUI 上创建项目、在某类智能体下创建 IP,确定的是 w / t / p,而拓扑里是 dv_ip、sw_ip,两者怎么映射。依据:代码(router、delivery、lifecycle、registry)与已有测试;本节没有新的实测。

### 8.1 测过没有

测过,规模比例子更大,只是 IP 名不是 uart / spi:

| 测试 | 规模 | 验证了什么 |
| --- | --- | --- |
| Router 单元测试(`test_router.py`) | dv / sw / rtl 三个角色,uart / gpio 两个 IP | 目标 IP 永远等于发送方自己的 IP(`test_target_ip_always_equals_sender_ip`);接口上无法跨 IP(`test_cross_ip_is_impossible_by_construction`);冒充 IP / 角色 / 项目被拒(`test_spoofed_ip_role_or_project`);方向错被拒 |
| VM 集成测试(真实 herdr、假 agent) | dv_uart、sw_uart、sw_gpio | 每个角色一个 tab(名为"dv智能体""sw智能体"),同一角色的不同 IP 在同一个 tab(`test_spawn_layout_identity_and_naming`) |
| 阶段 8 真实 fnx 规模测试(`real_fnx_scale.py`,调用模型) | 每个 IP 一个真实 fnx_dv + 一个真实 fnx_sw,跑过 5 个和 20 个 IP(ip00…ip19) | dv → sw → dv 往返;框架检查逐条核对每条消息的发送方、目标、文字都属于本 IP,不串 IP;20 个 IP 通过(提交 `2c9eb42`) |

没有专门用"uart、spi 各两个 pane"这个组合跑过,但内容等价。

### 8.2 通信不是按 pane 名指定目标

- agent 只能调用 `a2a_send(edge_id)`,唯一参数是通信边,**没有任何参数能指定目标**。
- 目标由 Router 推算:**目标 = (这条边的 to 角色, 发送方自己的 IP)**。例:dv_uart 调用 `a2a_send("dv_done")`,dv_done 的方向是 dv → sw,目标就是 (sw, uart) → `sw_uart`。
- 发送方身份取自它所在 pane 的环境变量(`A2A_ROLE`、`A2A_IP`、`A2A_PROJECT_ID`)加 herdr 注入的 `HERDR_PANE_ID`,并与注册表核对;模型说了不算。所以 dv_uart 从接口上做不到发给 sw_spi 或 dv_spi。
- 投递时 broker 按注册表登记的 **herdr agent 名**调用 `herdr agent prompt <agent 名>`,并核对它在登记的 pane 上(`delivery.py`)。

### 8.3 命名规则

统一固定为 `角色_IP`。pane 显示名和 herdr 里的 agent 名是 herdr 的**两个独立字段**,spawn 时 a2a 把两者**初始设成相同的值**(`lifecycle.py` 的 `pane_rename`、`agent_rename`)。投递只认 agent 名,显示名不参与寻址。例子里:

| tab(按角色,名字取拓扑里该角色的 label) | pane 显示名 = agent 名(初始) | 能发给谁(按通信边) |
| --- | --- | --- |
| 验证智能体(dv) | `dv_uart` | 只能发给 `sw_uart` |
| 验证智能体(dv) | `dv_spi` | 只能发给 `sw_spi` |
| 软件智能体(sw) | `sw_uart` | 只能发给 `dv_uart` |
| 软件智能体(sw) | `sw_spi` | 只能发给 `dv_spi` |

- "只能发给谁"的前提是拓扑里只有 dv → sw、sw → dv 两类边;加一条 dv → rtl 的边,dv_uart 也能发给 rtl_uart,但无论加什么边都不会跨 IP。
- GUI 可以另设 pane 显示名(如"UART 验证"),不影响通信;但**不能直接通过 herdr 改 agent 名**:注册表里记的 agent_name 会过期,投递找不到目标。a2a 的 agent 名必须是 `角色_IP`,且不超过 32 个字符。

### 8.4 拓扑名字与 w / t / p 的映射

**更正一个容易混淆的说法:通信不是通过 w / t / p,用户在 GUI 上的操作也不决定 w / t / p 的值。**

| 层次 | 由谁决定 | 例子 |
| --- | --- | --- |
| 拓扑(用户 / 配置) | 角色、IP、通信边 | 角色 `dv`、`sw`;IP `uart`、`spi`;边 `dv_done`(dv → sw) |
| agent 名(a2a 按规则推出) | `角色_IP` | `dv_uart`、`sw_spi` |
| 显示名(a2a 按规则设置) | workspace 名 = 工程的 `workspace_label`;tab 名 = 角色的 label;pane 名 = agent 名 | "SoC-A"、"验证智能体"、`dv_uart` |
| w / t / p 编号(herdr 分配) | 创建时由 herdr 生成 | `w1`、`w1:t2`、`w1:p3` |

拓扑里**没有** `dv_uart` 这种名字,只有角色列表和 IP 列表;agent 名由 a2a 拼出。w / t / p 由 herdr 在创建时分配,用户和 a2a 都不能事先指定。

**映射靠注册表**:a2a 在 herdr 里建好 pane 后,把 herdr 返回的编号记进 `<状态目录>/registry.json`,以 agent 名为键,例如:

```json
"dv_uart": {
  "role": "dv", "ip_id": "uart", "project_id": "soc_a", "session": "soc_a",
  "workspace_id": "w1", "tab_id": "w1:t2", "pane_id": "w1:p3",
  "agent_name": "dv_uart", "lifecycle": "running", ...
}
```

**创建**(用户在"验证智能体"界面新增 IP uart,GUI 后端执行 `a2a ip add uart --roles dv --session <工程会话>`):
1. 拓扑里加 IP uart;
2. 在 herdr 里按名称找 workspace("SoC-A"),没有就新建 → herdr 返回 w1;
3. 在 w1 里按名称找 tab("验证智能体"),没有就新建 → herdr 返回 w1:t2;
4. 取 pane:新建的 workspace / tab 自带的那个 pane 直接用;tab 里已有 pane 时,从最后一个 pane 往下分出一个新 pane → herdr 返回 w1:p3;
5. 在 w1:p3 里启动 fnx_dv,把 pane 显示名和 herdr 里的 agent 名都设成 dv_uart;
6. 注册表记下 dv_uart → (w1, w1:t2, w1:p3)。

**通信**(dv_uart 里的模型调用 `a2a_send("dv_done")`):
1. Router 核实发送方:pane 环境变量是 dv、uart,与注册表一致;
2. 推算目标:dv_done 指向 sw,IP 用发送方自己的 uart → sw_uart;
3. 查注册表:sw_uart 在 pane w1:p5,agent 名 sw_uart;
4. broker 用 `herdr agent prompt sw_uart` 投递,并核对它在 w1:p5。

**拓扑与规则决定发给谁,注册表把名字翻译成 herdr 的位置**。通信目标不由 w / t / p 决定;a2a 用它们记录和管理 pane 位置(投递核对,以及 stop / restore / close / recover 等生命周期操作,都持久化在注册表里),GUI 不需要自己分配。GUI attach 也用名字:`herdr --session <工程会话> agent attach dv_uart`,不需要记 w / t / p(要记的话可从 `a2a agent list` 的 `pane_id` 取)。

### 8.5 与用户设想的 GUI 流程的关键差别

如果设想是 **GUI 自己先调 herdr 建好 workspace / tab / pane,再交给 a2a**,那么**目前做不到**:a2a 只支持自己创建 pane,没有"把已有 pane 登记成某个 agent"的命令;GUI 自己建的 pane,a2a 不知道、注册表里没有,消息发不过去。

两种做法:
1. **GUI 改为调用 a2a 创建**(推荐,现在就能用):用户点"新增 IP",GUI 后端执行 `a2a ip add <ip> --roles <角色>`,a2a 建好 pane 后返回;GUI 从返回结果或 `a2a agent list` 拿到 agent 名与 pane 编号,再 attach。
2. **给 a2a 加"登记已有 pane"的命令**:GUI 先自己建 pane,再交给 a2a 接管(校验位置与名字符合规则、在里面启动 agent、写注册表)。需要新开发,并要校验:GUI 建的 tab / workspace 名必须与拓扑对得上、pane 必须只剩 shell。

需不需要第 2 种,取决于同事的 GUI 能否接受"创建动作交给 a2a"。

### Codex 审核 §8(2026-10-13)

阻断无;§8.1、§8.2、§8.5 与代码和测试相符。应修 2 项,已改:① §8.3 pane 显示名与 herdr agent 名是两个独立字段,只是初始相同;GUI 可另设显示名,但不能直接改 agent 名;② §8.4 w / t / p 不只用于最后投递,也被生命周期操作使用并持久化。将来考虑:无。

## 附:实测脚本(要点)

```bash
# herdr 会话 + 假 agent
tmux new-session -d -x 200 -y 50 -s ${S}_tty "cd ~ && herdr session attach $S"
herdr --session $S pane run w1:p1 "bash -c 'exec -a pi python3 tests/fake_agent.py <日志> work'"
herdr --session $S agent rename w1:p1 probe_agent
# 模拟 GUI 的一个窗口:独立 tmux 会话里 attach
tmux new-session -d -x 120 -y 30 -s ${S}_gui "herdr --session $S agent attach probe_agent"
tmux capture-pane -p -t ${S}_gui            # 只显示该 agent 的终端
tmux send-keys -t ${S}_gui "hello-from-gui" Enter      # agent 收到
herdr --session $S agent prompt probe_agent "prompt-from-api"   # attach 期间 API 投递仍可用
tmux new-session -d -s ${S}_gui2 "herdr --session $S agent attach probe_agent"   # 失败:已有客户端,需 --takeover
```
