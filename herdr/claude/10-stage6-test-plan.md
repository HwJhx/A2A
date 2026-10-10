# 阶段 6 测试方案:真实 fnx_dv ↔ fnx_sw 双向通信(草案,待用户确认)

## 1. 目标

用**真实的 fnx_dv 和 fnx_sw**,**调用模型**,跑通一次双向通信。两边都**不真的干活**:dv 不做 UVM 验证,sw 不做驱动和 HAL 开发。只验证三件事:
- 模型会调用插件注册的 `a2a_send` 工具;
- 消息能按拓扑送到对端;
- 对端能按消息内容做出正确回应。

阶段 6 包含两部分:
- pi 侧接入:`AGENTS.md`、`a2a_send` 插件、拦截直接调用 herdr 写入类命令;
- 真实模型链路:原计划放在阶段 7,现在并入阶段 6。

原因是,不用真实模型就无法证明模型会调用 `a2a_send`。

测试 IP:**uart**。agent 两个:`dv_uart`(fnx_dv)和 `sw_uart`(fnx_sw)。

## 2. 测试用拓扑与固定消息模板(仅测试阶段)

| 边 id | 方向 | 模板 |
| --- | --- | --- |
| `dv_done` | dv → sw | `{ip} ip 我已经完成了uvm验证，你需要对这个{ip} ip进行 驱动程序开发和HAL框架开发。不要真的去做，直接用 a2a_send 回复测试成功或测试失败（二选一）` |
| `sw_test_pass` | sw → dv | `我已经完成了{ip} ip的驱动程序开发，测试成功。你只回复收到即可，不要真的去做` |
| `sw_test_fail` | sw → dv | `我已经完成了{ip} ip的驱动程序开发，测试失败。你只回复收到即可，不要真的去做` |

"不要真的去做"之类的约束直接写进模板,不依赖 AGENTS.md。正式使用时,模板换成真实的工作指令。

注意:模板里的约束只是给模型的提示,**不能**阻止模型改文件(`tool_call` 拦截只针对 herdr 写入命令)。真正的限制靠第 3 节第 1 步的工具限制和临时工作目录。

## 2a. a2a_send 插件的实现与调用方式

- **注册**:插件用 `pi.registerTool()` 注册 `a2a_send`,参数只有 `edge_id`,取值限定为该角色在拓扑里允许的边。
- **执行**:插件**不自己实现鉴权和路由**,只用 Node 的 `execFile("a2a", ["send", edge_id])` 调用 Python Router 的命令行。不经过 shell,参数只有边 id,继承 fnx 进程的环境变量。fnx 在 pane 里启动,所以 `A2A_PROJECT_ID` / `A2A_ROLE` / `A2A_IP` / `A2A_STATE_DIR` / `HERDR_SESSION` / `HERDR_PANE_ID` 都在。鉴权、填模板、确定目标、入队都由 Router 完成,与命令行 `a2a send` 完全同一条路径。
- **返回**(Codex 审核后修订):结果分三类。
  - 发送成功:Router 的 JSON 回执(msg_id、目标、文字)作为工具结果交还模型。
  - 明确拒绝(退出码 4 + Router 的拒绝 JSON):入队之前被拒,消息确定没有发出,按工具错误返回原因。
  - 结果未知(超时、python 出错、回执看不懂、入队后写审计失败等):Router 先入队再写审计,消息可能已经发出。**不按工具错误返回**,而是告诉模型不要再次调用、停止本次自动流程、告诉用户,并给出操作员的核查方式(`a2a queue <目标>`、审计日志里的 src / edge_id / 时间)。
  - 不把工具的取消信号传给发送子进程:发送通常不到 1 秒,中途杀掉只会把确定结果变成"未知";代价是按 Esc 不能取消已经开始的一次发送。30 秒超时作兜底。
  - 这只降低模型重试导致重复投递的概率,不是机制级保证。toolCallId 不能做幂等键:模型重试是新的工具调用、ID 也是新的。机制级保证需要稳定的业务事件 ID,列为将来考虑。
- **调用方**:由**模型**在回复过程中自己决定调用,与调用 bash、read 一样;不是人在界面里输入。TUI 里显示为一个工具调用块(`a2a_send dv_done` 及结果)。
- **与内置工具的关系**:`--no-builtin-tools` 只关掉模型能用的内置工具(read/write/edit/bash 等),不影响插件在自己代码里启动子进程(pi 文档 `extensions.md`:关掉内置工具、保留扩展工具)。因此测试时模型只有 `a2a_send` 一个工具。
- **拦截**:`tool_call` 钩子拦截 agent 自己通过 bash 等工具调用 herdr 写入类命令(`herdr agent prompt`、`herdr pane send-*`、`herdr pane run`)。这是正式使用(开放内置工具)时的防线;测试时没有 bash,也就不会出现这种绕过。

以上基于 pi 原版文档;fnx 是定制版,插件能否从 `agent/extensions/` 自动加载、`--no-builtin-tools` 是否可用,要在第 3 节第 1 步实测。

## 3. 测试流程

1. **准备(不调用模型)**
   - 用 `a2a agent spawn` 启动 `dv_uart` 和 `sw_uart`:注入身份环境变量(含 `A2A_STATE_DIR`,保证 agent 与 broker 用同一个状态目录)、登记注册表、把 agent 改名为 agent_id。
   - **限制模型的能力**:fnx 以 `--no-builtin-tools` 启动,模型只剩 `a2a_send`,不能读写文件、不能执行命令;工作目录用可丢弃的临时目录,作为第二道保险。(需要给 spawn 增加"传启动参数"的能力)
   - 启动 broker。
   - 确认 fnx 加载了 a2a 插件,`a2a_send` 已注册;拦截生效,即直接执行 `herdr agent prompt` 之类的写入命令会被拦下。
2. **发起(唯一一次人工输入)**
   - 操作员用 `herdr agent prompt` 向 fnx_dv 输入:`假设你已经完成了 uart ip 的 UVM 验证，请用 a2a_send 工具通知软件智能体。`
   - 这句话是启动测试用的,不经过 a2a。
   - 不能改为在 pane 里执行 `a2a send` 命令行:那样会绕过插件,测不到工具。工具只能由模型调用。
3. **dv → sw**
   - fnx_dv 的模型调用 `a2a_send(dv_done)`。
   - Router 鉴权、填模板,broker 投递给 `sw_uart`。
4. **sw → dv**
   - fnx_sw 收到后不做开发,调用 `a2a_send(sw_test_pass)` 或 `a2a_send(sw_test_fail)`,二选一。
   - fnx_sw 不需要人工输入:收到的固定消息就是它的输入。
5. **dv 收尾**
   - fnx_dv 收到后只输出"收到",不做别的。

## 4. 判定通过

- 审计日志里能看到完整的两次投递:`dv_done` 送达 `sw_uart`,`sw_test_pass` 或 `sw_test_fail` 送达 `dv_uart`,各一次。
- 两条消息的文字都是模板填入 `uart` 后的结果。
- 用 `herdr agent read` 读 fnx_dv 的 pane,最后的回复是"收到"。
- agent 没有自己(通过 bash 或其他工具)绕过 broker 调用 herdr 写入类命令;如果尝试,应当被拦截并有记录。操作员在第 2 步从外部输入开头提示,属于测试步骤,不算违规。
- 两边都没有改动任何文件(临时工作目录保持为空)。

## 5. 关于多 IP 的担心:模板不用按 IP 重复声明

问题:一个项目有很多 IP,例如 30 个,是否要为每个 IP 声明一条消息?

**不需要。每条边只声明一次模板,`{ip}` 在发送时自动填。**

拓扑里 IP 列表和边是分开配置的:

```yaml
ips: [uart, gpio, spi, ...]        # 30 个 IP 写在这里
edges:
  - id: dv_done                    # 每条边只写一次
    from: dv
    to: sw
    template: "{ip} ip 我已经完成了uvm验证,……"
```

agent 发送时只给出边 id,例如 `a2a_send(dv_done)`。剩下的由 Router 完成:

1. **确认发送方身份**
   - 从 pane 的环境变量 `A2A_PROJECT_ID` / `A2A_ROLE` / `A2A_IP` 读取。这些变量在 spawn 时注入。
   - 再与注册表核对,防止冒充。
2. **填充 `{ip}`**:用发送方自己的 IP。`dv_uart` 发出"uart ip ……",`dv_gpio` 发出"gpio ip ……"。
3. **确定接收方**:取同一 IP 的对端角色。`dv_uart` 的 `dv_done` 只会发给 `sw_uart`。

所以 30 个 IP 共用一条 `dv_done`,各自填各自的 IP、发给各自的对端。新增 IP 只需要 `a2a topology add-ip <ip>`,不用改模板。

这也是安全设计的一部分。agent 的接口里**没有**目标、IP、自由文本这些参数,因此:
- 不可能跨 IP 发送(同 IP 铁律);
- 不可能发模板以外的内容。

已有验证:
- 阶段 5e 的端到端测试,在真实 pane 中执行 `a2a send dv_done`,接收方收到的是填入 `uart` 后的文字;冒充身份的发送被拒绝。
- 单元测试覆盖了多 IP:同一条边从不同 IP 发出,填入的 IP 和接收方都不同。
- 以上还没有在真实 fnx 上验证,阶段 6 用 uart 实测。

## 6. 覆盖范围

- 真实模型链路只会走 `sw_test_pass` 和 `sw_test_fail` 中的一条,另一条不会被真实模型覆盖。
- 两条边的鉴权、填模板、投递由不调用模型的集成测试覆盖(命令行发送 + 假 agent)。
- 是否用真实模型再跑一次以覆盖另一条,由用户决定。

## 7. 约束

- 模型调用只在第 3 节第 2 至 5 步发生,而且已经得到用户同意。
- 扩展文件放在 `~/.forenyx/fnx_dv/agent/extensions/` 和 `~/.forenyx/fnx_sw/agent/extensions/`。这一点要先实测:fnx 是否从这里自动加载扩展。
- 只往 fnx 目录里加外部文件,不改 fnx 的二进制、启动脚本和 libexec。
- 不动用户的 herdr 会话(default、test1、a2a_verify)和 Codex 的 a2a_codex_verify,使用独立的测试会话。
