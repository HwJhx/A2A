# 路线图与进度清单

> 规则:**完成一项,在行尾打 ✅**。未完成的行尾留空。只有真正做过并确认的才打 ✅。
> 最后更新:2026-10-08
>
> 相关文档:
> - `01-install-and-launch-agents.md` 安装与启动
> - `02-multi-agent-communication.md` 通信机制调研
> - `03-requirements-restated.md` 需求整理(含 §11、§12 你的答复)
> - `04-design-python-framework.md` 设计草案(v3,已合入 codex 方案)

---

## 阶段 0:前期调研与设计(已完成)

- [x] 安装 herdr、在 pane 中启动 claude / codex 的流程文档 ✅
- [x] 调研 herdr 多 agent 通信机制(`agent prompt/wait/read`、socket API)✅
- [x] 分析 B站介绍视频(转写 480 段,已整理要点)✅
- [x] 整理你的需求(角色 × IP 矩阵、同 IP 通信、有向边)✅
- [x] 你确认需求、回答 Q1/Q2/Q4/Q6/Q9 及第二轮 7 项决定 ✅
- [x] 完成设计草案 `04`(拓扑、模板、鉴权、broker、状态机)✅
- [x] 对比 codex 方案,合入 6 项借鉴点,并按你的决定修订 ✅
- [x] 读 `pi-custom` 源码,确认 pi 有 `bash` 工具、`registerTool`、`tool_call` 钩子 ✅
- [x] E7 决定:接收方先不验证来源 ✅

## 阶段 1:虚拟机环境探测

### 1.1 只读探测(已做,未改动任何东西)

- [x] 确认能用 `orb` 连接虚拟机(`ubuntu`,192.168.139.41)✅
- [x] 虚拟机 herdr 版本 0.9.3,与 Mac 一致,server 在运行 ✅
- [x] 找到 `fnx_dv`、`fnx_sw` 的位置(`~/.forenyx/fnx_dv/bin/fnx_dv`、`~/.forenyx/fnx_sw/bin/fnx_sw`,不在 PATH)✅
- [x] 确认 `python3`(3.10.12)与 `flock` 可用;`jq`、`node`、`pi` 不在 PATH ✅
- [x] 确认现有会话 `jhx`(1 个 workspace、2 个 tab、3 个 pane,当前无识别到的 agent),**未触碰** ✅

### 1.2 隔离探测(待你确认后执行,使用独立命名 session `a2a_probe`)

- [x] 创建命名 session,确认与现有 `jhx` 会话互不可见 ✅(`herdr session list` 两个会话并存;命名会话需要 `herdr session attach` 启动,我用后台 tmux `a2a_probe_tty` 托着它)
- [x] 探测 1:跨 tab 的 `agent prompt` 是否可用 ✅(从 pane 之外的 shell 向非聚焦 tab `sw` 里的 `w1:p3` 发 prompt 成功。严格的"从 dv 的 pane 里发给 sw 的 pane"没有单独测,但走的是同一个按 pane ID 寻址的接口)
- [x] 探测 2:`--env` 注入的变量能否被 pane 内子进程继承 ✅(`tab create --env` 注入的 `A2A_ROLE` / `A2A_IP` 被 pane 里的 shell 读到;`HERDR_PANE_ID`、`HERDR_TAB_ID`、`HERDR_WORKSPACE_ID`、`HERDR_ENV=1` 也都在)
- [x] 探测 3:`done` 的行为 ✅ **结论:`done` 不会自己变回 `idle`**(20 秒后仍是 `done`)。`agent wait --until idle` 在 `done` 状态下**超时失败**(rc 1);`--until done` 或 `--until idle --until done` 立刻返回。对 `done` 的 agent 发 prompt 能成功。聚焦(`agent focus`)之后 `done` 变 `idle`。所以**必须把 `done` 视为 READY**,等待时必须同时带 `--until idle --until done`
- [x] 探测 4:同一目标并发投递 ✅ **结论:不会文字交织**。两条 prompt 同时发给 `working` 的 agent,herdr 都返回成功,pi 把它们当作两轮依次处理,但**顺序不确定**(先发的 A 反而排在 B 之后)。所以 broker 的串行投递不是为了防止乱码,而是为了**保证顺序、避免在对方忙时发送**
- [ ] 探测 5:`blocked` 时 `agent prompt` 是否真的拒绝并返回 `agent_blocked`(**未测**:需要让 agent 停在审批界面,pi 默认没有审批,没找到触发办法)
- [x] 记录 herdr 命令的真实参数 ✅(`agent` / `pane` / `tab` / `workspace` / `session` 已读取;`api schema` 未读)
- [x] 清理:停止并删除命名会话、关闭 tmux、删除临时文件和符号链接 ✅(默认会话未触碰)

### 1.3 涉及 `fnx_dv` / `fnx_sw` 的探测(需你先同意,因为可能调用模型接口)

- [x] 探测 6:`fnx_dv` 能否被 herdr 识别为 `pi` ✅ **结论:不能自动识别**。前台进程名是 `forenyx-cli`(`~/.forenyx/fnx_dv/libexec/forenyx-cli`),`agent list` 为空。用 `herdr pane report-agent <pane> --source <id> --agent pi --state idle --seq N` 手动上报后,被识别为 `pi`、状态 `idle`,可按 pane ID 寻址。需要让 fnx 自己上报,见下方「新增事项」
- [x] 探测 7:pi 的 `bash` 能否拿到 `A2A_ROLE` / `A2A_IP` / `HERDR_PANE_ID` ✅(用 `!` 前缀在 fnx_dv 里执行,读到 `ROLE=dv IP=uart PANE=w1:p2 ENV=1`;注意这是用户侧 `!` 命令,不是模型调用的 bash 工具,模型侧仍需再确认)
- [ ] 探测 8:pi 二开版的 `bash` 工具是否会弹人工批准
- [ ] 确认 `fnx-sw` 的 pi 版本与 `pi-custom` 一致(我只读了 `pi-custom`)
- [ ] 把 §10.3 中 6 条未验证事实逐条标为"已验证"或"不成立,已改"

### 1.4 探测中发现的新事项

**关键结论:herdr 靠前台进程的名字认 agent,上报(`report-agent`)不能替代。**

- `fnx_dv` / `fnx_sw` 的前台进程叫 `forenyx-cli`(启动脚本最后一行 `exec "$LIBEXEC_DIR/forenyx-cli"`),herdr 认不出。
- 用 `pane report-agent` 上报后,`agent list` / `agent get` **能识别**,显示 `pi / idle`。但我对 `w1:p2`(`fnx_dv`,前台进程 `forenyx-cli`)执行 `agent prompt`,三次都报 `agent_not_ready: agent w1:p2 is no longer the pane foreground process`,且消息没有写入。三次的上报顺序不同(先上报后启动、启动后上报、释放后换来源重报),结果一致。**结论仅限这一条件;用户反馈上报后可被识别(与我一致),但上报后的 `agent prompt` 结果待用户确认。**
- **有效办法一(单个智能体):让进程名变成 `pi`。** 建名为 `pi` 的符号链接指向 `forenyx-cli`,带启动脚本的环境变量运行。herdr 自动识别,`agent prompt` 成功。**缺点**:每个智能体的程序、数据目录、环境变量都不同(`fnx_dv` 与 `fnx_sw` 的程序校验值不同),要为每个智能体各建一个链接并复制它的环境变量逻辑。
- **有效办法二(通用,推荐):覆盖启动脚本的 `exec`,让最终进程的 `argv[0]` 是 `pi`。** 不建链接、不改 fnx 安装包、不复制环境变量逻辑:

  ```bash
  bash -c 'exec(){ builtin exec -a pi "$@"; }; source "<该智能体的启动脚本路径>"'
  ```

  验证(`fnx_dv` v0.4.7、`fnx_sw` v0.0.8):herdr 都自动识别为 `pi / idle`;横幅显示各自身份(各自脚本的环境变量都生效);`agent prompt` 都成功,状态 `working → done`;`A2A_ROLE` / `A2A_IP` / `HERDR_PANE_ID` 都能传入。依据:herdr 按 `argv[0]` 识别(`exec -a pi sleep 600` 即被识别为 `pi`)。
  **前提**:启动脚本只有一处 `exec`,且不依赖 `$0`/`BASH_SOURCE`(`fnx_dv`、`fnx_sw` 的脚本第 227 行为唯一一处 `exec "$LIBEXEC_DIR/forenyx-cli" "$@"`)。另外三个智能体(Spec/架构/RTL 对应的 fnx_xx)不在这台虚拟机上,**未验证**。

待办:

- [x] 确定正式的启动方式 ✅ 采用办法二(覆盖 `exec`),拓扑配置里每个智能体只记录**启动脚本路径**,框架不关心各智能体安装目录和环境变量差异
- [ ] 框架启动前做预检:启动脚本只有一处 `exec`、不使用 `$0`/`BASH_SOURCE`;启动后用 `pane process-info` 与 `agent list` 确认已被识别为 `pi`,否则报错
- [ ] 在另外三个智能体(Spec/架构/RTL)的机器上验证办法二的前提成立
- [x] 办法二已对 `fnx_dv`、`fnx_sw` 各验证启动、识别、`agent prompt`、环境变量 ✅(授权检查等深层功能仍只测了一句对话)
- [ ] `agent start --kind pi` 会执行名为 `pi` 的命令;若上面链接放在 PATH 里,可以直接用 `agent start`,框架就无需自己拼 `pane run`
- [ ] 虚拟机里 pane 的默认工作目录是 Mac 的共享路径,建 pane 时要用 `--cwd` 明确指定
- [ ] `blocked` 与模型侧 bash 工具的批准弹窗仍未验证

### 1.5 其他已完成的验证

- [x] 用户自己按 `06-manual-walkthrough.md` 手动走通覆盖 `exec` 的启动流程 ✅(基本没问题)
- [x] `agent rename` 实测 ✅:改名成功;按名字 `get` / `prompt` / `wait` / `read` 都成功;重名报 `agent_name_taken`,非法名报 `invalid_agent_name`;agent 退出后名字失效,重启后不自动恢复,需重新改名
- [x] 已确认 `agent prompt` 不携带发送方信息,发送方身份只能由框架记录在审计日志里(D11:接收方先不验证来源)✅
- [x] 设计文档 `04` 已按实测结果更新(§2.1.1、§2.3、§10.3)✅
- [ ] 另外三个智能体(Spec / 架构 / RTL)的启动脚本验证
- [ ] `blocked` 状态与模型侧 bash 批准弹窗验证

## 阶段 2:`HerdrClient`(已完成,代码在 `herdr/a2a/`)

只封装 herdr CLI,暂不做业务路由。用 `subprocess`,解析 JSON,**不依赖 `jq`**。依赖仅标准库,兼容 Python 3.9(Mac)和 3.10(虚拟机)。

- [x] 创建 / 删除 / 重命名:workspace、tab、pane、agent(`agent rename`,启动后调用,重启后需重设)✅
- [x] 创建时返回 `Created(workspace_id, tab_id, pane_id)`,不事后猜 ID ✅
- [x] `agent start` 之外的启动方式:`launcher.build_launch_command`(覆盖 `exec`)+ `preflight_launcher` 预检 ✅
- [x] `agent get / list / read / wait / prompt / send-keys / focus / explain` ✅
- [x] `pane run / send-text / send-keys / read / wait-output / process-info` ✅
- [x] `wait_for_agent_detected`(识别之后才能改名)、`find_agent` ✅
- [x] session 辅助:`session_list / session_stop / session_delete` ✅
- [x] 统一错误处理:按 herdr 的 `error.code` 映射成异常类;退出码 2 = 用法错误;子进程超时;找不到 herdr ✅
- [x] 单元测试(假 runner,73 个)✅
- [x] 在虚拟机里的集成测试(自建一次性命名会话,结束自动清理):布局、环境变量注入、改名与按名字寻址、重名与非法名、超时、`argv[0]` 识别等 ✅
- [x] 真实 `fnx_dv` / `fnx_sw` 启动测试(预检、覆盖 `exec` 启动、被识别为 `pi`、身份变量、改名),不向模型发提示词 ✅

测试中发现的新事实(已写进 `herdr/a2a/README.md`):

- tab / pane 编号不一定是数字(例如 `w1:tC`)
- 刚被识别的一瞬间状态可能是 `unknown`,"识别到了"不等于"可以投递"
- `pane send-text` 再 `send-keys enter` 不是原子操作,TUI 需要约 0.5 秒间隔;发消息用 `agent prompt`
- 虚拟机与 Mac 共享目录,刚改完文件立刻在虚拟机里运行,偶尔读到同步中的残缺文件

仍未验证:

- [ ] `blocked` 状态下 `agent prompt` 的真实行为(代码按文档映射,未实测)
- [ ] 文本以 `-` 开头的 `agent prompt` 是否被 herdr 当成选项

## 阶段 3:拓扑、身份、注册表

- [ ] `topology.yaml` 的读取与校验(角色、IP、有向边、模板)
- [ ] 业务 ID:`agent_id` = `{角色码}_{ip}`,不带项目前缀;名字长度 ≤ 32 的检查
- [ ] 注册表:业务 ID → 当前 workspace / tab / pane / agent_name / 状态,持久化
- [ ] 身份解析:`A2A_ROLE` + `A2A_IP` + `HERDR_PANE_ID` 与注册表交叉核对
- [ ] pane 重建后只更新注册表,业务 ID 不变

## 阶段 4:Router 与固定模板

- [ ] 鉴权六步(§3.1):身份、发送方已注册、边存在且方向对、目标已注册、模板字段合法、入队
- [ ] 同 IP 铁律硬编码
- [ ] 模板渲染,`{ip}` 由框架填入
- [ ] 拓扑与模板的动态增删改(`add_edge` / `remove_edge` / `set_template`)
- [ ] **反向用例全部被拒绝并写审计日志**:
  - [ ] 同角色跨 IP(`dv_uart` → `sw_gpio`)
  - [ ] 未配置的边
  - [ ] 方向反了(`sw` → `dv`)
  - [ ] 目标不存在或已 `purge`
  - [ ] 伪造 `A2A_IP`,而 `HERDR_PANE_ID` 对应别的节点
  - [ ] 模板字段不合法
  - [ ] 目标 `blocked`:立即得到 `TARGET_BLOCKED`,且不向其写入任何输入

## 阶段 5:Broker

- [ ] 单实例(`flock` 保证只有一个 broker)
- [ ] 持久队列(`spool/{dst}/{msg_id}.json`),重启后恢复
- [ ] 每目标一个串行 worker,FIFO
- [ ] READY 判定:`idle` / `done` 可投递;`working` 等待;`blocked` 立即失败;`unknown` 等到超时后失败
- [ ] 投递后用 `agent wait --until working` 确认
- [ ] 超时、重试、去重(`msg_id`)
- [ ] 消息状态机与审计日志(§4.6)
- [ ] `a2a send <edge_id>` 立即返回 `msg_id`;`a2a status <msg_id>` 可查
- [ ] 并发测试:同一目标同时收到多条,不交织

## 阶段 6:pi 侧接入

- [ ] 在 `AGENTS.md` 中写明:什么时候用 `a2a send`、不要直接执行 `herdr` 写入类命令
- [ ] pi 扩展:`pi.registerTool()` 注册 `a2a_send`,只接受边 id
- [ ] pi 扩展:`tool_call` 钩子,拦截 `herdr agent prompt` / `herdr pane send-*` / `herdr pane run`
- [ ] 让 `fnx_sw` 收到固定句式后真的开始驱动与 HAL 开发(提示词或技能)
- [ ] 让 `fnx_dv` 在 UVM 验证完成时调用 `a2a send dv_done`

## 阶段 7:全链路

- [ ] 跑通 `dv_uart` → `sw_uart`
- [ ] 回退通信(如验证 → RTL)按拓扑配置跑通
- [ ] 审计日志里能看到完整一次链路

## 阶段 8:规模化

- [ ] 批量创建 5 个 tab
- [ ] 按 IP 动态增删 pane / agent(`stop` / `close` / `purge` / `restore`)
- [ ] registry 自动恢复
- [ ] 多 IP 并发
- [ ] 150 个 agent 的资源压测
- [ ] 视情况引入 socket 事件订阅(已决定:CLI 先行,socket 留到规模化;动机是 150 个目标各用一个 `agent wait` 子进程过重;socket 尚未实测)
- [ ] 全局 Monitor 与 GUI(drawio 中的后续阶段)

---

## 仍待你决定的事项

| # | 问题 | 当前默认 |
| --- | --- | --- |
| E1 | 同一对角色之间是否需要多种句式 | 允许多条边,用不同 `id` |
| E4 | 防绕过的强度(L1~L4) | 先 L1 + L2,看审计日志再决定 |
| E5 | 发送方身份防冒充:环境变量 + `HERDR_PANE_ID` 交叉核对 | 接受则按设计实现 |
| E6 | 模板里除 `{ip}` 外是否还要其他字段 | 默认只有 `{ip}` |
| E8 | 发送失败(如目标 blocked)后是否自动通知人 | 提供 `a2a status`;是否自动通知待定 |
| — | 阶段 1.2 是否同意在虚拟机里开命名 session `a2a_probe` | 待你确认 |
| — | 阶段 1.3 是否允许启动 `fnx_dv` / `fnx_sw` | 待你确认(可能调用模型接口) |
