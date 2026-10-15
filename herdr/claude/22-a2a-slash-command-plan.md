# 22. 插件斜杠命令 `/a2a <边>`:手动发送,不经过模型(方案)

2026-10-14。背景见 21 号文档 §12:没有插件时用户输入 `/a2a dv_done`,pi 把它当普通提示词交给模型,模型翻文件、用 bash 调 a2a 命令行,约 45 秒、多次模型调用;装了插件后模型能直接调 `a2a_send`,但仍要调用模型(还碰到过模型服务超时)。目标:用户在 agent 终端里输入 `/a2a dv_done`,由插件直接调用 Router 发送,**不调用模型、不耗 token**。

## 1. pi 0.79.10 的相关行为(已在 fnx 自带代码与文档里核对)

- `pi.registerCommand(name, { description, getArgumentCompletions?, handler(args, ctx) })` 注册斜杠命令;`getArgumentCompletions(prefix)` 返回补全项或 null(类型上也允许返回 Promise)。
- 输入以 `/` 开头时,`AgentSession.prompt()` **最先**查扩展命令,命中就执行 handler 并返回,不进入模型、不触发 `before_agent_start` / `agent_start`(所以不会上报 working / idle,也不会换幂等键)。
- `ctx.ui.notify(msg, "info" | "warning" | "error")`:在聊天区追加一行(info 灰色、warning 黄、error 红);**不写入会话,不进入模型上下文**。连续两条 info 时后一条替换前一条。
- 同名命令由多个扩展注册时 pi 都保留,并加后缀 `/a2a:1`、`/a2a:2`(所以插件被装两份时命令也会变两份——与 21 号 §12 的防重复安装一致)。

## 2. 行为

| 输入 | 结果 |
| --- | --- |
| `/a2a`(无参数) | 列出本角色当前可用的出边:`edge_id → 接收方:内容`(info);没有出边时提示"没有可用的通信边"(warning) |
| `/a2a <边>`,边在出边里 | 立即发送,显示结果(见下) |
| `/a2a <边>`,边不在出边里 | error:"没有这条边",并附上可用边列表;**不发送** |
| `/a2a a b`(多个参数) | error:用法说明;不发送 |
| 不在 a2a 管理的 pane 里 | 不注册命令(与 `a2a_send` 一样,插件在普通 fnx 里什么都不做) |

发送结果与 `a2a_send` 用同一个 `runSend`(同样的三分类),只是显示给人看:

- 已发送:info `已发送给 <dst>(msg_id=…):「内容」`
- Router 拒绝(确定没发):error `已拒绝,消息没有发送:<code>: <reason>`
- 结果未知(可能已入队):warning `发送结果未知……请用 a2a queue <dst> 或审计日志核实,不要直接重发`

## 3. 设计取舍

- **出边以发送时为准**:handler 每次执行都现读 `a2a edges`(与工具的 refresh 同一个函数),拓扑改了马上生效;Router 仍会完整鉴权,插件只是提前给出友好提示。
- **补全用缓存**:`getArgumentCompletions` 每敲一个字都会调用,为避免每次都启动一个 Python 子进程(百毫秒级),用最近一次读到的出边(加载时、每轮 refresh、每次执行 `/a2a`)做补全。缓存过期只影响补全提示,不影响发送。
- **幂等键**:手动发送是用户的明确操作,每次执行都应该真的发;不带幂等键(若带模型当轮的键,用户在模型发过同一条边之后再手动发会被当成重复而不发)。
- **不告诉模型**:提示不进模型上下文。模型下一轮不知道用户手动发过——这是"手动发送"的本意;对方的回复照常由 broker 投递进来,模型会看到回复。
- **模型工作中也能用**:命令在 `prompt()` 最开始执行,不排队等模型(实测确认)。
- **不加确认对话框**:命令本身就是明确的意图;误发的代价与模型调 `a2a_send` 相同(Router 约束不变)。

## 4. 改动范围

- `herdr/a2a/pi-extension/a2a.ts`:新增 `registerSendCommand(pi, cfg, list, cache)`(或在 `makeSendTool` 旁边),导出纯函数 `parseCommandArgs`、`formatOutcomeForUser`、`completions` 便于单元测试;默认导出里注册命令,出边缓存与工具 refresh 共用。
- 单元测试(`node --test`,沿用现有 `tests/` 里插件测试的写法):无参数列边 / 无出边、合法边发送(假 CLI 回执)、非法边不调用 CLI、多参数、三种结果的提示与级别、补全过滤、缓存更新、发送不带幂等键。
- 真实 fnx 集成(VM,不调用模型):在临时项目里起 broker 与两个 agent,向发送方 pane 输入 `/a2a <边>` 回车,验证:接收方队列 / 审计里有消息、发送方用量仍为 0.0%(没调用模型)、提示行出现;非法边不入队。
- 文档:`herdr/a2a/README.md` 插件一节、`gui_demo/验收步骤.md` 加一步手动发送。
- 版本:改完后发 `a2a-plugin-v0.2.0` 标签,VM 上 `fnx_dv / fnx_sw install …@a2a-plugin-v0.2.0` 升级。

## 5. 待确认

1. 命令名用 `/a2a`(你之前输入的就是它)。
2. 发送前**不**弹确认框。
3. 手动发送**不**通知模型(只在终端显示一行)。

## 6. Codex 审核(2026-10-14)与修订

| # | 级别 | 问题 | 判断 | 修订 |
| --- | --- | --- | --- | --- |
| 1 | 阻断 | 命令绕过 `agent_start`,等 Router 期间不报 working,herdr 仍认为发送方 idle,broker 可能往同一 pane 投递;模型运行中开始的命令,`agent_end` 可能先报 idle | **成立,但后果比"同时接收"窄**。核对 pi 0.79.10 交互模式:空闲时提交走 `pendingUserInputs` → 主循环 `while(true){ input = await getUserInput(); await session.prompt(input) }`;命令在 `prompt()` 里执行,**执行期间主循环被占住**,broker 打进来的输入排队,命令结束后才处理——不会丢、也不会并发处理,但若命令拖得比 broker 的观察窗口(5 秒)长,broker 观察不到 working → `DELIVERY_UNCERTAIN`(实际已投递)。模型运行中提交时走 `isStreaming` 分支 `prompt(text, {streamingBehavior:"steer"})`,命令同样立即执行但**不占主循环**;此时 `agent_end` 报 idle 后 broker 投递的消息会被正常处理,命令只是一个独立的子进程,不冲突。 | 命令开始时若 `ctx.isIdle()`(主循环被本命令占住):**先上报 working(与 agent_start 相同,等完成,最多 2 次)再发送;结束时上报 idle(最多 3 次)再返回**。herdr 按序号丢弃旧上报,序号在调用时同步递增:本命令的 idle 一定早于之后排队输入触发的 `agent_start` working。若开始时模型在运行:不上报,状态由模型的 `agent_start` / `agent_end` 负责。working 上报失败仍发送(退回屏幕识别,最坏 `DELIVERY_UNCERTAIN` 交操作员);idle 上报失败按现有规则打印醒目错误(broker 会等到 TIMEOUT)。 |
| 2 | 阻断 | 入站模板以 `/a2a` 开头会被接收方 pi 当命令拦截 | **成立,且是已有问题、范围更大**:broker 把填好的模板原样 `agent prompt` 进 pane,pi 交互模式先 `trim()`,再按 `/quit`、`!`/`!!`(直接执行 bash)、扩展命令(`/a2a`、`/skill:` 等)处理——与本插件无关,已有拓扑里只要模板以 `/` 或 `!` 开头就会在接收方执行命令。 | **拓扑校验拒绝**去掉开头空白后以 `/` 或 `!` 开头的模板(`topology._validate_template`;创建、修改、加载都经过它)。`{ip}` 填入的 IP 名字由 `_SLUG` 限定以小写字母开头,不会造成这种开头。已有拓扑若有这种模板会加载失败(fail-closed),目前验收工程没有。不提供转义:模板是固定通知句式,没有以命令开头的必要。 |
| 3 | 应修 | `getArgumentCompletions` 类型允许 Promise,"接口同步"的说法不准 | 成立 | §1、§3 已改:缓存的理由是避免每次补全都起 Python。 |
| 4 | 应修 | 测试需覆盖:模型运行中执行命令、命令等待期间 broker 入站、状态协调、模板以 `/a2a` 开头 | 成立 | 见 §7。 |

## 7. 测试(修订)

- 插件单元测试(`node --test`):§4 原列各项;加上状态协调——空闲时:working 上报完成后才调用发送、发送结束后报 idle 才返回、working 失败仍发送、idle 失败打印错误;模型运行中(`isIdle()` 为 false):不上报;没有 `HERDR_PANE_ID` 时不上报。用假的 reporter 记录调用顺序。
- 拓扑单元测试:模板以 `/`、`!`、`  /a2a x`、`\n!ls` 开头被拒(创建、`set_template`、加载已有文件);中间含 `/`、`!` 的正常模板通过。
- 真实 fnx 集成(VM,不调用模型):
  1. 空闲时 `/a2a <边>`:接收方收到、发送方用量 0.0%、提示行出现;用一个慢的假 `A2A_PYTHON` 包装(在真实 send 前 sleep 几秒)让命令拖长,期间查 `herdr agent get` 为 working,结束后回到 idle。
  2. 命令拖长期间,另一个 agent 向发送方发消息:broker 显示等待目标(WAITING_TARGET),命令结束后才投递,最终 DELIVERED(不是 UNCERTAIN)。
  3. 非法边、多参数:不入队,error 提示。
  4. 模型运行中执行命令:需要模型在跑,**会调用模型**,单独征得同意后再做;否则只做单元测试覆盖。
- 拓扑:`a2a topology` 相关命令设置以 `/a2a` 开头的模板被拒(真实 CLI)。

## 8. Codex 第二轮复核(2026-10-14)与再修订

| # | 级别 | 问题 | 判断 | 修订 |
| --- | --- | --- | --- | --- |
| 1 | 阻断 | 命令上下文的 `ctx.isIdle()` 只是 `!session.isStreaming`,压缩期间也为 true(交互模式在 `isCompacting` 时也立即执行扩展命令);命令结束报 idle 会覆盖 `session_before_compact` 的 working | 成立 | 不再用 `ctx.isIdle()`。改为插件内统一的就绪判定(见下)。 |
| 2 | 阻断 | 模型运行中开始的命令,模型可能先结束、`agent_end` 报 idle,而命令仍在途 | 成立(后果是 broker 在命令在途时投递;主循环此时不被命令占住,一般能正常处理,但这样就绪的定义不一致) | 同上:命令在途时 `agent_end` 只记下"可以 idle",等命令结束再报。 |
| 3 | 应修 | 拓扑加载严格校验:已有拓扑若有以 `/`、`!` 开头的模板,新进程启动失败,`set-template` 也修不了(构造 store 时就失败);热重载与冷启动行为不同 | 成立 | 改为**写入时校验 + 发送时拒绝**,加载不校验此项:`create` / `add_edge` / `set_template` 拒绝这类模板;Router 填好模板后若去掉开头空白以 `/` 或 `!` 开头,**拒绝发送**(退出码 4,`rejected: unsafe_template`,入队之前,消息确定没发)。旧拓扑照常加载,可用 `set-template` 修正;冷启动与热重载一致。 |
| 4 | 应修 | 测试要有确定性时序:压缩中执行命令不能过早报 idle;模型运行中开始命令、模型先结束时不能在命令在途时报 idle | 成立 | 见下"测试"。 |

### 8.1 统一的就绪判定(插件内)

插件维护两项状态,只在下面列出的时机改变:

- `quiet`:**模型这一侧确定已结束**。`agent_start`、`session_before_compact` 置 false(并照旧上报 working);`agent_end` 且 `idleIsCertain(...)` 为真时置 true;压缩结束不置 true(沿用 16 号方案:等之后的 `agent_end` 判断)。初始为 true(fnx 刚启动时空闲)。
- `inflight`:正在执行的 `/a2a` 命令数。

上报规则:

- `/a2a` 开始:`inflight += 1`;从 0 变 1 时**先上报 working(等完成,最多 2 次)再发送**。
- `/a2a` 结束(无论结果):`inflight -= 1`;变 0 且 `quiet` 为真时上报 idle(最多 3 次)。
- `agent_end`:判定为确定结束时置 `quiet = true`;`inflight == 0` 才上报 idle,否则推迟到最后一条命令结束时。
- idle 上报的**每次重试前**重新检查 `quiet && inflight == 0`,不满足就放弃(避免重试的 idle 序号晚于新的 working)。working 不受此限。
- 序号仍在每次调用 herdr 时同步递增;herdr 丢弃序号更旧的上报。

覆盖的情形:空闲时命令(working → 发送 → idle);压缩中命令(`quiet` 为 false,命令结束不报 idle);模型运行中开始命令、模型先结束(`agent_end` 推迟,命令结束时报 idle);命令在途时模型又开始(`agent_start` 置 false,命令结束不报 idle)。

### 8.2 测试(再修订)

- 插件单元测试:把就绪判定写成不依赖 pi 的小状态机(`makeReadiness(reporter)`,方法 `agentStart / agentEnd(certain) / compactStart / commandStart / commandEnd`),用假 reporter 和可控的 Promise 确定性地排列时序,断言上报序列:
  1. 空闲:命令 → [working, idle],且发送发生在 working 完成之后;
  2. 压缩中命令:compactStart → 命令开始/结束 → 只有 working,没有 idle;之后 agentStart/agentEnd(certain) → idle;
  3. 模型运行中开始命令、模型先结束:agentStart → commandStart → agentEnd(certain) → 此时无 idle → commandEnd → idle;
  4. 命令在途时模型又开始:commandStart → agentStart → commandEnd → 无 idle;
  5. 两条命令重叠:只在最后一条结束时报 idle;
  6. idle 重试期间状态变化(第一次 idle 失败后 agentStart)→ 不再重试 idle;
  7. agentEnd(不确定)→ 无 idle,命令结束也不报 idle。
- 现有状态上报测试改为经过同一状态机,行为不变。
- 拓扑 / Router:写入类操作拒绝 `/`、`!`、`  /a2a`、`\n!ls` 开头的模板;**加载含此类模板的旧拓扑成功**,`set-template` 能修正;发送这样的边被拒(退出码 4、`unsafe_template`、队列与审计里没有入队记录)。
- 真实 fnx 集成(VM,不调用模型):同 §7 第 1–3 项;第 2 项(命令拖长期间 broker 入站)验证投递等到命令结束、结果 DELIVERED。

## 9. Codex 第三轮复核(2026-10-14)与再修订

| # | 级别 | 问题 | 判断 | 修订 |
| --- | --- | --- | --- | --- |
| 1 | 阻断 | 模板检查用 Python `lstrip()` 时 `﻿` 不会去掉,而 pi 提交时 JS `text.trim()` 会去掉 → `﻿!…` 绕过检查后在接收方执行 bash | 成立(Python `str.isspace()` 不含 U+FEFF;JS `trim()` 去掉 WhiteSpace + LineTerminator,含 U+FEFF) | 检查**渲染后的完整消息**:先按 JS `trim()` 的字符集去掉开头空白——`\t \n \v \f \r`、空格、U+00A0、U+FEFF、U+1680、U+2000–U+200A、U+2028、U+2029、U+202F、U+205F、U+3000——**再并上** Python `isspace()` 的字符,反复去掉直到开头不是其中之一,然后看是否以 `/` 或 `!` 开头。宁多勿少。测试含 BOM、NBSP、全角空格、U+2028 前缀。 |
| 2 | 阻断 | 压缩把 `quiet` 置 false,不重试的成功压缩(手动 `/compact`、阈值压缩)之后不一定有 `agent_start / agent_end`,`quiet` 永远为 false,命令结束不报 idle,broker 一直等到超时 | 成立,而且**现有状态上报本来就是这样**(16 号方案 §2.1 规定压缩结束不报 idle),本命令没有让它更糟;但修订时一并改。核对 pi 0.79.10:`session_compact` 事件带 `reason`、`willRetry`;成功压缩后 pi 在 `willRetry` 为真时继续,否则 `return agent.hasQueuedMessages()`(有排队消息才继续)。压缩**失败**时只给界面发 `compaction_end`,扩展收不到。 | `session_compact` 且 `willRetry === false` 且 `!ctx.hasPendingMessages()`(会话层与 agent 队列同步维护的待处理计数)时:`quiet = true`,`inflight == 0` 就报 idle。`willRetry` 为真或有待处理消息时保持 working,由随后的 `agent_start / agent_end` 判定。压缩**失败**扩展无从得知,保持 working → broker 超时告警,按 16 号 §2.2 重启该 agent 恢复(已有限制,写入 README)。测试:手动压缩成功无后续轮次 → idle;willRetry → 不报;有待处理消息 → 不报;压缩期间命令在途 → 压缩结束等命令结束再报。 |
| 3 | 应修 | 0→1 的 working 上报失败仍发送,herdr 可能仍认为发送方 idle | 成立 | **working 上报失败就不发送**:error 提示"无法向 herdr 上报状态,未发送;稍后重试",`inflight` 回退(回到 0 时若 `quiet` 则尝试补报 idle,失败只记日志)。没有 `HERDR_PANE_ID`(不在 a2a 管理的 pane 里)时插件根本不注册命令,不存在"不协调就发送"。测试覆盖。 |
| 4 | 应修 | Router 拒绝位置:渲染后、入队前;幂等路径也要先过检查 | 成立(Codex 确认位置正确) | 检查放在 Router 渲染模板之后、**任何幂等查找与入队之前**;拒绝写 REJECTED 审计、不写 QUEUED。实现时对照 `router.py` 的幂等分支加测试:带幂等键的不安全发送被拒且不产生 intent。 |
| 5 | 将来考虑 | fnx 在 `/a2a` 在途时退出:`inflight` 随进程消失,send 子进程的结果可能来不及显示 | 是 | 保持不报 idle(进程没了,herdr 不保留上报状态,回到屏幕识别)。README 写明:结果未知时先 `a2a queue <对方>` 或查审计核实,**直接重发可能重复**(手动发送不带幂等键)。 |

## 10. Codex 第四轮复核(2026-10-14)与再修订

| # | 级别 | 问题 | 判断 | 修订 |
| --- | --- | --- | --- | --- |
| 1 | 阻断 | `ctx.hasPendingMessages()` 只看 `session.pendingMessageCount`,不含交互模式独立维护的 `compactionQueuedMessages`(压缩期间提交的输入暂存于此,`compaction_end` 时才 flush);`session_compact` 在 `compaction_end` 之前被 await → 插件可能在 flush 前报 idle | 成立(已在 fnx 内 pi 0.79.10 找到 `compactionQueuedMessages` 的 push 与 flush)。扩展既看不到这个队列,也收不到 `compaction_end`,**无法可靠判断压缩之后是否真正收尾** | **撤回 §9 第 2 项**,恢复 16 号方案原规则:`session_compact` 不报 idle,`quiet` 保持 false,直到之后的 `agent_end` 确定结束。代价(已有限制,本命令没有让它更糟):不重试的成功压缩之后若没有新的模型轮次,herdr 一直认为该 agent 在 working,发给它的消息等到超时、告警,按 16 号 §2.2 重启该 agent 恢复;压缩之后用户随便输入一句让模型跑完一轮也能恢复。自动阈值压缩本来就只发生在插件已判定"不确定"(用量 ≥ 阈值 90%)而保持 working 的时候。README 写明。测试:压缩结束后无后续轮次 → 不报 idle;压缩中命令在途 → 命令结束也不报 idle;之后 agentStart/agentEnd(确定)→ idle。 |
| 2 | 应修 | 检查前置到所有幂等查找之前,会让旧拓扑下已入队请求的同键重放被报成"未发送",破坏重放语义 | 成立(`_send_idempotent`:记录为 `enqueued` 时直接返回原回执,不重新鉴权) | 检查只作用于**新消息入队之前**:放进 `_authorize`(新发送与 intent 补做的重新鉴权都经过它),另外在 intent 补做**直接使用快照**入队之前也检查快照文本(防旧版本留下的 intent);`enqueued` 记录照旧返回原回执。拒绝写 REJECTED 审计、不写 QUEUED。测试:无幂等键的新发送、带键的新发送、intent 补做(快照与重新鉴权两条路)被拒;已有 `enqueued` 记录的重放仍返回原回执。 |

## 11. 实现记录(2026-10-15)

按 §2–§10 实现:

- **Python 后端**
  - `topology.py`:`command_prefix(text)`(JS `trim()` 字符集并上 `isspace()`,反复去掉开头后看 `/`、`!`)、`check_template_safe`;只在 `TopologyStore.create`、`add_edge`、`set_template` 调用,`Topology.from_dict`(加载、热重载)不调用。
  - `router.py`:拒绝码 `unsafe_template`;`_authorize` 渲染后检查(新发送、intent 补做的重新鉴权都经过);intent 用快照入队前再查快照文本;`enqueued` 重放不经过检查,照旧返回原回执。协议文档 08 号拒绝码表补上。
- **插件 `a2a.ts`**
  - `makeReporter` 的 `report(state, attempts, valid?)`:每次尝试前检查 `valid()`,不满足就放弃(不算失败、不打日志);新增可选参数 `run` 只供单元测试替换真正的 herdr 调用。
  - `makeReadiness(reporter)`:`quiet` + `inflight` 状态机;`registerStateReporting` 的 `agent_start` / `agent_end` / `session_before_compact` 都经过它,行为与阶段 8 相同。
  - `/a2a` 命令:`parseCommandArgs`、`formatEdges`、`formatOutcome`、`makeSendCommand`;补全用缓存(加载时、每轮 refresh、每次执行命令时更新);没有 `HERDR_PANE_ID` 不注册。
- **测试**
  - 拓扑 / Router / 幂等:命令前缀字符集(含 BOM、NBSP、全角空格、U+2028、U+1680、`\x1c`;零宽空格 U+200B 不算)、写入拒绝与文件不变、旧拓扑加载并可修正、发送被拒不入队无 QUEUED 审计、修正后可发、带键新发送不留 intent、intent 补做两条路径被拒、`enqueued` 重放返回原回执。
  - 插件:`tests/js/readiness.test.mjs` 9 个确定性时序用例(§8.2 的 7 项,加上 working 失败两种);`test_pi_extension.py` 新增 11 个(列边、发送经 Router 且上报 [working, idle]、两次发送两条消息不留幂等记录、非法边 / 别的角色的边 / 多参数不发、旧拓扑的 `unsafe_template` 显示未发送、读边失败、结果未知为 warning 并提示不要重发、working 失败不发、没有 pane 不注册、补全)。
  - 真实 fnx(VM,`RealFnxCommand`,不调用模型、不启动 broker):输入框里 `/a2a` 列边、`/a2a dv_done` 入队且屏幕显示 msg_id、`/a2a nope` 不入队、之后状态回到 idle / done、会话文件里没有 assistant 消息;占住 IP 排空锁让命令卡住时 herdr 显示 working、队列为空,放开后发送完成、回到 idle / done。
  - `RealFnxPlugin` 改为启动参数 `--no-extensions -e <开发版插件> -e <检查扩展>`:VM 上已在线安装插件,原来的软链接安装会被(21 号 §12 的防护)拒绝;新方式不往 fnx 目录写任何东西。已实测 `-ne` 不加载在线包、`-e` 只加载指定文件。
  - 变异检查(都被抓到):agent_end 不管在途命令、命令不等 working、idle 重试不复查、working 失败仍发送、压缩不置 quiet=false、Router 不查前缀、快照不查前缀、set_template 不查、字符集不含 BOM。
- **与方案的差异**:§7 第 2 项"命令拖长期间 broker 入站、最终 DELIVERED"没有用真实 fnx 做——投递会把消息打进 dv 的 pane、触发模型。改为验证 broker 依据的 herdr 状态:命令在途时为 working、结束后为 idle / done;broker 在 working 时等待、idle / done 时投递,由已有的投递测试覆盖。§7 第 4 项(模型运行中执行命令)只有单元测试,真实测试需要调用模型,未做。

## 12. Codex 审核实现(2026-10-15)

阻断无、应修无。Codex 确认:状态机与 pi 事件对应正确(压缩不提前报 idle);进入发送后 `runSend`、格式化与 notify 都在 try/finally 里,`commandEnd` 一定执行;working 失败不发送并回退 `inflight`;`command_prefix` 字符集正确;Router 两处检查与幂等语义吻合。

| 将来考虑 | 判断 | 处理 |
| --- | --- | --- |
| `RealFnxPlugin` 改用 `-ne -e` 后不再验证"软链接安装后被 fnx 自动发现" | 是 | 记录为缺口:VM 上已在线安装插件,软链接安装会被防护拒绝,恢复这项测试需临时卸掉用户的在线包(改用户环境),暂不做。软链接安装本身的逻辑由 `test_plugin.py` 覆盖;fnx 能加载软链接扩展已在阶段 6 与 19 号方案实测过。 |
| 检查只约束新入队;升级前已入队的不安全消息 broker 不再校验 | 是,影响小(现有拓扑没有这类模板) | README 写明边界与升级前核查方法:`a2a queue` 查看;排队中的消息不能直接裁定(`resolve` 只处理队列头的不确定 / 终态消息),先停接收方让它变成 `TARGET_MISSING`,再 `resolve … abandon`,然后恢复接收方。(初稿写成直接 `resolve … abandon`,核对 `rulings.plan` 后更正。) |

## §13 codex-test 交叉验证(2026-10-15)

环境:OrbStack VM `ubuntu` / Ubuntu 22.04.5 / Python 3.10.12。运行前 Mac/VM 的 11 个改动文件 SHA-256 全部一致:`README.md`、`pi-extension/a2a.ts`、`router.py`、`topology.py`、`plugin_harness.mjs`、`readiness.test.mjs`、`test_idempotency.py`、`test_integration_lifecycle.py`、`test_pi_extension.py`、`test_router.py`、`test_topology.py`。没有改源码或测试文件。

| 检查 | 结果 | 实测记录 |
| --- | --- | --- |
| Python 单元全套 | ✅ 662 项，615 通过、0 失败、47 跳过 | 清除 `herdr/a2a` 下 `__pycache__` 后，在 VM `herdr/a2a` 运行 `env PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v`；输出 `Ran 662 tests in 23.683s`、`OK (skipped=47)`。 |
| Node readiness | ✅ 9/9，0 失败、0 跳过 | 仓库根运行 `node --test herdr/a2a/tests/js/readiness.test.mjs`；9 个时序用例全部通过。Node 对 `.ts` 模块有 `MODULE_TYPELESS_PACKAGE_JSON` 性能提示，不影响结果。 |
| 指定真实 fnx 插件与斜杠命令 | ✅ 3/3，0 失败、0 跳过 | `A2A_INTEGRATION=1 A2A_INTEGRATION_FNX=1 PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v tests.test_integration_lifecycle.RealFnxPlugin tests.test_integration_lifecycle.RealFnxCommand`；`Ran 3 tests in 28.761s`、`OK`。其中一项验证插件加载，两项只输入 `/a2a`、`/a2a dv_done`、`/a2a nope`，不启动 broker；会话文件断言没有 assistant 消息。未向真实 fnx 输入普通文字或调用模型。 |
| 全量集成发现（FNX 开关关闭） | ✅ 47 项，39 通过、0 失败、8 跳过 | VM `herdr/a2a` 运行 `env -u A2A_INTEGRATION_FNX PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 A2A_INTEGRATION=1 python3 -m unittest discover -s tests -p 'test_integration_*.py' -v`；`Ran 47 tests in 335.348s`、`OK (skipped=8)`。此轮作为完整 discover 基线；8 项为真实 fnx 类被开关跳过。 |
| 安全全量集成（FNX 开关开启） | ✅ 45/45，0 失败、0 跳过；排除 2 项 | 使用临时 `/mnt/mac/private/tmp/a2a_xc_safe_integration.py` 收集全部 `test_integration_*.py`，只排除 `TestRealFnxLaunch` 两项（它们会输入 `! echo ...`）；其余测试在 `A2A_INTEGRATION=1 A2A_INTEGRATION_FNX=1 PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1` 下运行。输出 `collected=47 excluded_non_slash_real_fnx=2`、`Ran 45 tests in 404.468s`、`OK`。真实 fnx 的 lifecycle、recovery、IP ops、plugin 和 `RealFnxCommand` 均实际运行；真实 fnx pane 只收到测试中的 `/a2a` 命令，未调用模型。临时 runner 已删除。 |
| 拓扑 CLI 拒绝危险模板 | ✅ 8/8 拒绝，拓扑文件逐次字节不变 | 临时拓扑与状态位于 `/tmp/a2a_xc_manual`。对 `add-edge` 和 `set-template` 分别测试 `/a2a x`、`!ls`、BOM+`!ls`、全角空格+`/quit`，每次 CLI 返回码 6，写前后文件字节完全相同。 |
| 普通正文中的 `/` 与 `!` | ✅ | `set-template` 写入“正文中间 /a2a x 与 !ls 都是普通文字”返回码 0。 |
| Router 最后防线 | ✅ | 手工把拓扑模板改为 `/a2a x`，以临时注册表身份运行真实 `a2a send dv_done` CLI；返回码 4，stderr 的 `rejected` 为 `unsafe_template`，`sw_uart` pending 队列为 0。 |
| 变异检查 | ✅ 两处均被目标测试捕获 | 隔离副本中将 `a2a.ts` 的 `inflight === 1` 改为 `inflight > 0`；readiness 测试 8 通过、1 失败，重叠命令用例实际观察到两次 `working`，正确断言要求一次。另一隔离副本把 Python `command_prefix` 改为恒返回 `None`；`test_detects_slash_and_bang_after_js_and_python_whitespace` 的 34 个子用例失败。两处都使用 trap 恢复，`cmp` 与备份及原文件一致。 |
| 清理与限制 | ✅ | 临时状态、拓扑、变异副本、日志和脚本已删除；测试生成的会话和 tmux 均无残留。检查时另见 fnx_dv `agent/sessions/--tmp-a2a_ne_check--`，它不属于本轮测试命名且来源未核实，因此未触碰。未触碰禁用的 Herdr 会话，未修改 fnx agent 安装目录/在线包，未读取 `auth.json` 或密钥，未调用模型。 |

结论:所测 Python 模板写入拒绝、Router 入队前二次检查、插件 readiness 状态机及真实 fnx `/a2a` 命令行为符合预期。最终安全全量集成覆盖了所有不需要普通 shell 输入的用例；仅排除 `TestRealFnxLaunch` 两项以遵守只允许 `/a2a` 的约束。
