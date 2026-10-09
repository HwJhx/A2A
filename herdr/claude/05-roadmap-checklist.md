# 路线图与进度清单

> 规则:**完成一项,在行尾打 ✅**。未完成的行尾留空。只有真正做过并确认的才打 ✅。
> 最后更新:2026-10-09
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

## 阶段 3:拓扑、身份、注册表(已完成,代码在 `herdr/a2a/`)

拓扑与注册表移植自 `herdr/a2a_codex`(该目录未被改动);保留 `project_id`;动态拓扑一并实现。

- [x] `topology.yaml` 的读取与校验(角色、IP、有向边、模板)✅ 模板只允许 `{ip}`;拒绝属性访问、格式说明、空占位符
- [x] 业务 ID:`agent_id` = `{角色码}_{ip}`,不带项目前缀;名字长度 ≤ 32 的检查 ✅
- [x] 保留 `project_id`(D13)✅ 拓扑必填;`A2A_PROJECT_ID` 注入;不进入 `agent_id`
- [x] 注册表:业务 ID → 当前 workspace / tab / pane / agent_name / 状态,持久化 ✅ JSON + 跨进程文件锁 + 原子替换
- [x] 身份解析收成一个入口 `resolve_sender` ✅ 环境变量 + `HERDR_PANE_ID` 与注册表交叉核对;要求 running;用拓扑重新校验节点还在;session 必须显式传入
- [x] pane 重建后只更新注册表,业务 ID 不变 ✅ `update_runtime`
- [x] 状态目录解析 ✅ `A2A_STATE_DIR` > `XDG_STATE_HOME/a2a` > `~/.local/state/a2a`;拒绝相对路径
- [x] **动态拓扑**(D14)✅ `TopologyStore`:热加载(坏文件保留上一份合法拓扑)、增删 IP / 角色 / 边、改模板、跨进程文件锁、写前备份、修订号
- [x] 身份失败用例全覆盖 ✅ 缺各个环境变量、project 不符、伪造 role / ip、他人的 pane、未登记、已注销、重建后旧 pane 号、错误会话、非 running、节点已不在拓扑
- [x] 跨进程并发与崩溃验证 ✅ 6 个进程并发改拓扑、8 个进程并发登记注册表不丢更新;`os.replace` 前强杀进程,原文件完好
- [x] 测试:161 个全部通过 ✅(Mac,Python 3.9.6;虚拟机,Python 3.10 + PyYAML 5.4.1,含真实 herdr 与真实 fnx 的集成测试)

阶段 4 要接上的约定:

- Router 每次发送都用 `TopologyStore.current()` 取拓扑,并把 `revision` 写进审计日志
- Router 必须显式传入 session 调 `resolve_sender`
- 创建 tab / pane 时的 `env=` 用 `identity_env(project_id, role, ip)` 生成,不要手拼
- `lifecycle` 删除 IP / 角色前要先清除对应 agent(`TopologyStore` 不检查)

## 阶段 4:Router 与固定模板(已完成,代码在 `herdr/a2a/`)

`router.py` 负责鉴权、拓扑校验、固定模板渲染、入队;**不碰 herdr**。同时新增 `messages.py`、`spool.py`(持久队列)、`audit.py`(审计日志)。

- [x] 鉴权(§3.1):身份、边存在、方向正确、目标节点在拓扑里、目标已登记且 running、模板渲染 ✅
- [x] 同 IP 铁律 ✅ 接口里没有目标 IP / 目标名字 / 自由文本参数;目标 = (边的 to 角色, 发送方自己的 IP)
- [x] 模板渲染,`{ip}` 由框架填入 ✅ 并检查非空、长度上限、控制字符
- [x] 拓扑与模板的动态变化即时生效 ✅ 每次发送取 `TopologyStore.current()`;审计里记录拓扑修订号
- [x] 持久队列 ✅ 原子写入;msg_id 按时间排序,同一目标按入队顺序(FIFO);终态消息归档到 `done/`
- [x] 审计日志 ✅ 追加写 JSON Lines、跨进程文件锁、文件权限 0600
- [x] **反向用例全部被拒绝并写审计日志** ✅(每次拒绝恰好一条审计记录,且不入队):
  - [x] 同角色跨 IP(`dv_uart` → `sw_gpio`)✅ 按构造不可能:目标 IP 恒等于发送方 IP;`sw_uart` 缺失时消息不会落到 `sw_gpio`
  - [x] 未配置的边 ✅ `unknown_edge`
  - [x] 方向反了(`sw` → `dv`)✅ `wrong_direction`;反向边配置后才通,删除后立即失效
  - [x] 目标不存在或已 `purge` ✅ `target_missing`;目标非 running → `target_not_running`
  - [x] 伪造 `A2A_IP` / `A2A_ROLE` / `A2A_PROJECT_ID`,或使用他人的 pane ✅ `identity`
  - [x] 缺环境变量、pane 未登记 / 已注销 / 重建后旧号、别的会话冒充、发送方非 running、节点已不在拓扑 ✅ `identity`
  - [x] 模板内容不合法 ✅ 拓扑加载时拒绝非法字段;渲染后超长 / 含控制字符 → `bad_message`
  - [ ] 目标 `blocked`:立即得到 `TARGET_BLOCKED`,且不向其写入任何输入 —— **属于阶段 5**(要向 herdr 查实时状态,Router 不查)
- [x] 多进程并发发送 ✅ 6 个进程并发发送 30 条,全部入队、ID 唯一、审计日志无交织
- [x] 测试:214 个全部通过 ✅(Mac,Python 3.9.6;虚拟机,Python 3.10,含真实 herdr 与真实 fnx 的集成测试)

### 阶段 4 补强:复核(对照 `herdr/a2a_codex`)后修复

复核意见见 `07-review-of-a2a_codex.md`。对我自己的实现用同样的探针测出 6 个缺陷(P1~P7),已全部修复并有测试:

- [x] **P2 队列**:终态归档两步之间崩溃会留下 pending / done 两份,broker 重启后会重复投递 ✅ `pending()` 读取时过滤已有 done 记录的消息;`recover()` 清理残留与临时文件(Broker 启动时调用)
- [x] **P3 队列**:`msg_id` / 目标 ID 无格式校验,可路径穿越 ✅ 严格正则校验,非法一律 `InvalidIdError`
- [x] **P6 队列**:`update` 接受未知状态名 ✅ 未知状态 → `SpoolError`
- [x] **P7 队列**:终态可回退为非终态 ✅ 状态只能沿合法迁移表前进;终态不可再修改任何字段;新消息只能以 `QUEUED` 入队
- [x] **P1 审计**:崩溃留下半行后,下一条记录被拼进坏行丢失 ✅ 追加前把残片隔离到 `audit.jsonl.corrupt`,补记 `AUDIT_REPAIRED`;`read()` 仍容忍中间坏行且不修改文件;写入循环处理短写
- [x] **P4 Router**:不检查目标登记的会话 ✅ 目标会话必须等于发送方会话,否则 `target_missing`
- [x] 状态迁移表写入协议文档 `08-protocol.md` ✅ 并新增 `DELIVERY_UNCERTAIN`;`tests/test_protocol.py` 逐行对照文档与代码,不一致即失败
- [x] 测试:246 个全部通过 ✅(Mac,Python 3.9.6,17 个集成测试按设计跳过;虚拟机,Python 3.10,含真实 herdr 与真实 fnx)

### 协议版本 2 定稿(与 Codex 多轮复核)

- [x] `08-protocol.md` 修订为版本 2 并经三次补充决议 ✅ Codex 复审结论"可以定稿"
  - `DELIVERY_UNCERTAIN` 默认不自动重发,没有时间出口,只能由操作员裁定或经验证的机制离开;删除 `DISPATCHING → WAITING_TARGET`;新增 `RETRYING → TARGET_BLOCKED / TARGET_MISSING`
  - herdr 错误分类表(§5):只有能证明未提交的错误才能重试;`agent_blocked` / `agent_not_ready` / `server_not_running` / 调用后 `agent_not_found` 待实测
  - 新增 §7 目标队列:队列头模型、确定失败也暂停、操作员动作(裁定已送达 / 重试 / 放弃并继续)、`queue_seq`、裁定幂等与作废
- [x] `messages.py` 迁移表对齐版本 2 ✅ `test_code_table_equals_the_document` 通过
- [x] 测试:279 个全部通过 ✅(Mac,Python 3.9.6,17 个跳过;虚拟机,Python 3.10,16 个跳过,未开启集成测试开关)
- [ ] `a2a_codex` 的对齐由 Codex 自行完成(`a2a` 已对齐不代表 `a2a_codex` 已对齐)

另外:文件权限经查无问题(状态文件均为 0600)。以下是 Codex 复核意见里**我认可、但尚未处理**的项,留给阶段 5 或之后:

- [ ] 客户端缺口:`rename_agent` 不能清除名字已有(`agent_rename(None)`);仍需补 `agent_prompt` 的客户端超时与 `agent_prompt_stalled` 一并映射为"结果不确定"(`HerdrPromptStalled` 已有,Broker 要据此进入 `DELIVERY_UNCERTAIN`)
- [ ] `done/` 目录的保留期、归档与磁盘告警策略(不做无策略删除)
- [ ] `Spool.recover()` 放在 Broker 启动时调用(已实现方法,待 Broker 接上)

新发现(已写进 04 号文档):

- 命名会话里的 pane 带有 `HERDR_SESSION`(会话名)和 `HERDR_SOCKET_PATH`,`a2a send` 可直接据此得到会话名,不需要额外注入。**默认会话里的取值未测**。

阶段 5 要接上的约定:

- **启动顺序**:按 `08-protocol.md` §8:`Spool.recover()` → 停在 `DISPATCHING` 的消息迁到 `DELIVERY_UNCERTAIN` → 对账未生效的操作员裁定 → 恢复槽位放行记录 → 从各目标队列头继续
- broker 从 `Spool.pending_targets()` / `pending(dst)` 取消息,**按 `queue_seq` 只处理队列头**(现在按 msg_id,需改);状态更新用 `Spool.update`(只能沿合法迁移表前进,到终态自动归档;**归档不等于放行**,槽位放行要单独持久记录)
- 调用 herdr 后结果不明(含 `agent_prompt_stalled`、客户端超时、未经验证语义的错误码)→ `DELIVERY_UNCERTAIN`,**默认不自动重发,也不自动转 `FAILED`**,等操作员裁定
- broker **持有 Spool 锁时不能等待 herdr**:锁只覆盖短的文件操作
- broker 的每次状态变化都要写审计日志,并带上 `msg_id`
- 投递时才查 herdr 实时状态;`blocked` 立即 `TARGET_BLOCKED`
- 去重(§4.7)在 broker 里做
- 还没写 `a2a send` / `a2a status` 命令行(属于阶段 5 的交付物)

## 阶段 5:Broker

- [ ] 单实例(`flock` 保证只有一个 broker)
- [ ] 持久队列(`spool/{dst}/{msg_id}.json`),重启后恢复
- [ ] 每目标一个串行 worker,FIFO
- [ ] READY 判定:`idle` / `done` 可投递;`working` 等待;`blocked` 立即失败;`unknown` 等到超时后失败
- [ ] 投递后用 `agent wait --until working` 确认
- [x] **Broker 实现前**:实测 4 个错误码的"未提交"语义 ✅ herdr 0.9.3 上四者均未写入,已写回 08 §5;新发现 `agent_prompt_failed`(写入中途 pane 被关闭)返回时**已写入**,走 `DELIVERY_UNCERTAIN`(`09` 号文档 §7)
- [ ] `HerdrClient` 的错误码映射补上 `agent_prompt_failed`(目前按未知错误码处理,结果正确但不显式)
- [ ] 升级 herdr 后重跑 `review-probes/probe_herdr_errors.py`
- [ ] `queue_seq`(每目标持久单调、原子分配;重试继承原值),改 `Router` / `Spool`
- [ ] 队列头调度:确定失败与不确定态都暂停该目标;槽位放行记录持久化
- [ ] 操作员命令:裁定已送达 / 重试 / 放弃并继续 / 作废裁定 / 恢复投递(命令形式与 `actor` 来源待设计,08 §10)
- [ ] 裁定对账:`ruling_id` 幂等补做、`RULING_APPLIED`、裁定记录不可读时 fail-closed
- [ ] 告警:不确定态 1 小时提醒、24 小时升级(暂定)
- [ ] 观察窗口(暂定 30 秒)、状态查询退避与上限(暂定 5 次 / 300 秒),实测后定
- [ ] 超时、去重(`msg_id`)
- [ ] 消息状态机与审计日志(§4.6)
- [ ] `a2a send <edge_id>` 立即返回 `msg_id`;`a2a status <msg_id>` 可查
- [ ] 并发测试:同一目标同时收到多条,不交织
- [ ] 行为测试:队列排序、暂停与放行、重试继承序号、恢复幂等;故障注入覆盖作废前后各副作用已持久化的崩溃点(Codex 建议)

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
