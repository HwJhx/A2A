# pi 插件 a2a.ts 说明：与各 agent、与 Python 库的关系

插件文件：`herdr/a2a/pi-extension/a2a.ts`。设计细节见 `10-stage6-test-plan.md` §2a。

## 1. 所有 agent 用的是不是同一份插件？

**是同一份。** fnx_dv 和 fnx_sw 用的都是 `herdr/a2a/pi-extension/a2a.ts`，代码完全一样，只是各自复制了一份到自己的 `agent/extensions/` 下：

- `~/.forenyx/fnx_dv/agent/extensions/a2a.ts`
- `~/.forenyx/fnx_sw/agent/extensions/a2a.ts`

(2026-10-10 更新:为避免与 Codex 实现的同名工具冲突,已不再常驻安装;只在测试或真实运行时临时复制进去,结束后还原。)

每个 agent 看到的工具不一样，比如 dv 只能选 `dv_done`，sw 能选 `sw_test_pass` 和 `sw_test_fail`。这个差别不是插件代码造成的，而是运行时决定的：

1. `a2a agent spawn` 启动 agent 时，往它的 pane 里注入环境变量：`A2A_PROJECT_ID`、`A2A_ROLE`（如 `sw`）、`A2A_IP`（如 `uart`）、`A2A_STATE_DIR`、`A2A_TOPOLOGY`、`A2A_PYTHON`、`A2A_SRC`。
2. 插件加载时执行 `a2a edges`。Python 按这些环境变量查拓扑，返回"sw 角色在 uart 上能用哪些边、填好 IP 后的文字是什么"。
3. 插件按返回结果注册 `a2a_send`，工具的可选值（`edge_id` 的 enum）和说明都来自这个结果。

所以要新增角色或 IP，改的是拓扑（YAML），不用改插件。不在 a2a 管理的 pane 里（缺少上述环境变量）时，插件什么都不做，平时使用 fnx 不受影响。

### 另外 3 个基于 pi 的智能体

原则上也能用同一份，前提有三个：

- 它们像 fnx 一样，会从 `agent/extensions/` 自动加载扩展。这在 fnx_dv、fnx_sw 上实测过，另外 3 个还没测。
- 它们的 pi 版本和这里的扩展接口兼容（`pi.registerTool`、`pi.on("tool_call")`）。目前只在 pi 0.79.10（fnx）上验证过。
- 拓扑里给它们声明了角色、启动命令和边。

还有一点：现在是每个 agent 目录各放一份副本，将来改插件容易漏改某一份。可以改成软链接，都指向仓库里的同一个文件。

## 2. 插件和 Python 库是什么关系？

**插件只是一个很薄的转接层，所有逻辑都在 Python 库里。** 插件不 import Python，也不重复实现任何规则，它只是用 Node 的 `execFile` 去运行 Python 的命令行（不经过 shell），从 stdout 读 JSON 结果：

```
模型决定调用 a2a_send(dv_done)
   │
   ▼
插件 a2a.ts（pi 进程里）
   │  execFile(A2A_PYTHON, ["-m","a2a.cli","send","dv_done"])，PYTHONPATH=A2A_SRC，继承 pane 的环境变量
   ▼
Python a2a.cli → Router              ← 鉴权：环境变量里的身份要和注册表、pane 一致
   │                                    填模板：{ip} → uart
   │                                    定目标：同 IP 的对端 sw_uart
   ▼
spool 队列（落盘），返回 JSON 回执 → 插件把回执交还模型
   │
   ▼
broker（独立的 Python 进程）→ 等对端空闲 → herdr agent prompt 把文字送进 sw_uart 的 TUI
```

### 分工

| | 插件（TypeScript） | Python 库 |
| --- | --- | --- |
| 作用 | 让模型能用到 a2a | 规则和投递的全部实现 |
| 做什么 | 注册 `a2a_send` 工具；拦截 bash 里直接调用 herdr 写入的命令（`agent prompt`、`pane send-text/send-keys/run`） | 拓扑、身份核实、注册表、模板、队列、broker 投递、审计、操作员裁定、spawn/stop |
| 调用的命令 | `a2a edges`（加载时，只读拓扑）、`a2a send <edge_id>`（模型调用工具时） | — |
| 怎么找到 Python | spawn 注入的 `A2A_PYTHON`、`A2A_SRC` | — |

### 几个容易误解的点

- **插件只管"发"，不管"收"。** 接收方收到消息，是 broker 用 `herdr agent prompt` 把文字打进对方的 TUI，在模型看来就像有人输入了一段话。接收方的插件不参与接收。
- **插件绕不过 Python 的检查。** 它和在终端里手动执行 `a2a send dv_done` 走的是同一条路径。就算插件被改坏，或者有人伪造了环境变量，Router 照样会核对注册表，拒绝并记审计。真正的安全边界在 Python 这边；插件的 bash 拦截只是防止模型"图省事"的软限制。
- **`a2a edges` 不授予权限。** 它只读拓扑、不核对注册表（插件加载时 agent 还没登记），只用来生成工具说明；真正发送时 `send` 仍做完整鉴权。
- **这样分的好处：** 规则只有一份实现。以后接入不是 pi 的 agent（比如 Claude Code、Codex），给它写一个同样很薄的转接层，或者直接让它调用 `a2a send` 命令行就行，Python 这边不用改。
