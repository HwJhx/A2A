# 阶段 8 修复方案:插件主动向 herdr 上报 agent 状态(草案,待用户确认、Codex 审核)

## 1. 问题

真实 fnx 规模测试 A 档(报告 14 号)发现:同一角色的多个 IP 放在一个 tab 里时,后面的 pane 很小(只剩 2–4 行),herdr 靠屏幕识别 working 的规则看不到 "Working" 那一行,broker 投递后观察不到目标进入 working,只能按协议判为 `DELIVERY_UNCERTAIN`。5 个 IP 里 6 次投递因此"不确定",链路停下等操作员裁定。正式使用约 20 个 IP 时会更严重。

## 2. 方案(Codex 审核后修订)

在已有的 a2a 插件(`pi-extension/a2a.ts`)里增加状态上报。调用的命令是
`herdr pane report-agent $HERDR_PANE_ID --source a2a --agent pi --state <working|idle> --seq <n>`。

### 2.1 上报时机(依据 pi 0.79.10 源码;Codex 第三轮复核后改为不依赖计时)

原则:**只在确定 pi 已经结束时报 idle;不确定时保持 working**。宁可让 broker 等到超时、告警、交给人处理,也不把消息投给还会继续处理的 agent。

- pi 会**等**插件的事件处理完成(`agent-session.ts:616/618`),所以上报要短、有上限。
- **`agent_start`:上报 working,并等它完成**(单次超时 1 秒,失败再试 1 次,合计约 2 秒),明显短于 broker 的 5 秒观察窗口;先上报完再让模型开始,不竞速。herdr 命令本身只要几毫秒。
- **`agent_end`(Codex 第四轮复核后修订:白名单 + 与 pi 相同的数据):** 只看最后一条 assistant 的 `stopReason`,只认白名单:
  - `aborted`(被中断):报 idle。pi 不重试;结束后的压缩检查跳过 aborted(`_checkCompaction` 的 `skipAbortedCheck` 默认 true,`agent-session.ts:1816-1821`)。
  - `stop`(正常结束):用**最后一条 assistant 的 usage**、按 pi 相同的公式(`calculateContextTokens`:`totalTokens`,或 `input + output + cacheRead + cacheWrite`,`compaction.ts:136-138`)算上下文 token,窗口取 `ctx.getContextUsage().contextWindow`(即当前模型的 contextWindow,与 pi 判断时相同);**低于 (窗口 − 预留) × 90% 时报 idle**,否则保持 working。pi 在 stop 后正是用这份数据判断是否压缩(`agent-session.ts:1897-1899`,`shouldCompact`:`token > 窗口 − 预留`)。**预留读实际设置**(Codex 第五轮复核):pi 读全局 `<agent 目录>/settings.json` 与项目 `<cwd>/<配置目录>/settings.json` 的 `compaction.reserveTokens`(默认 16384);agent 目录来自环境变量 `<应用名>_CODING_AGENT_DIR`(fnx 为 `FORENYX_CODING_AGENT_DIR`)。插件读全局与项目(`.forenyx`、`.pi` 两种目录名)设置,保守地取所有值与默认值中的最大者;找不到 agent 目录、设置文件读不出或值不合法时保持 working。usage 或窗口缺失时也保持 working。
  - 其他(`error`、`length`、`toolUse`、未知、缺失):**保持 working**。`error` 时 pi 可能自动重试,重试会再次 `agent_start`,最终 `stop` 时再判断;重试用尽或其他情况会一直 working,broker 300 秒判 `TIMEOUT`、暂停并告警,由人按 §2.2 恢复。
  - 不压缩时,`_handlePostAgentRun` 之后只处理"插件在 agent_end 里排进来的消息"(`agent-session.ts:983-985`)。**部署约束**:a2a 管理的 fnx 只加载本插件,不加载其他会在 `agent_end` 里排消息的扩展;真实测试脚本在发提示词前检查每个 fnx 的扩展列表只有本插件。
- **防御**:万一仍发生压缩,`session_before_compact` 时上报 working;`session_compact` 时不报 idle,等之后的 `agent_end` 按上面的规则判断。
- **排队的第二条输入**:pi 在同一次处理里先处理完排队的消息才发 `agent_end`(源码注释 `agent-session.ts:983`),不会中间报 idle。
- **残余窗口**:非 error、低用量时,`agent_end` 处理完后 pi 还要执行 `_handlePostAgentRun` 的几步判断(不调用模型、不等待外部),是微秒级;broker 从看到 idle 到 prompt 写入要经过 herdr 命令(百毫秒级),不会落在这个窗口里。

### 2.2 失败与恢复(fail-closed;Codex 审核更正)

- `working` 上报失败:herdr 仍按屏幕识别;小 pane 下投递可能判为不确定,等操作员裁定。不会重复投递。
- `idle` 上报失败,或模型出错后不再重试:herdr 会一直认为它在 working(上报的状态不会被屏幕识别覆盖)。broker 等 300 秒判 `TIMEOUT`、暂停该队列并告警。不会误投,但这个目标会卡住。
- 插件在上报全部失败时,在 fnx 控制台打印醒目的错误。
- **恢复方式:重启这个 agent**(`a2a agent stop <role> <ip>` 再 `a2a agent restore <role> <ip>`),再按已有流程用 `a2a resolve` 处理被暂停的消息。已验证:fnx 退出后 herdr 不保留上报的状态,同一 pane 里重启的新进程从序号 1 上报仍被接受。**不建议手工补报状态**:手工用的序号和插件进程内的计数会脱节,之后插件的上报可能被 herdr 忽略(Codex 复核指出)。README 写明。

### 2.3 其他

- **序号**:每个 fnx 进程内单调递增(从 1 开始)。已验证:同一 pane 重启后新进程从 1 开始上报仍被接受。
- **herdr 程序**:spawn 额外注入 `A2A_HERDR_BIN`(lifecycle 进程里 `herdr` 的绝对路径),插件用它调用,不依赖 fnx 进程的 PATH。pane 里本来就有 `HERDR_PANE_ID` 和 `HERDR_SOCKET_PATH`,能连到正确的会话。
- **只在 a2a 管理的 pane 里生效**:沿用插件现有的判断。
- **不改 broker、Router 和协议**:broker 仍用 `agent prompt --wait --until working` 观察。

## 3. 已有的探测结论(真实 fnx、2 行可见的 pane、不调用模型)

| 验证项 | 结果 |
| --- | --- |
| 上报 working | 状态立即变 working;3 秒后仍是 working,不被屏幕兜底覆盖 |
| 上报 idle | 状态变 done(broker 视为可投递) |
| fnx 退出后 | 0.5 秒内 herdr 里找不到该 agent,上报状态不残留,`agent stop` 不受影响 |
| 同一 pane 重启 | 新进程从序号 1 上报仍被接受(`restore` 不受影响) |
| `release-agent` | 带 `--agent` 时正常执行 |

还没验证、需要在实现后测的:
- 真实处理时 `agent_start` 是否足够早:broker 的 `agent prompt --wait` 只等 5 秒,上报必须在这之前到达。
- `agent_end` 之后 herdr 显示 done,下一条消息投递时 broker 能否正常观察到新的 working。
- 主动上报和 herdr 屏幕规则同时存在、且 pane 很大能看到 "Working" 时,两者是否冲突。

## 4. 测试

1. **插件单元测试**(Mac,node,假 pi + 记录调用的假 herdr 程序):
   - `agent_start` 上报 working 并在 handler 返回前完成;假 herdr 卡住时 handler 约 2 秒内返回、不抛异常;
   - `agent_end`:`stop` 且 token(totalTokens 或四项之和)低于 (窗口 − 16384) × 90% 时报 idle;达到阈值、没有 usage、窗口未知时不报;`aborted` 报 idle;`length` / `toolUse` / 未知 / 缺失不报;
   - `agent_end`(`error`)不报 idle;之后的 `agent_start` 报 working、成功的 `agent_end` 报 idle;
   - `session_before_compact` 报 working;`session_compact` 不报 idle;
   - 序号在进程内严格递增;上报全部失败时打印错误、不抛异常;不在 a2a pane 里时不上报。
2. **真实 fnx 集成测试**(VM,不调用模型):插件加载后,确认注入了 `A2A_HERDR_BIN`,并在小 pane 里从外部检查 herdr 状态在启动后仍正常(不发提示词)。
3. **真实模型复测**(须用户同意):用同一个 `real_fnx_scale.py` 重跑 A 档(5 个 IP、约 25 次调用),同样的布局。预期:不再出现 `DELIVERY_UNCERTAIN`。报告里给出每次投递从发出 prompt 到观察到 working 的时间,确认远小于 5 秒。
4. 模型接口出错、自动重试、上下文压缩这几条路径无法用真实模型稳定触发,只靠第 1 步的单元测试覆盖,报告里写明。
5. 通过后再请用户决定是否跑 B 档(20 个 IP、约 100 次调用)。

## 5. 附带收益(本次不做)

spawn 时每个 agent 要等约 3 秒让 herdr 把 `unknown` 判为 idle。插件也可以在 `session_start` 时上报一次 idle,把这 3 秒省掉。但这要确认 fnx 在 `session_start` 时已经能接收输入,否则提示词可能丢失。先不做,列为将来考虑。

## 6. 范围

- 改动:`pi-extension/a2a.ts`(上报)、`lifecycle.py`(注入 `A2A_HERDR_BIN`)、对应测试、文档。
- 不改 broker、Router、协议。
- Codex 的实现(`a2a_codex`)经 Codex 确认也受同一问题影响(依赖 herdr 状态观察,没有主动上报;默认观察窗口 30 秒只降低误判概率),是否修改由 Codex 自己决定。

## 7. 已知局限(用户确认接受,2026-10-11)

pi 的插件接口没有"本次输入的处理已全部结束"的信号,以下边角情况只能缩小、不能彻底消除:
- 其他扩展在 `agent_end` 里排消息导致续跑:靠部署约束(只加载本插件)排除。
- 上下文接近压缩阈值(≥ 阈值的 90%)、或出现非白名单的结束原因时保持 working:目标会卡住,broker 超时告警,需重启 agent。宁可卡住,不误投。
- `stop` 结束后 pi 还执行 `_handlePostAgentRun` 的几步判断(微秒级,不调用模型);broker 从看到 idle 到 prompt 写入要经 herdr 命令(百毫秒级)。
- 预留值同时读项目目录下的 `.forenyx/` 与 `.pi/` 设置并取较大值;当前 fnx 只读 `.forenyx/`,若存在不生效的 `.pi/settings.json` 且预留更大,agent 会更早保持 working、最终超时。这是保守方向的误报,不会误投(Codex 复核:将来考虑,可改为只读实际生效的配置目录)。
- 对照修复前:小 pane 里 herdr 看不到任何屏幕信号,一律判为 idle(包括模型正在处理时)。即使发生误投,pi 也会把输入排队、处理完当前的再处理;broker 对同一目标一次只投一条,不会重复、不会乱序。
