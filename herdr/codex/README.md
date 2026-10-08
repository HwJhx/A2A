# macOS 安装 Herdr，并在 Herdr 中启动 Codex 与 Claude Code

> 本文面向 macOS Apple Silicon（`arm64`）。本文只记录操作流程，不会自动执行安装。

## 1. 前置检查

打开 Terminal，确认两个编程 agent 已经可以直接调用：

```bash
uname -m
command -v codex
command -v claude
```

如果 `uname -m` 输出 `arm64`，说明是 Apple Silicon。`codex` 和 `claude` 都应返回可执行文件路径。

## 2. 安装 Herdr

### 方案 A：官方安装脚本（推荐）

```bash
curl -fsSL https://herdr.dev/install.sh | sh
```

安装完成后，如果当前 Terminal 找不到命令，关闭并重新打开 Terminal，或确认安装目录已经加入 `PATH`。

### 方案 B：Homebrew

如果你已经正常使用 Homebrew：

```bash
brew install herdr
```

两种方案选一种即可，不要重复安装。Homebrew 安装的版本应使用 Homebrew 升级：

```bash
brew upgrade herdr
```

官方安装脚本安装的版本可以使用：

```bash
herdr update
```

## 3. 验证 Herdr 是否安装成功

```bash
command -v herdr
herdr
```

`herdr` 会启动或重新连接默认后台会话。第一次启动后，可以先按 `Ctrl+B` 进入前缀模式，再按 `?` 查看当前快捷键。

## 4. 在 Herdr 中启动 Codex 和 Claude Code

进入你要工作的项目目录，例如：

```bash
cd /path/to/your/project
herdr
```

在 Herdr 打开的终端窗格中启动 Codex：

```bash
codex
```

然后新建一个窗格，在另一个窗格启动 Claude Code：

```bash
claude
```

Herdr 会自动识别 `codex` 和 `claude`，并在侧边栏显示 agent 状态，例如 `working`、`blocked`、`done` 或 `idle`。

常用分屏快捷键：

| 操作 | 快捷键 |
| --- | --- |
| 进入 Herdr 前缀模式 | `Ctrl+B` |
| 向右分屏 | `Ctrl+B`，然后 `V` |
| 向下分屏 | `Ctrl+B`，然后 `-` |
| 新建 tab | `Ctrl+B`，然后 `C` |
| 切换下一个/上一个 tab | `Ctrl+B`，然后 `N` / `P` |
| 分离但保持 agent 继续运行 | `Ctrl+B`，然后 `Q` |

也可以完全使用鼠标：右键窗格边框，选择分屏或创建 tab。

## 5. 安装 Herdr 的 agent 集成（建议）

Herdr 可以通过集成获得更准确的状态识别，并在 Herdr server 重启后更好地恢复支持的 agent 会话。分别执行：

```bash
herdr integration install codex
herdr integration install claude
```

然后重新启动 Herdr：

```bash
herdr
```

注意：集成安装不会替代 Codex 或 Claude Code 本身；这两个 agent 仍需要单独安装、登录并能在普通 Terminal 中正常运行。

## 6. 推荐的实际使用流程

```bash
cd /path/to/your/project
herdr
```

进入 Herdr 后：

1. 在第一个窗格执行 `codex`。
2. 使用右键分屏，或按 `Ctrl+B`、`V`。
3. 在第二个窗格执行 `claude`。
4. 分别完成 Codex 和 Claude Code 的登录或授权。
5. 关闭 Terminal 窗口，或按 `Ctrl+B`、`Q` 分离；两个 agent 会继续在后台运行。
6. 之后回到项目目录，再执行 `herdr`，即可重新连接。

## 7. 常见问题

### `herdr: command not found`

重新打开 Terminal，然后检查：

```bash
command -v herdr
echo "$PATH"
```

如果仍然找不到，优先重新执行官方安装脚本，或根据安装器提示把安装目录加入 shell 的 `PATH`。

### Herdr 侧边栏没有识别到 agent

确认 agent 可以在普通 Terminal 中启动：

```bash
codex
claude
```

确认是在 Herdr 的窗格里直接启动命令，而不是在 Herdr 外部启动后再尝试接入。也可以重新安装对应集成：

```bash
herdr integration install codex
herdr integration install claude
```

### 想停止后台会话

```bash
herdr server stop
```

这会停止 Herdr server 以及其中运行的窗格进程；需要保留 agent 运行时只做分离，不要执行此命令。

### macOS 上使用代理导致安装失败

如果 Homebrew 或 `curl` 被配置到了不可用的本地代理，先检查：

```bash
env | grep -i proxy
```

确认代理可用后再安装；不要为了安装 Herdr 随意修改系统代理或 Homebrew 目录权限。

## 8. 官方资料

- [Herdr 中文 README](https://github.com/herdrdev/herdr/blob/master/README.zh-CN.md)
- [Herdr 文档首页](https://herdr.dev/docs/)
- [安装 Herdr](https://herdr.dev/docs/install/)
- [快速开始](https://herdr.dev/docs/quick-start/)
- [Agent 支持与状态识别](https://herdr.dev/docs/agents/)

## 9. Herdr 如何支持多个异构 agent 协作

Herdr 的关键能力是提供一个本地的 agent 编排层。它不是把 Codex 和 Claude Code 合并成一个 agent，也不会自动把两个窗格的内容互相转发；而是让脚本、终端中的某个 agent，或自定义工具能够控制另一个 agent。

Herdr 官方把能力分成三层：

| 层 | 作用 |
| --- | --- |
| Layout | 创建 workspace、tab、pane，并组织终端布局 |
| Pane | 执行普通命令、发送输入、读取输出、等待终端输出 |
| Agent | 按 agent 名称或 pane 控制已识别的 Codex、Claude 等 agent，并读取 `working`、`blocked`、`done`、`idle` 状态 |

因此，Codex 和 Claude Code 可以保持各自的模型、会话和终端环境，同时通过 Herdr 进行任务分派和结果收集。

### 最常见的协作模式

```text
Codex（编排者）
  ├─ 发送任务给 Claude Code
  ├─ 等待 Claude Code 完成或进入 blocked
  ├─ 读取 Claude Code 的输出
  └─ 根据结果继续实现、修复或汇总
```

反过来也可以由 Claude Code 调用 Codex。真正的“通信”由 Herdr 的 `agent prompt`、`agent read`、`agent wait` 等命令完成。

## 10. 对当前已经打开的两个窗格做协作配置

### 10.1 查看 Herdr 当前识别到的 agent

在任意一个 Herdr 窗格中执行：

```bash
herdr agent list --json
```

先记录输出里的 agent 名称或 pane ID。已经手动启动的 agent 可以直接被 Herdr 检测并控制，不需要重新启动。

如果想使用稳定、易读的名称，可以把两个 pane 重命名为 `codex-main` 和 `claude-reviewer`：

```bash
herdr agent rename <codex-pane-id> codex-main
herdr agent rename <claude-pane-id> claude-reviewer
```

将 `<codex-pane-id>` 和 `<claude-pane-id>` 替换为 `herdr agent list --json` 返回的实际 pane ID。名称必须唯一，只要对应的 agent 仍在运行就可以作为后续命令的目标。

### 10.2 让 Codex 委托 Claude Code 做审查

在任意普通 shell 窗格，或者让 Codex 执行以下命令：

```bash
herdr agent prompt claude-reviewer \
  "请审查当前项目的未提交改动，重点检查功能错误、边界条件和安全风险；不要修改文件，完成后给出问题清单和结论。" \
  --wait \
  --timeout 120000
```

读取 Claude Code 最近的输出：

```bash
herdr agent read claude-reviewer --source recent-unwrapped --lines 120
```

如果只想等待它进入某个状态：

```bash
herdr agent wait claude-reviewer --until idle --timeout 120000
```

### 10.3 让 Claude Code 委托 Codex 做实现或测试

同样可以把目标换成 Codex：

```bash
herdr agent prompt codex-main \
  "请根据当前项目状态实现上一条需求，并运行相关测试；完成后说明修改的文件和测试结果。" \
  --wait \
  --timeout 120000
```

读取 Codex 的输出：

```bash
herdr agent read codex-main --source recent-unwrapped --lines 120
```

## 11. 建议的异构 agent 工作流

适合你当前 Codex + Claude Code 组合的一个安全工作流是：

1. 让 Codex 负责拆解需求、修改代码或担任主协调者。
2. 让 Claude Code 负责独立审查、补测试或检查边界条件。
3. 通过 `herdr agent prompt ... --wait` 明确提交任务，并等待结果。
4. 通过 `herdr agent read ...` 读取结果；不要仅凭 `done` 判断内容正确。
5. 如果目标 agent 进入 `blocked`，先用 `herdr agent read` 查看它需要什么，再用 `herdr agent send-keys` 进行明确的交互。
6. 让主 agent 根据审查结果决定是否继续修改，形成“实现 → 审查 → 修复 → 测试”的循环。

建议让两个 agent 使用不同的职责和工作目录边界，避免同时修改同一个文件造成冲突。涉及写文件、执行命令、提交代码或删除内容时，主 agent 应明确告诉协作者权限范围和是否允许修改。

## 12. 用脚本自动创建协作 agent

如果不想手动分屏，Herdr CLI 可以创建 workspace、tab 和 pane，并在已有 shell pane 中启动 agent。下面是官方文档中的简化模式：

```bash
created=$(herdr workspace create --cwd ~/project --label api --no-focus)
pane_id=$(printf '%s\n' "$created" | jq -r '.result.root_pane.pane_id')

split=$(herdr pane split "$pane_id" --direction right --no-focus)
review_pane=$(printf '%s\n' "$split" | jq -r '.result.pane.pane_id')

herdr agent start reviewer --kind claude --pane "$review_pane"
herdr agent prompt reviewer "审查当前项目并输出问题清单" --wait --timeout 120000
herdr agent read reviewer --source recent-unwrapped --lines 120
```

启动 Codex 时把 `--kind claude` 换成 `--kind codex`。`agent start` 需要目标 pane 处于交互式 shell 提示符；它不会自动替你创建或分割 pane。

你已经手动启动了两个 agent，所以当前更适合使用上一节的 `agent list`、`agent rename`、`agent prompt` 和 `agent read`，无需重新创建会话。

## 13. CLI 与 Socket API 的关系

Herdr 提供本地 Socket API，供脚本、agent 和自定义工具检查或控制正在运行的会话。官方建议：

- 日常协作和 shell 脚本优先使用 `herdr agent ...`、`herdr pane ...` 等 CLI 封装。
- 需要编写自己的编排器、持续订阅事件或直接处理请求/响应时，再使用 Socket API。
- 可以查看当前版本的 API schema：

  ```bash
  herdr api schema --json
  ```

Socket API 覆盖 workspace、tab、pane、agent、集成安装以及事件订阅等能力；agent 可以通过 `agent.prompt` 提交任务，通过 `agent.wait` 等待状态，通过 `agent.read` 读取结果。

## 14. 重要限制与注意事项

- Herdr 提供的是控制和消息传递通道，不负责合并两个 agent 的上下文，也不自动解决代码冲突。
- `agent prompt` 发送的是给目标 agent 的新提示词；它不是把当前 agent 的完整上下文复制过去。
- 目标 agent 如果处于 `blocked`，`agent prompt` 会拒绝继续发送；应该先读取屏幕并有意识地处理审批或问题。
- `idle` 和 `done` 都表示可以继续交互；`unknown` 表示 Herdr 无法可靠判断状态，不等于任务已经完成。
- `agent read` 读取的是目标终端的输出。重要结果最好要求协作者写入 Markdown 文件，再由主 agent 读取文件，避免长输出被终端截断。
- Herdr 的本地 Socket 和 session 数据可能包含提示词、命令输出、token 或其他敏感内容，应按终端历史和本地开发配置一样保护。

## 15. 相关官方文档

- [Agent automation](https://herdr.dev/docs/agent-automation/)
- [Socket API](https://herdr.dev/docs/socket-api/)
- [Integrations](https://herdr.dev/docs/integrations/)
- [Session state and restore](https://herdr.dev/docs/session-state/)
- [Agent skill file](https://herdr.dev/docs/agent-skill/)

## 16. Workspace、Tab、Pane 的关系

可以把 Herdr 的层级理解成：

```text
Session（Herdr server 的运行空间）
└── Workspace（一个项目或任务）
    ├── Tab（该项目的一种工作视图）
    │   ├── Pane（真实终端：Codex）
    │   └── Pane（真实终端：Claude Code）
    └── Tab（另一种工作视图）
        └── Pane（日志、测试或其他 agent）
```

### Workspace

Workspace 是项目级容器，通常一个仓库或一项调查使用一个 workspace。它拥有多个 tab；workspace 的状态可以汇总显示其中 agent 的状态。

从编排角度看，workspace 主要负责：

- 给一组 tab 和 pane 提供项目边界。
- 让同一项目的多个 agent 被放在一起管理。
- 作为创建、移动和查询布局的上层对象。

Workspace 本身不是一个聊天通道，也不会自动把其中 agent 的上下文拼接起来。

### Tab

Tab 是 workspace 内的一种布局或视图。可以按职责组织，例如：

- `agents`：Codex 和 Claude Code
- `tests`：测试、构建和日志进程
- `review`：代码审查 agent

Tab 的主要作用是管理视图和布局，不是隔离通信。Tab 可以被 CLI 和 Socket API 寻址，但 agent 通信最终仍然落到具体 pane 或具体 agent。

### Pane

Pane 是一个真实的终端，里面可以运行 shell、测试服务或 coding agent。Herdr 的 `agent` 实际上是“被 Herdr 识别出的、运行在某个 pane 内的进程”。

从编排角度，pane 是最重要的通信端点：

- `pane run`：在 pane 的 shell 中执行命令。
- `pane send-text` / `pane send-keys`：向终端进程发送输入。
- `pane read`：读取 pane 的终端输出。
- `pane wait-output`：等待 pane 输出匹配文本或正则。
- `agent prompt`：以 agent 语义向 pane 中的已识别 agent 提交任务。
- `agent read` / `agent wait`：读取 agent 输出并等待生命周期状态。

简化地说：

```text
Workspace = 项目边界
Tab       = 工作视图 / 布局分组
Pane      = 真正运行进程、发送输入、读取输出的通信端点
Agent     = Pane 中被 Herdr 识别出来的 coding agent
```

## 17. 同一 Workspace 下不同 Tab 的 Pane 能否通信？

可以。跨 tab 不会阻断 pane 或 agent 通信。

例如下面两个 agent 即使不在同一个 tab，也可以互相编排：

```text
workspace: my-project
├── tab: implementation
│   └── pane: codex-main
└── tab: review
    └── pane: claude-reviewer
```

只要它们属于同一个 Herdr session，并且目标 pane/agent 能被唯一寻址，就可以：

```bash
herdr agent list --json
herdr agent prompt claude-reviewer "审查当前项目的未提交改动，并输出风险清单。" --wait --timeout 120000
herdr agent read claude-reviewer --source recent-unwrapped --lines 120
```

这里不需要先切换到 `review` tab；CLI 和 Socket API 可以直接按 agent 名称或 pane ID 操作目标。pane ID 通常包含 workspace 和 pane 的限定信息，例如 `w1:p2`；如果要区分 tab，应以 `herdr agent list --json` 或 `herdr api snapshot` 返回的记录为准，不要自行猜测 ID。

也可以直接按 pane 操作：

```bash
herdr pane read <review-pane-id> --source recent --lines 80
herdr pane send-text <review-pane-id> "请只输出审查结论"
herdr pane send-keys <review-pane-id> enter
```

不过，对 Codex 或 Claude Code 这类已识别 agent，优先使用 `agent prompt`、`agent read` 和 `agent wait`。它们会检查目标是否仍由该 agent 占用，并按 agent 生命周期处理 `working`、`blocked`、`done` 和 `idle` 状态；`pane` 命令则是更底层的原始终端控制。

### 跨 tab 协作的推荐方式

把 tab 当作职责视图，把 agent 当作协作角色：

1. `implementation` tab 放 Codex，负责主实现。
2. `review` tab 放 Claude Code，负责独立审查。
3. Codex 通过 `herdr agent prompt claude-reviewer ...` 委托审查。
4. Claude Code 完成后，Codex 通过 `agent read` 读取结果。
5. `tests` tab 可以放测试命令或第三个 agent，继续按同样方式被调度。

这种布局上的分离不会影响通信，反而能让人更清楚地看到每个角色的职责。

## 18. 跨 Workspace 或跨 Session 的边界

需要区分两种情况：

- 同一 session 内：workspace、tab、pane 可以由同一个 Herdr server 编排；跨 workspace、跨 tab 的 agent 通信可以按 agent 名称或 pane ID 完成。
- 不同 named session：它们是独立的运行时命名空间，拥有独立的 pane、socket 和持久化状态。不要假设一个 session 中的 `agent prompt` 能直接找到另一个 session 的 agent。

因此，如果目标是让 Codex 与 Claude Code 协作，最简单的做法是让它们处于同一个 Herdr session；是否位于同一 workspace 或同一 tab，不是通信成立的必要条件。
