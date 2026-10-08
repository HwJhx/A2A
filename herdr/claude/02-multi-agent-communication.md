# herdr 多 Agent(异构)通信机制调研

> 承接 `01-install-and-launch-agents.md`。你的现状:herdr 里已有两个 pane,一个 `claude`,一个 `codex`。
> 本文只做调研与操作流程整理,**未执行任何 herdr 命令**。
>
> 信息来源(2026-10-08 抓取):
> - https://herdr.dev/docs/agent-automation/
> - https://herdr.dev/docs/agent-skill/
> - https://raw.githubusercontent.com/herdrdev/herdr/v0.9.3/skills/herdr/SKILL.md(命令细节主要来自这里)
> - https://herdr.dev/zh-cn/docs/socket-api/
> - https://herdr.dev/docs/integrations/
> - https://herdr.dev/zh-cn/docs/session-state/
>
> 抓取工具返回的是摘要,个别参数可能有出入。**以你本机 `herdr --help`、`herdr agent --help` 的输出为准。**

## 1. 结论先行

herdr 里的"agent 间通信"**不是 A2A 协议那种 agent 卡片/任务对象的协议级通信**,而是:

> 通过 CLI / Unix socket,让一个 agent(或脚本)去**读取、输入、等待另一个 agent 所在的终端 pane**。

也就是"终端层的编排":发一段文字进对方的输入框,等对方状态变成 idle/done/blocked,再把对方屏幕上的输出读回来。因为只依赖终端,所以天然支持异构 agent(Claude Code、Codex、OpenCode、Gemini 等二十多种),不要求它们实现任何统一协议。

这点对你学 A2A 很关键:herdr 是一种"pane 即通道、屏幕即消息"的实现,可以和真正的 A2A 协议做对照(见第 9 节)。

## 2. 三层抽象

| 层 | 对象 | 说明 |
| --- | --- | --- |
| layout | workspace / tab / pane | 拓扑结构,ID 如 `w1`、`w1:t1`、`w1:p1` |
| pane | 原始终端 | 可 run / send-text / send-keys / read / wait-output |
| agent | 被识别出的编码 agent | 可 start / prompt / wait / read / send-keys,可用"名字"或所在 pane ID 寻址 |

ID 是不透明的稳定句柄:关闭后不复用;`pane move` 后会换新 ID。

## 3. 通信四件套

### 3.1 发送:`agent prompt`

```bash
herdr agent prompt <名字或pane-id> "要对方做的事" --wait --timeout 120000
```

- 向目标 agent 提交一段提示词。
- 加 `--wait`:等对方进入第一个稳定的 `idle` / `done` / `blocked` 状态才返回(此时不要再加 `--until`)。
- 如果对方此刻停在**审批/提问界面**(`blocked`),返回 `agent_blocked`,**不会发送任何输入**,需要人先处理。

### 3.2 等待:`agent wait` / `pane wait-output`

```bash
herdr agent wait <名字> --until blocked --timeout 120000
herdr pane wait-output <pane-id> --match "字面子串" --timeout 120000
herdr pane wait-output <pane-id> --regex "<Rust正则>" --timeout 120000
```

- `agent wait`:等 agent 状态。不带 `--until` 时默认等 idle/done/blocked,可重复 `--until` 指定多个。
- `pane wait-output`:等输出匹配。注意它不解析 agent 生命周期,而且**已存在的文字也会立即匹配**。

### 3.3 读取:`agent read` / `pane read`

```bash
herdr agent read <名字> --source recent-unwrapped --lines 120
herdr pane read <pane-id> --source recent-unwrapped --lines 120
```

`--source` 取值:

| 值 | 含义 |
| --- | --- |
| `visible` | 当前可见视口 |
| `recent` | 近期输出(含软换行) |
| `recent-unwrapped` | 近期输出,去软换行,**读日志推荐** |
| `detection` | herdr 做状态检测用的纯文本快照 |

- 默认去掉 ANSI 转义;`pane read` 用 `--format ansi` 保留(agent-automation 页写作 `--ansi`,以 `--help` 为准)。
- Claude Code、OpenCode 这类全屏 TUI,**空闲时**读长历史会自动用鼠标滚动采集;**忙碌时**显式请求历史会返回 `agent_not_idle`。

### 3.4 底层输入:`send-keys` / `send-text` / `run`

```bash
herdr agent send-keys <名字> esc          # 给 agent 界面发按键(esc/up/enter/ctrl+c ...)
herdr pane send-text <pane-id> "文本"      # 只发文字,不回车
herdr pane send-keys <pane-id> enter       # 发按键/组合键
herdr pane run <pane-id> "just test"       # 发命令 + 回车(原子),用于普通 shell pane
```

## 4. 状态模型(通信的"同步点")

`blocked`(等你批准/回答)、`working`、`idle`、`done`、`unknown`。

- 空闲 ≠ 完成:刚启动就绪、恢复会话这类不算"完成的工作"。
- Codex 在活跃轮次和回复结束后标题可能相同,herdr 会回落到 `unknown`,等待它时要留意超时。
- 状态判断来源:前台进程 + 屏幕内容 +(可选)集成上报。装了集成的 agent 会通过 `pane.report_agent` 主动上报语义状态,更准确。

## 5. 对你当前场景的实操:让 Claude 驱动 Codex

你已经手动启动了两个 pane,可以直接用**pane ID** 寻址,不必重新用 `agent start`。

### 5.1 先摸清环境(在任意 herdr pane 里)

```bash
test "${HERDR_ENV:-}" = 1 && echo "在 herdr 内"
printf '%s\n' "$HERDR_WORKSPACE_ID" "$HERDR_TAB_ID" "$HERDR_PANE_ID"
herdr workspace list
herdr pane list --workspace "$HERDR_WORKSPACE_ID"
herdr agent list
```

- 每个受管 pane 都被注入了 `HERDR_ENV=1`、`HERDR_WORKSPACE_ID`、`HERDR_TAB_ID`、`HERDR_PANE_ID`。
- 不要在 pane 里直接敲裸 `herdr`(会再启动/附加 TUI)。

`herdr agent list` 应能看到两个 pane 里的 claude 和 codex,以及它们的状态。

### 5.2 人手验证一次(在第三个普通 shell,或任一 pane 里)

假设 codex 在 `w1:p2`:

```bash
herdr agent get w1:p2
herdr agent prompt w1:p2 "只回复一句话:你是谁,当前目录是什么。" --wait --timeout 120000
herdr agent read w1:p2 --source recent-unwrapped --lines 60
```

能收到回复,说明链路通了。

### 5.3 让 Claude Code 自己去指挥 Codex

在 Claude Code 的输入框里,直接用自然语言下任务,例如:

> 你在 herdr 里运行。请用 herdr CLI 向 w1:p2 里的 codex 发送:"审查当前 git diff,只报告可执行的问题",等它完成后读回输出并总结给我。不要关闭任何 pane。

Claude 会自己组合出 `agent prompt --wait` + `agent read`。为了让它**稳定地**会用,建议装官方 skill(下一节)。

## 6. 安装官方 agent skill(让 agent 学会用 herdr)

skill 文件位于仓库 `skills/herdr/SKILL.md`,作用是教 coding agent 在 herdr pane 里控制 herdr。它要求先检查 `HERDR_ENV=1`,不满足就停手。

```bash
# 方式一:skills 工具,-g 全局安装;去掉 -g 则只装到当前项目
npx skills add herdrdev/herdr --skill herdr -g

# 方式二:herdr 自带,打印与当前版本匹配的 SKILL.md
herdr --skill
```

- 官方页面没写 Claude / Codex 各自的安装路径。方式二可以拿到文本,再自行放进:
  - Claude Code:`~/.claude/skills/herdr/SKILL.md`(用户级)或项目 `.claude/skills/herdr/SKILL.md`
  - Codex:没有统一 skill 机制时,把内容追加到 `AGENTS.md`
  - 以上两个路径是我按两款工具的惯例推断,**官方页面未明说**,装前请自行确认。
- `npx skills` 会往你的用户目录写文件,执行前请知悉。

## 7. 让 agent 自己开 helper agent(`agent start`)

标准配方(SKILL.md 给出的):

```bash
# 1) 看布局,宽 pane 向右拆、窄/高 pane 向下拆
herdr pane layout --pane "$HERDR_PANE_ID"

# 2) 拆出同级 pane,不抢焦点,新 pane ID 在 .result.pane.pane_id
herdr pane split --current --direction right --cwd "$PWD" --no-focus

# 3) 在新 pane 里启动命名 agent(目标必须是空闲的交互式 shell)
herdr agent start reviewer --kind codex --pane <新pane-id>
#    给 agent 传参:-- 之后原样透传
herdr agent start reviewer --kind codex --pane <新pane-id> -- -m gpt-5.4

# 4) 派活并等待
herdr agent prompt reviewer "Review the current diff and report only actionable findings." --wait --timeout 120000

# 5) 取回结果
herdr agent read reviewer --source recent-unwrapped --lines 120
```

规则:
- 名字须匹配 `[a-z][a-z0-9_-]{0,31}`,在存活 agent 中唯一;agent 退出后名字失效。
- `--kind` 支持 `claude`、`codex`、`gemini`、`opencode` 等二十多种。
- `agent start` 不会新建/移动布局,只往已有 shell pane 里启动。默认启动超时 30 秒(`--timeout` 可调)。
- 命令接受"唯一存活名字"或"承载该 agent 的 pane ID",**不接受**terminal ID 或 agent 类型名。

> 待验证:对**已经手动启动**的 `claude`/`codex` pane,能否事后给它们起名字(别名)?文档只写了通过 `agent start --name` 式的命名,未见"重命名已有 agent"。所以对手动启动的 pane,用 pane ID 寻址最稳。

## 8. 常见陷阱

1. **超时 ≠ 没发出去**:`agent prompt` 超时或返回 `agent_prompt_stalled`,输入可能已送达。重试前先 `agent get` + `agent read`,避免重复提交。
2. **读结果别靠一次性输出**:输出长或被折叠时,可以让对方把 Markdown 写进临时文件,只回复路径,再读文件(别在最初 prompt 里就要求写文件)。
3. **blocked 要人来**:对方在审批/提问时 `agent prompt` 不会硬塞输入;需要 `agent read` 看界面,再决定 `agent send-keys` 或让人处理。
4. **别抢焦点**:后台操作带 `--no-focus`;优先用 `--current` 或明确 ID,不要依赖"当前焦点 pane"。
5. **别乱关**:不关不是自己创建的 workspace/tab/pane;不在活动会话里 `herdr server stop`;实验用命名测试会话。
6. **权限/审批**:Claude 或 Codex 默认会在执行命令、写文件时弹审批。你让 A 去驱动 B,B 会卡在 blocked。是否放开 B 的权限属于你的安全决策,请单独评估,别为了流畅就默认全开。
7. **提示注入风险**:A 读回 B 的屏幕内容并据此行动,B 屏幕里如果混入不可信文本(网页、依赖包输出),可能影响 A。重要操作保留人工确认。

## 9. 与 A2A 协议的对照(学习要点)

| 维度 | herdr | Google A2A 协议 |
| --- | --- | --- |
| 传输 | 本地 Unix socket(Windows 命名管道)+ CLI,换行分隔 JSON | HTTP(S) + JSON-RPC / SSE 等 |
| 寻址 | pane ID / 本地 agent 名字 | Agent Card 描述的 URL |
| 能力发现 | `agent list`(只知道种类、状态) | Agent Card 声明技能与能力 |
| 消息内容 | **纯文本进输入框、从屏幕读回** | 结构化 Message / Task / Artifact |
| 任务生命周期 | blocked/working/idle/done 屏幕推断或集成上报 | Task 状态机,协议原生 |
| 跨机器 | `herdr machine add` + SSH,命令加 `--machine` | 网络原生 |
| 对 agent 要求 | 无(只要是终端程序) | 需实现协议 |

一句话:herdr 是"终端复用器上的 agent 编排",A2A 是"agent 之间的应用层协议"。前者零侵入、后者结构化。两者不冲突,可以研究如何在 herdr 之上包一层 A2A 适配(见第 11 节)。

## 10. 底层:Socket API(想自己写程序时)

- 协议:每行一个 JSON 请求 `{"id":..., "method":..., "params":...}`,响应同 `id`,成功带 `result`,失败带 `error{code,message}`。
- 默认 socket:`~/.config/herdr/herdr.sock`;命名会话:`~/.config/herdr/sessions/<name>/herdr.sock`。
- 解析顺序:`--session` → `HERDR_SOCKET_PATH` → `HERDR_SESSION` → 默认会话。
- 方法分组:
  - 服务器:`ping`、`server.stop`、`server.reload_config`、`server.agent_manifests`
  - 会话:`session.snapshot`
  - 窗格:`pane.split / read / send_text / send_keys / send_input / report_agent / wait_for_output / close …`
  - 智能体:`agent.list / get / read / explain / prompt / wait / start`
  - 事件:`events.subscribe`、`events.wait`(长连接订阅;落后会收到 `events_lost`,需重订阅并用 `session.snapshot` 对账)
  - 集成/通知/插件:`integration.install`、`notification.show`、`plugin.*`
- 获取完整 JSON Schema:`herdr api schema --json`
- 官方建议:大多数自动化优先用 CLI 包装命令,需要事件订阅或直接请求/响应时才用原始 socket。

## 11. 集成(integration)的作用与副作用

```bash
herdr integration install claude
herdr integration install codex
herdr integration status
herdr integration uninstall <agent>
```

作用:让 herdr 记录 agent 的原生会话 ID(重启后恢复同一对话),部分集成还会上报精确状态。

**会改动你的本机配置**(装之前请知悉):

- Claude Code:写 `~/.claude/hooks/herdr-agent-state.sh`,并在 `~/.claude/settings.json` 里追加 hook 条目。(若设了 `CLAUDE_CONFIG_DIR` 则用该目录,目录必须已存在。)
- Codex:写 `~/.codex/herdr-agent-state.sh`,更新 `hooks.json`,并确保 `config.toml` 含 `[features] hooks = true`。卸载**不会**还原 `config.toml`。

可用 `[session] resume_agents_on_restore = false` 关闭恢复功能。

## 12. 会话持久化(与通信的关系)

| 机制 | 保留什么 | 限制 |
| --- | --- | --- |
| 分离(`ctrl+b q`) | 进程、pane、agent 都继续跑 | 仅 server 活着时 |
| 快照恢复 | 布局、目录、焦点 | 进程不保留,pane 重开为新 shell |
| agent 原生续接(默认开) | 对话 | 需官方集成且版本够 |
| 热交接 `--handoff`(实验) | 活跃 pane 与进程迁到新 server | 进行中的 CLI/API 请求、wait、订阅、pane 间消息可能中断,客户端需重连重试;仅官方 updater 安装的版本可用(Homebrew/mise/Nix 不行) |

注意:你如果用 Homebrew 装的 herdr,无法 `herdr update` 热交接。

## 13. 建议的动手顺序(下一步)

1. `herdr --help`、`herdr agent --help`、`herdr pane --help` 看真实参数,和本文对照。
2. 在 herdr 的 pane 里跑 5.1 的只读命令(`agent list`、`pane list`),确认能看到 claude / codex。
3. 做 5.2 的手工 `agent prompt` + `agent read` 往返。
4. 再决定是否装 skill(第 6 节)、是否装集成(第 11 节)。
5. 让 Claude 通过 herdr 指挥 Codex 做一个只读任务(如审查 diff),观察 blocked 与超时行为。
6. 之后再研究:用 Socket API 写一个小脚本,把 herdr 包成 A2A 的 Agent Card + Task 接口。

## 14. 待确认

- 对手动启动的 agent 能否命名/改名。
- `pane read --format ansi` 与 `agent read --ansi` 的真实参数名。
- skill 在 Claude / Codex 的推荐安装位置。
- 以上都可以通过 `herdr ... --help` 和读取仓库 `skills/herdr/SKILL.md` 原文确认;如需要,我可以把 SKILL.md 全文落盘到本目录供你对照。
