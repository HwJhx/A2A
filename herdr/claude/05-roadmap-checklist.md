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
- [x] 测试:246 个全部通过 ✅(Mac,Python 3.9.6,17 个集成测试按设计跳过;虚拟机,Python 3.10)。**更正(2026-10-09)**:按会话记录核对,这一轮没有打开集成开关,不含真实 herdr / fnx。真实 herdr 集成测试的实际记录:2026-10-08(阶段 2/3)、2026-10-09 上午(阶段 4)、2026-10-09 阶段 5a 之后(16 个:herdr 14 + 真实 fnx 启动 2,全部通过)

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

## 阶段 5:Broker(子阶段经与 Codex 对照、用户确认,2026-10-09)

依据 `08-protocol.md` v2。用户确认的决定:采用"方案 C"(生命周期留在阶段 5 末尾的 5f,拓扑命令放 5d);**不加**"RETRYING 次数上限转 FAILED",由总等待时限转 `TIMEOUT` 兜底;操作员裁定的 `actor` v1 取执行命令的系统用户名,只作审计署名,不作认证。

前置(已完成):

- [x] 实测 4 个错误码的"未提交"语义 ✅ herdr 0.9.3 上四者均未写入,已写回 08 §5;新发现 `agent_prompt_failed` 返回时**已写入**,走 `DELIVERY_UNCERTAIN`(`09` 号文档 §7)
- [x] 协议规则 10 的排序键改为 `queue_seq` ✅ 加一致性测试(Codex 指出)

### 5.0 准备

- [x] `HerdrClient` 显式映射 `agent_prompt_failed`(结果不确定)✅ 新增 `HerdrPromptFailed`;`agent_not_found` 也显式映射
- [x] 按 08 §5 的 prompt 返回分类函数 ✅ `policy.classify_prompt_result`;测试逐行解析 08 §5 表格对照;herdr 版本不在实测范围(`VERIFIED_HERDR_VERSIONS`,当前 0.9.3)时四个实测错误码退回"不确定";新增 `herdr_version()`
- [x] Broker 配置与暂定默认值 ✅ `policy.BrokerConfig`(未知配置项报错)与 `backoff_delays`;等待 READY 总超时 300 秒;观察窗口 30 秒;状态查询退避 1 秒起倍增、单次 ≤30 秒、连续失败 5 次(均可配置,实测后定)

测试:296 个通过(Mac 17 跳过;VM 16 跳过,VM 上 herdr 0.9.3 判定为已实测版本)。

### 5a 队列基础(不调用 herdr)

- [x] `queue_seq`:每目标持久单调、锁内原子分配 ✅ 由 `Spool.enqueue` 分配(调用方不能预设),Router 回执与审计带上 `queue_seq`;`Spool.head()` 求队列头
- [x] 槽位放行记录持久化 ✅ `<spool>/queues/<dst>.json`;到终态时先记未放行槽位再归档;`Spool.release()` 只放行队列头、幂等、记 `ruling_id`
- [x] 受信任的重试入队路径 ✅ `Spool.enqueue_retry()`:继承授权与 `queue_seq`,按新 `msg_id` 幂等;拒绝非终态、已送达、已放行、槽位已有未终结消息、非最后一次结果
- [x] 测试 ✅ `tests/test_queue.py` 25 个:时钟回拨下仍按序、跨重启不复用、5 进程并发 50 条序号连续、队列文件损坏 fail-closed、三步归档各崩溃点、旧消息兼容;全部 321 个通过(Mac / VM)

真实 herdr 集成测试(5a 之后补跑,`A2A_INTEGRATION=1`、`A2A_INTEGRATION_FNX=1`):16 个全部通过;临时会话已清理。**以后每个子阶段完成时都跑一次,汇报时与单元测试分开写。**

### 5b 单条消息投递引擎(先用假 HerdrClient)

- [x] 进入 `DISPATCHING` 前复核目标 → 写前标记 → `agent prompt` → 分类 → 观察窗口 → `DELIVERED` / `DELIVERY_UNCERTAIN` ✅ `delivery.DeliveryEngine`。按登记的 agent 名字寻址(agent 退出后名字即失效,不会发到被复用的 pane),并核对 pane 与登记一致;观察窗口用 `agent prompt --wait --until working --until blocked`,由 herdr 观察"这次提交之后"的状态(5 秒内没看到会返回 `agent_prompt_stalled` → 不确定)
- [x] 覆盖 08 §3 每一条迁移;持有 Spool 锁时不等待 herdr ✅ `tests/test_delivery.py` 34 个(假 HerdrClient + 假时钟):除操作员裁定外的全部迁移、查询失败退避 1/2/4/8 秒后第 5 次判 `TIMEOUT`、`RETRYING` 由总时限兜底转 `TIMEOUT`、herdr 版本未实测时退回不确定;每个用例校验审计里的状态路径都在迁移表内
- [x] 在 VM 用假 agent 小规模验证,不调用模型 ✅ `tests/test_integration_delivery.py` 5 个**真实 herdr** 用例全部通过:空闲目标送达且文字确实写入;忙碌目标等其空闲后才发;blocked 目标立即失败且未写入;目标不表现出开始处理 → 不确定,且日志证明文字其实已写入;agent 名字不存在 → `TARGET_MISSING`。假 agent `tests/fake_agent.py` 用 `report-agent` 模拟 working/idle

测试(5b 完成时):单元测试 360 个(Mac 22 跳过,VM 21 跳过,跳过的都是集成测试);**真实 herdr 集成测试 21 个全部通过**(herdr 14 + 投递引擎 5 + 真实 fnx 启动 2),临时会话已清理。

限制(v1):等待总时限从本次投递开始计时,broker 重启后重新计时。

### 5c Broker 进程

- [x] 单实例(`flock`);每目标一个串行 worker,只处理队列头;不确定态与确定失败暂停该目标 ✅ `broker.Broker`。补充:herdr 服务不在时整体暂停投递(不消耗查询失败次数,避免服务重启导致整批 `TIMEOUT`);一个状态目录只服务一个会话,别的会话的消息原样不动;broker 内部错误时该目标冷却 30 秒再试
- [x] 启动恢复按 08 §8 五步;审计 `QUEUE_PAUSED` / `QUEUE_RELEASED` ✅ 第 3 步(对账裁定)留空,5d 实现裁定时补上;损坏的队列文件只让该目标 fail-closed;读不出的待投递消息 → 全局停止(`dispatch.halted` + `DISPATCH_HALTED`)
- [x] 告警 v1 ✅ 审计 `ALERT`(含裁定命令提示和被暂停的消息数)+ 日志(前台运行时在终端,服务运行时在 journalctl);只告警,不改状态
- [x] 前台命令 `a2a broker run` ✅ `cli.py`(`--once` 调试用),`pyproject` 注册 `a2a` 命令;systemd 用户服务模板 `config/a2a-broker@.service`(实例名 = 会话名)
- [x] 故障注入:`DISPATCHING` 中途 kill -9 ✅ 两处:单元测试(假 herdr 可执行文件,prompt 期间 SIGKILL)和**真实 herdr**(假 agent 不响应、herdr 等待期间 SIGKILL);重启后都转 `DELIVERY_UNCERTAIN`、文字只写入一次、后一条不被投递。**裁定各副作用之间**的 kill -9 留到 5d(裁定在 5d 实现)

测试(5c 完成时):单元测试 376 个(Mac 24 跳过,VM 23 跳过,跳过的都是集成测试);**真实 herdr 集成测试 23 个全部通过**(herdr 14、投递引擎 5、broker 子进程 2、真实 fnx 启动 2):broker 按队列顺序写入 3 条、blocked 目标只暂停自己、kill -9 后不重发;临时会话与进程已清理。

### 5d 命令行

- [x] `a2a send <edge_id>`、`a2a status <msg_id>`、`a2a queue [<dst>]` ✅ 输出 JSON;退出码 0/2/3/4/5/6;`status` 对被拒绝的发送从审计里查;`queue` 显示每个目标的队列头、暂停原因、积压数、可用的裁定命令,以及全局停止状态和未生效/读不出的裁定
- [x] 操作员:`a2a resolve <msg_id> delivered | retry | abandon --reason`、`a2a ruling void`、`a2a dispatch resume` ✅ `rulings.py`:只能裁定队列头;先写 `OPERATOR_RULING` 再生效;每个效果带 `ruling_id` 且幂等;全部生效后写 `RULING_APPLIED`;broker 启动时补做(08 §8 第 3 步已接上);读不出的裁定(审计中间的坏行或被隔离的残片)→ 全局停止,作废后才能 `dispatch resume`;补做时状态与裁定矛盾 → 全局停止。**补充**:消息新增 `ruling_id` 字段,与状态迁移同一次落盘——否则"重试已生效、重发后又不确定"时补做会再重试一次(测试中发现并修正)
- [x] `a2a topology show / add-ip / remove-ip / add-edge / remove-edge / set-template` ✅ 每次修改写 `TOPOLOGY_CHANGED` 审计(含操作者与前后修订号)
- [x] 回归:消息入队后删边,旧消息照常投递,新消息被拒绝(`unknown_edge`)✅

测试(5d 完成时):单元测试 395 个(Mac 24 跳过,VM 23 跳过,跳过的都是集成测试),其中裁定与命令行 21 个,覆盖各崩溃点的补做(用进程内模拟崩溃,不是真实 kill -9);**真实 herdr 集成测试 23 个全部通过**。临时会话与进程已清理。

### 5e 端到端(假 agent)

- [x] VM 里用假 agent 跑通 send → broker → 投递 → status,含 broker 重启 ✅ `tests/test_integration_e2e.py`(**真实 herdr**):真实 shell pane(注入 dv_uart 身份)里执行 `a2a send dv_done` → Router 渲染"uart已完成UVM验证,请开发驱动。"入队 → 常驻 broker 投递 → 假 agent sw_uart 收到;broker 停止期间发出的 2 条在重启后按序补投;pane 里 `a2a status` 查到 `DELIVERED`;消息的会话名确实来自 herdr 注入的 `HERDR_SESSION`;另一个 shell pane 冒充 sw_uart 发送被拒(`identity`,退出码 4)
- [x] 并发:同一目标同时收到多条,按 `queue_seq` 串行、不交织 ✅ 单元级:5 进程并发入队 50 条序号连续(5a)、多线程 broker 两个目标各 3 条按序(5c);真实 herdr 上 broker 按序写入 3 条(5c)。**未做**:多个真实 pane 同时 `a2a send` 的压测(留到规模化阶段)
- [x] 行为测试:队列排序、暂停与放行、重试继承序号、恢复幂等 ✅ 分布在 `test_queue.py`(5a)、`test_broker.py`(5c)、`test_rulings_cli.py`(5d)

测试(5e 完成时):单元测试 397 个(Mac 26 跳过,VM 25 跳过,跳过的都是集成测试);**真实 herdr 集成测试 25 个全部通过**(herdr 14、投递引擎 5、broker 子进程 2、端到端 2、真实 fnx 启动 2)。临时会话与进程已清理;集成测试结束时回收 broker 子进程。

### 5f 生命周期管理

- [x] 按 IP 启动 agent pane:注入身份环境变量、登记注册表 ✅ `lifecycle.Lifecycle.spawn` / `a2a agent spawn <role> <ip>`:布局 workspace(拓扑 workspace_label)→ 角色 tab(角色 label)→ 每 IP 一个 pane(同角色在同一 tab 里拆分);预检启动脚本 → 覆盖 exec 启动 → 等识别 → 等 READY → 最后 `agent rename` 为 agent_id → 登记;启动失败关闭新建的 pane、不登记
- [x] stop / close / purge / restore 四档 ✅ stop:对 agent 的前台进程组发 SIGTERM(先核对进程组不是 shell、argv[0] 是 pi),等它从 herdr 消失,pane 与注册保留;close:关 pane,注册保留;purge:关 pane 并注销(**不修改拓扑**,删 IP 用 `a2a topology remove-ip`);restore:stopped 在原 pane 重启、closed 新建 pane,都重新改名;每步写 `AGENT_*` 审计;同一 agent 的操作串行(文件锁)
- [x] 回归:目标停止后,队列中的消息按 08 §7.2 处理 ✅ 队列头判 `TARGET_MISSING` 并暂停,后续消息不发;restore 之后仍不自动放行,操作员 `resolve retry` 后继续,接收方三条各收到一次

测试(5f 完成时):单元测试 412 个(Mac 30 跳过,VM 29 跳过,跳过的都是集成测试);**真实 herdr 集成测试 29 个全部通过**:herdr 14、投递引擎 5、broker 子进程 2、端到端 2、生命周期(假 agent)3、真实 fnx 启动 2、**真实 fnx 生命周期 1**(fnx_dv / fnx_sw 的 spawn → stop → restore → purge,不发提示词)。临时会话与进程已清理,无 fnx 残留进程。

限制:stop 用 `os.killpg` 发信号,必须与 herdr 在同一台机器上运行(本项目是虚拟机)。

**阶段 5 完成。**

### 阶段 5 审核修复(Codex 审核提交 4a003bd)

- [x] 队列状态文件丢失时 fail-closed ✅ 已有带 queue_seq 的消息而状态文件不见了 → `QueueStateError`,不再按"无未放行槽位"初始化(否则失败的队列头会被跳过)
- [x] 恢复时不误删未放行槽位 ✅ 只有"done/ 里没有、pending/ 里还有未终结副本"才删;done/ 丢失或读不出时保留,由队列头 fail-closed
- [x] stop 核对 agent 身份 ✅ 发信号前按登记的 agent_name 查 herdr,必须在登记的 pane 上;pane 里换成了别的 pi 时拒绝(Codex 定为阻断,我们评为应修,一并修)
- [x] 裁定补做时补齐审计 ✅ 按 (ruling_id, 事件, msg_id) 补记缺失的迁移 / 重试入队 / 放行事件,已有的不重复;一个槽位的放行只记一次(含 broker 自动放行)

- [x] 裁定后的自动放行与裁定关联(Codex 复核应修 1)✅ 消息被裁定为已送达后由 broker 自动放行时,`release` 与 `QUEUE_RELEASED` 带该 ruling_id(查审计确认是"已送达"裁定;来自更早"未送达,重试"裁定的不算);运行中裁定遇到"已放行"不再另记,只有启动恢复时补记崩溃丢失的放行审计
- [x] 裁定跨进程串行(Codex 复核应修 2)✅ `rulings.lock`(状态目录下 rulings.lock):`a2a resolve` 的 检查 → 落盘 → 生效、`a2a ruling void`、broker 启动补做共用一把全局锁;并发测试用两个真实进程裁定同一队列头,只有一个落盘
- [x] broker 自动放行遇到"已放行"不再写审计(Codex 第二次复核应修)✅ 裁定线程先放行时,broker 的 release 得到 already_released,直接继续;谁实际放行谁写审计(裁定侧与 broker 侧一致)。测试在 broker 读到队列头之后、放行之前插入裁定
- [x] broker 优雅停机(审核 Codex 阶段 6 时发现我方同类问题)✅ SIGTERM/SIGINT 后等投递线程收尾:等待目标空闲分段进行(每段 ≤2 秒)、退避等待可被打断,收到停机时消息保持原状态返回;已调用 prompt 的等它返回并分类完再退出(上限 观察窗口+30 秒);systemd 模板加 `KillMode=mixed`、`TimeoutStopSec=90`。测试:单元"等待中停机"、VM"prompt 进行中收到 SIGTERM → DELIVERED 且只写入一次"
- [ ] 将来考虑:stop 在"核对 agent 名字"与 killpg 之间仍有极短的进程替换窗口,可评估进程身份快照校验(如核对 pid 与启动时间)

测试:单元测试 425 个(Mac 31 跳过,都是集成测试);VM 全套 425 个通过、0 跳过,其中**真实 herdr 集成测试 30 个全部通过**(含真实 fnx 3 个,不发提示词)。

其他:

- [ ] 升级 herdr 后重跑 `review-probes/probe_herdr_errors.py`
- [x] 真实 `fnx_dv → fnx_sw` 端到端:经用户同意并入阶段 6,已跑通(见下)

## 阶段 6:pi 侧接入 + 真实模型链路(2026-10-10,测试方案 `10-stage6-test-plan.md`,插件说明 `11-pi-extension-explained.md`)

pi 侧接入(不调用模型):

- [x] spawn 支持启动参数:拓扑角色新增 `launch_args`(测试时 `--no-builtin-tools`,模型只剩 `a2a_send`)
- [x] spawn 向 pane 额外注入 `A2A_STATE_DIR` / `A2A_TOPOLOGY` / `A2A_PYTHON` / `A2A_SRC`,插件与 broker 用同一状态目录,VM 不需安装 a2a
- [x] 新命令 `a2a edges`:列出当前身份可用的边及填好 IP 的文字;只读拓扑、不核对注册表(插件加载早于登记),不授予权限,`send` 仍完整鉴权
- [x] pi 扩展 `pi-extension/a2a.ts`:`pi.registerTool()` 注册 `a2a_send`,参数只有 `edge_id`(enum 限定为本角色出边),执行时 `execFile` 调 `python -m a2a.cli send`,不经 shell;被拒时把原因作为工具错误返回
- [x] pi 扩展:`tool_call` 钩子,拦截 bash 中的 `herdr agent prompt` / `herdr pane send-text/send-keys/run`;读状态命令不拦
- [x] 实测 fnx(pi 0.79.10)从 `agent/extensions/` 自动加载扩展、`--no-builtin-tools` 后只剩扩展工具
- [x] `AGENTS.md`:不需要。测试约束("不要真的去做"等)直接写进模板,工具说明里写明用 `a2a_send`、不要直接调 herdr
- [ ] 将来考虑:插件目前是每个 agent 目录一份副本,可改为软链接到仓库同一文件;另外 3 个 pi 智能体待实测能否加载
- [x] 2026-10-10 卸掉常驻安装的插件(用户同意)✅ 审核 Codex 阶段 7 时发现:我常驻的 `a2a.ts` 与 Codex 测试临时安装的插件都注册 `a2a_send`,同一 extensions 目录里会同时加载,可能同名冲突。已删除 `~/.forenyx/fnx_{dv,sw}/agent/extensions/a2a.ts` 及空目录;以后只在测试或真实运行时临时安装、结束还原

测试:Mac 单元 439 个通过(跳过 32:31 个集成 + 1 个需本机装 fnx 的启动脚本预检);插件单元测试 8 个(Mac 用 node 加载,覆盖 插件 → 命令行 → Router → 队列);VM 全套 439 个通过、跳过 8 个(插件单元测试,VM 无 node);其中**真实 herdr 集成测试 31 个全部通过**(含真实 fnx 4 个:插件加载、只剩 `a2a_send`、enum 与说明文字正确,不发提示词)。

真实模型链路(经用户同意调用模型,uart,会话 `a2a_s6`):

- [x] 唯一人工输入:`herdr agent prompt` 给 fnx_dv"假设你已经完成了 uart ip 的 UVM 验证,请用 a2a_send 工具通知软件智能体。"
- [x] fnx_dv 模型自己调用 `a2a_send(dv_done)` → broker 投递给 sw_uart
- [x] fnx_sw 模型调用 `a2a_send(sw_test_pass)`(二选一选了"测试成功")→ broker 投递给 dv_uart
- [x] fnx_dv 回复"收到",没有调用工具
- [x] 审计:两次投递各一次(QUEUED → DISPATCHING → DELIVERED → QUEUE_RELEASED),文字与模板填入 uart 后逐字一致;全程约 6 秒;broker 日志无错误
- [x] 模型证据:两边 TUI 状态栏 `(SophNet) DeepSeek-Flash`;fnx 会话文件每条 assistant 消息记录 provider=sophnet、model=DeepSeek-Flash 和 token 用量
- [x] 临时工作目录保持为空
- [ ] 观察:两边调用 `a2a_send` 后都多输出了一段"已发送"总结,不影响通信;正式使用时可在模板里加约束
- [x] `sw_test_fail` 用真实模型补跑(用户要求,会话 `a2a_s6b`,状态目录 `~/a2a_stage6_run2`)✅ 为了确定性地走到这条边,本轮用 `a2a topology set-template` 把 `dv_done` 的结尾改为"直接用 a2a_send 回复测试失败"(只改这一轮的拓扑,有 `TOPOLOGY_CHANGED` 审计)。fnx_sw 调 `a2a_send(sw_test_fail)`,fnx_dv 回复"收到";审计两次投递各一次,文字逐字一致,约 3 秒;工作目录为空;fnx 会话文件记录 sophnet / DeepSeek-Flash。插件为修复后的版本
- [ ] 正式使用:让 `fnx_sw` 收到固定句式后真的开始驱动与 HAL 开发、让 `fnx_dv` 在 UVM 验证完成时调用 `a2a_send(dv_done)`(模板换成真实工作指令)

### 阶段 6 审核修复(Codex 审核未提交的阶段 6)

- [x] 插件发送结果分三类(Codex 应修:入队后报错会让模型重试、重复投递)✅ 只有"退出码 4 + Router 拒绝 JSON"报"已拒绝,消息没有发送"(工具错误);超时、python 出错、回执看不懂、入队后写审计失败都报"发送结果未知"(不是工具错误),要求不要再次调用、停止自动流程、告诉用户,并给出核查方式;取消信号不再传给发送子进程(Esc 不能取消已开始的发送);工具说明加"每次通知只调用一次"。修复方案先经 Codex 审核。测试:插件单元新增 5 个(崩溃、超时、回执看不懂、入队后审计失败、说明文字),变异检查(未知改回按拒绝处理)4 个失败
- [x] 成功回执严格校验(Codex 复核应修)✅ `msg_id`/`dst`/`text` 非空、`state` 为 QUEUED、`queue_seq` 为非负整数才算发送成功,否则归为结果未知。测试:10 种畸形回执 + 1 个合法回执;变异检查(改回只查 msg_id)8 个子用例失败
- [x] 真实 fnx 集成测试不再删除已常驻安装的插件(结束时还原原内容)
- [ ] 将来考虑:机制级防重复需要稳定的业务事件 ID 幂等;toolCallId 不行(模型重试是新的调用、新的 ID)
- [ ] 将来考虑:`HERDR_WRITE` 正则可能误拦文档 / 搜索命令,也能被变量、别名绕过;它是软限制,不是安全边界
- [ ] 将来考虑:插件加载时缓存可用的边,拓扑新增边要重启 agent 才可见,删除的边仍显示但会被 Router 拒绝;留到动态拓扑阶段

修复后测试:Mac 单元 445 个通过(跳过 32:31 个集成 + 1 个需本机 fnx);插件单元 14 个在 Mac 通过;VM 全套 445 个通过、跳过 14 个(插件单元测试,VM 无 node),其中真实 herdr 集成测试 31 个全部通过。已常驻安装的插件更新为修复后的版本(运行中的 `a2a_s6` 两个 agent 仍是旧版,重启后才加载新版)。

## 阶段 7:全链路

- [x] 跑通 `dv_uart` → `sw_uart`(已在阶段 6 用真实模型跑通双向)
- [x] 测试方案 `12-stage7-rollback-plan.md`,经 Codex 审核两轮(1 阻断 + 5 应修已改)✅ 用户决定:spec / arch / rtl 暂不安装,用假 agent;回退边 `dv_bug`(dv → rtl)、`rtl_fixed`(rtl → dv)
- [x] 假 agent 新增 `script:<边>,...` 模式 ✅ 由 spawn 启动,第 n 次输入执行 `a2a send <第 n 条边>`(用注入的 `A2A_PYTHON` / `A2A_SRC`,与插件一致);可选同步屏障;发送结果写进单独的 events 文件
- [x] 不调用模型的回退与多 IP 集成测试(真实 herdr)✅ `tests/test_integration_rollback.py` 5 个:回退闭环 dv → rtl → dv(sw 未收到);多条出边不串;uart / gpio 用屏障同时发送,各自只到本 IP;**入队后目标消失**:rtl_gpio 停止后 gpio 队列 TARGET_MISSING 暂停,uart 照常送达;**入队前拒绝**:目标未 spawn(`target_missing`)/ 已停止(`target_not_running`)不入队。按精确内容和次数核对接收日志。变异检查:目标 IP 固定为 uart → 并发隔离用例失败;目标角色固定为 sw → 闭环、多出边用例失败
- [x] 回退通信的真实模型链路(用户同意,会话 `a2a_s7`,状态目录 `~/a2a_stage7_run`)✅ dv、sw = 真实 fnx(用户要求 sw 也用真实的),rtl = 假 agent(`script:rtl_fixed`)。fnx_dv 在 `dv_done` / `dv_bug` 中选了 `dv_bug` → rtl_uart;假 rtl 自动回 `rtl_fixed`,此时 fnx_dv 仍在输出总结,broker 记 `WAITING_TARGET`,等它空闲后才投递;fnx_dv 回复"收到。"。审计两次投递各一次,文字逐字一致,约 2 秒;sw_uart 没有任何消息,fnx_sw 会话目录为空(模型未被调用);工作目录为空;fnx_dv 会话文件记录 sophnet / DeepSeek-Flash,调用参数 `{'edge_id': 'dv_bug'}`

测试:Mac 单元 450 个通过(跳过 37:36 个集成 + 1 个需本机 fnx);VM 全套 450 个通过、跳过 14 个(插件单元测试,VM 无 node),其中**真实 herdr 集成测试 36 个全部通过**(新增回退 5 个)。

- [x] 审核修复(Codex 应修:"没收到"只靠再等 1 秒,晚到的多余消息可能漏判)✅ 每个用例加队列侧证据:先等所有运行中的 agent 回到 idle、队列无待处理消息,再核对入队记录恰好是预期的(边, 发送方, 目标)、每条最多送达一次。变异检查:假 rtl 3 秒后重复发送 → 闭环用例失败
- [x] 回退通信(如验证 → RTL)按拓扑配置跑通 ✅ 不调用模型的 5 个集成测试 + 真实模型链路
- [x] 审计日志里能看到完整一次链路(阶段 6 真实链路)

## 阶段 8:规模化

第一步规模压测:方案 `13-stage8-scale-plan.md`(Codex 审核三轮),报告 `14-stage8-scale-report.md`。

- [x] 布局探测 ✅ 5 tab × 30 pane 共 150 个全部建成,零失败;herdr 服务 145 MB
- [x] N = 2 全部步骤 ✅(初步观察)
- [x] N = 10 / 20 / 30 ✅ 150 个 agent 无错误、无丢失 / 重复 / 串 IP;同时投递 95 分位 1.3–1.5 秒,三档未见上升;broker 线程与 herdr 并发跟活跃目标数走;积压 600 条 27 秒送完;唯一瓶颈是逐个 spawn(150 个 10 分钟);本次负载未显示必须改 socket 的证据
- [x] Codex 审核 d5a962f(阻断 1、应修 6)已修 ✅ 唯一会话名 + 只清理本次创建的资源;采样估计标注;结论收窄;停止条件可中断;清理失败即运行失败;布局探测失败即停
- [x] 查清 spawn 每个约 4 秒的原因 ✅ herdr 识别 pi 后约 3 秒才把 `unknown` 判为 `idle`,a2a 必须等到可投递才登记;逐个启动时 150 个约 10 分钟
- [x] 真实 fnx 启动与内存实测 ✅ spawn 每个约 4 秒(与假 agent 相同);60 个实测内存线性增长,每个约 98 MB(首批约 124 MB,共享代码只加载一次);150 个空闲约 14.9 GB,放不下;留 20% 余量约能放 120 个
- [ ] 并行批量 spawn:用户很少整体重启,暂不做
- [ ] 真实 fnx 规模测试(方案 `15-stage8-real-fnx-scale-plan.md`,脚本 `tests/stress/real_fnx_scale.py`,Codex 审核多轮):A 档(5 个 IP、25 次模型调用)按框架判定停止——6 次投递 DELIVERY_UNCERTAIN。会话记录证明消息都已送达、5 条链路模型层面都走完、无重复无串 IP;根因是同一 tab 里对半拆分的 pane 太小(2–4 行),herdr 只靠屏幕识别 Working,看不到。修复后待重跑 A 档,再由用户决定 B 档
- [x] 修复:插件主动向 herdr 上报 working / idle ✅ 方案 `16-stage8-report-state-plan.md`(Codex 审核六轮)。agent_start 上报 working(等完成,约 2 秒上限);agent_end 只在能确定 pi 已结束时上报 idle(白名单 aborted,或 stop 且按 pi 相同公式算的上下文 token 低于 (窗口 − 实际预留) × 90%),其他保持 working;压缩开始报 working。spawn 注入 A2A_HERDR_BIN。部署约束:fnx 只加载本插件。已知局限与恢复方式(重启 agent)写进方案 §7 与 README。真实 fnx 探测:上报覆盖屏幕判断、进程退出后不残留、重启后序号可重来。测试:插件单元 25 个(Mac),变异检查覆盖各条规则;Mac 单元 462 通过(跳过 37);VM 全套 462 通过(跳过 25,插件单元),真实 herdr 集成 36 个全部通过
- [x] 修复 broker:全局停止投递(dispatch.halted)挡不住已在途的 worker ✅ Codex 审核真实 fnx 规模测试脚本时发现:投递引擎的停止检查只看停机信号,已在等目标空闲的 worker 在写了 dispatch.halted 后仍会发 prompt。改为停机或全局停止都让引擎在等待循环每一轮、写 DISPATCHING 前、重试退避后停下,消息保持原状态。先补测试确认失败(halt 后目标空闲仍发出 prompt),修后通过。局限:检查与写 DISPATCHING 之间仍有极短窗口。测试:Mac 单元 451 通过(跳过 37);VM 全套 451 通过(跳过 14,插件单元),真实 herdr 集成 36 个全部通过
- [x] 真实规模的内存方案 ✅ 用户决定(2026-10-10):同时活跃的 IP 不超过 20 个左右(约 100 个 agent,本 VM 空闲时放得下);超过时给服务器加内存。不做"只运行活跃 IP"或多 VM
- [x] 批量创建 5 个 tab ✅ spawn 按角色建 tab、按 IP 拆 pane;5 tab × 30 pane 布局探测零失败
- [ ] 按 IP 动态增删 pane / agent(`stop` / `close` / `purge` / `restore`)
- [ ] registry 自动恢复
- [x] 多 IP 并发 ✅ 阶段 7 用屏障验证 uart/gpio 同时发送隔离;阶段 8 压测 30 个 IP 同时投递无串 IP
- [x] 150 个 agent 的资源压测 ✅ 假 agent 150 个跑通;真实 fnx 60 个测内存线性度(每个约 98 MB);用户决定同时活跃不超过约 20 个 IP,超过时加内存
- [ ] 视情况引入 socket 事件订阅:150 个假 agent 的压测未显示必须改 socket 的证据(并发约 30 个 herdr 子进程,开销小),暂不做;socket 仍未实测
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
