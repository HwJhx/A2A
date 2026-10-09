# 消息协议:状态、合法迁移与审计事件

> **协议版本 2**(2026-10-09)。本文是 `herdr/a2a` 实现所遵循的协议定义,也是 Broker(阶段 5)的契约。
>
> **对齐状态(2026-10-09):`herdr/a2a` 的迁移表已对齐。** `src/a2a/messages.py` 的 `ALLOWED_TRANSITIONS` 等于本文 §2 的迁移表,由 `tests/test_protocol.py` 的 `test_code_table_equals_the_document` 保证。
> 以后修订本文时,先改文档,这条测试会失败,直到代码对齐;不要为了让它通过而回改本文。
> **迁移表之外的规则**(§7 队列头、`queue_seq`、槽位放行记录、裁定与对账、告警、观察窗口等)**由 Broker 实现,尚未实现**;
> `herdr/a2a_codex` 由 Codex 自行对齐,**`a2a` 已对齐不代表 `a2a_codex` 已对齐**。
>
> 依据的用户决策:D8(目标 `blocked` 时立即失败,Broker 不排队、不自动重试、不自动批准;操作员显式裁定的重试是例外,见 §7.3)、D9(`done` 当作可投递)、D11(接收方先不验证来源,去重只放 Broker)。
> 版本 2 采纳了 `herdr/codex/对08协议的复核与a2a_codex对齐方案.md` 的三项意见,并经用户确认,见 §11 修订记录。

## 1. 状态

| 状态 | 终态? | 含义 | 由谁设置 |
| --- | --- | --- | --- |
| `QUEUED` | 否 | 已写入持久队列,等待 Broker 投递。两条入队路径:正常入队(鉴权通过后),或**重试入队**(§7.3 规则 3) | Router 或受信任的重试入队路径(入队时唯一允许的初始状态;操作员不直接设置消息状态) |
| `WAITING_TARGET` | 否 | 目标忙(`working` 或 `unknown`),正在等它变成可投递 | Broker |
| `DISPATCHING` | 否 | **写前标记**:Broker 在调用 herdr 发出 prompt **之前**先把它落盘。因此从它出去的每一条路都发生在"调用已经开始"之后 | Broker |
| `RETRYING` | 否 | 出现**已证明未提交**的错误,退避后重试。表示"确定没发出去" | Broker |
| `DELIVERY_UNCERTAIN` | 否 | prompt **可能已经送达**:调用结果不明(超时、`agent_prompt_stalled`、无法证明未提交的错误、Broker 崩溃窗口,或 herdr 接受了但观察窗口内没看到目标开始处理)。**默认不自动重发**,见 §6 | Broker;出口见 §6 |
| `DELIVERED` | **是** | 已送达,且有证据(见 §9 证据种类) | Broker 或操作员 |
| `TARGET_BLOCKED` | **是** | 目标卡在审批或提问界面。按 D8 **立即失败**:Broker 不排队等待、**不自动重试**、不自动批准。操作员显式裁定的重试是例外(§7.3 规则 2) | Broker |
| `TARGET_MISSING` | **是** | 目标 agent 已不存在 | Broker |
| `TIMEOUT` | **是** | 等待目标变成可投递超时;也涵盖"目标状态查询持续失败,达到次数或时间上限"(§3) | Broker |
| `FAILED` | **是** | 操作员放弃一条消息,或确定不可恢复的错误(例如配置缺陷)。**从 `DELIVERY_UNCERTAIN` 进入 `FAILED` 只能由操作员放弃**,永远不由超时或自动的错误处理产生(§6、§7.2) | Broker 或操作员 |
| `REJECTED` | **是** | 鉴权失败。**只出现在审计日志里,永远不进入队列** | Router |

## 2. 合法迁移表

同一状态内更新 `detail` / `attempts` 不算迁移,对非终态允许。**终态之后不允许任何修改**(含改回非终态、改 `detail`、改 `attempts`)。

`FAILED` 在迁移表上可以从任何非终态到达,**但 `DELIVERY_UNCERTAIN → FAILED` 只能由操作员放弃触发**(§6),这条通用规则不覆盖它。`QUEUED` 和 `REJECTED` 不是任何迁移的目标。

迁移表只规定单条消息的状态;"同一目标的下一条消息何时可以投递"由 §7 的队列规则规定。

| 当前状态 | 可以迁往 |
| --- | --- |
| `QUEUED` | `WAITING_TARGET`、`DISPATCHING`、`TARGET_BLOCKED`、`TARGET_MISSING`、`TIMEOUT`、`FAILED` |
| `WAITING_TARGET` | `DISPATCHING`、`TARGET_BLOCKED`、`TARGET_MISSING`、`TIMEOUT`、`FAILED` |
| `DISPATCHING` | `DELIVERED`、`DELIVERY_UNCERTAIN`、`RETRYING`、`TARGET_BLOCKED`、`TARGET_MISSING`、`FAILED` |
| `RETRYING` | `DISPATCHING`、`WAITING_TARGET`、`TARGET_BLOCKED`、`TARGET_MISSING`、`TIMEOUT`、`FAILED` |
| `DELIVERY_UNCERTAIN` | `DELIVERED`、`RETRYING`、`FAILED`(**三条都有守卫,见 §6;没有任何由时间触发的出口**) |
| `DELIVERED` `TARGET_BLOCKED` `TARGET_MISSING` `TIMEOUT` `FAILED` `REJECTED` | (终态,无出口) |

与版本 1 的差异:**删除** `DISPATCHING → WAITING_TARGET`;**新增** `RETRYING → TARGET_BLOCKED`、`RETRYING → TARGET_MISSING`。`DELIVERY_UNCERTAIN` 的出口没变,但 `→ DELIVERED`、`→ RETRYING` 加了守卫。

机器可读版本(`tests/test_protocol.py` 解析这一段,**请保持格式**:每行 `状态 -> 目标1 目标2 ...`):

<!-- transitions:begin -->
```text
QUEUED -> WAITING_TARGET DISPATCHING TARGET_BLOCKED TARGET_MISSING TIMEOUT FAILED
WAITING_TARGET -> DISPATCHING TARGET_BLOCKED TARGET_MISSING TIMEOUT FAILED
DISPATCHING -> DELIVERED DELIVERY_UNCERTAIN RETRYING TARGET_BLOCKED TARGET_MISSING FAILED
RETRYING -> DISPATCHING WAITING_TARGET TARGET_BLOCKED TARGET_MISSING TIMEOUT FAILED
DELIVERY_UNCERTAIN -> DELIVERED RETRYING FAILED
```
<!-- transitions:end -->

示意图(实线是自动迁移;`DELIVERY_UNCERTAIN` 的三条虚线都带守卫):

```text
          ┌──────── 目标复核放在进入 DISPATCHING 之前 ────────┐
          ▼                                                   │
QUEUED ─▶ WAITING_TARGET ─▶ [复核通过] ─▶ DISPATCHING ─▶ DELIVERED
  │            │                              │  │  │
  │            │                              │  │  └─▶ DELIVERY_UNCERTAIN ┄┄▶ DELIVERED   (守卫: 操作员裁定 / 经验证的机制)
  │            │                              │  │            ┆  └┄┄▶ RETRYING            (守卫: 操作员裁定 / 经验证的机制)
  │            │                              │  │            └┄┄┄┄┄┄▶ FAILED              (守卫: 操作员裁定放弃;不会因超时发生)
  │            │                              │  └─▶ RETRYING ─▶ DISPATCHING / WAITING_TARGET
  │            │                              │         │
  └────────────┴──────────────────────────────┴─────────┴─▶ TARGET_BLOCKED / TARGET_MISSING / TIMEOUT / FAILED  (终态)
```

## 3. 每个迁移由什么触发

| 迁移 | 触发条件 |
| --- | --- |
| `QUEUED → DISPATCHING` | 复核确认目标是 READY(`idle` 或 `done`) |
| `QUEUED → WAITING_TARGET` | 目标是 `working` 或 `unknown` |
| `QUEUED → TARGET_BLOCKED` | 复核发现目标 `blocked`(D8:立即失败) |
| `QUEUED → TARGET_MISSING` | 目标 agent 不存在 |
| `WAITING_TARGET → DISPATCHING` | 目标变成 READY,**且复核通过** |
| `WAITING_TARGET → TARGET_BLOCKED` | 等待期间发现目标 `blocked`(D8) |
| `WAITING_TARGET → TARGET_MISSING` | 等待期间目标不存在 |
| `QUEUED → TIMEOUT` / `WAITING_TARGET → TIMEOUT` | 等待超过上限;或目标状态查询连续失败达到次数上限、或累计耗时达到总等待上限(§3 末段) |
| `DISPATCHING → DELIVERED` | herdr **接受**了 prompt,**并且**在观察窗口内观察到目标开始处理 |
| `DISPATCHING → DELIVERY_UNCERTAIN` | 调用结果不明:超时、`agent_prompt_stalled`、任何**无法证明未提交**的错误、herdr 接受了但观察窗口内没看到目标开始处理 |
| `DISPATCHING → RETRYING` | **只有**能证明 prompt 未提交的错误(§5) |
| `DISPATCHING → TARGET_BLOCKED` | herdr 返回 `agent_blocked`。已实测证实它在写入前拒绝(§5,`09` 号文档) |
| `DISPATCHING → TARGET_MISSING` | herdr 返回 `agent_not_found`。已实测证实它在写入前查找目标、找不到即返回(§5,`09` 号文档)。目标在**调用前**的独立复核中就不存在的,不经过 `DISPATCHING`,见上面的 `QUEUED / WAITING_TARGET / RETRYING → TARGET_MISSING` |
| `RETRYING → DISPATCHING` | 退避结束,**复核通过**(目标 READY) |
| `RETRYING → WAITING_TARGET` | 退避结束后复核发现目标忙 |
| `RETRYING → TARGET_BLOCKED` | 复核发现目标 `blocked`:`RETRYING` 表示"确定没发出",所以按 D8 立即失败 |
| `RETRYING → TARGET_MISSING` | 复核发现目标不存在 |
| `RETRYING → TIMEOUT` | 重试期间累计等待超过上限;或目标状态查询持续失败达到上限(同上) |
| `DELIVERY_UNCERTAIN → DELIVERED` | **操作员明确裁定已送达**,或**经真实 herdr 验证的机制**确认已送达(§6) |
| `DELIVERY_UNCERTAIN → RETRYING` | **操作员明确裁定未送达、可以重发**,或**经真实 herdr 验证的机制**确认未提交且允许重试(§6) |
| `DELIVERY_UNCERTAIN → FAILED` | **只有操作员裁定放弃**。不会因为时间流逝、Broker 的错误处理、崩溃恢复、拓扑变更或目标消失而发生(§6、§7.2) |

目标状态检查阶段(`agent get` / `agent wait`)出现临时错误时,消息**留在当前状态**,只更新 `attempts` 和 `detail`,不迁移——此时 prompt 还没有发出,不涉及投递不确定性。该阶段必须有**退避**(间隔逐次拉长,暂定 1 秒起、每次加倍、单次不超过 30 秒),并同时受两个上限约束,任一达到即转 `TIMEOUT`:**连续失败次数**(暂定 5 次)和**总等待时限**(Broker 的等待超时,暂定 300 秒)。因此 `TIMEOUT` 既表示"目标一直没有 READY",也表示"目标状态持续查不到"。以上数值都是**暂定、可配置**,待 Broker 实测。

## 4. 规则

1. **终态不可变。** 到达终态时消息自动从 `pending/` 归档到 `done/`。
2. **`REJECTED` 不进队列。** 鉴权失败只写审计日志(含 `reject_code`)。
3. **新消息只能以 `QUEUED` 入队。**
4. **`DELIVERY_UNCERTAIN` 默认不自动重发。** 只有**操作员明确裁定**,或**经真实 herdr 验证的机制能够确认结果**时,才允许转为 `DELIVERED` 或 `RETRYING`;**人工裁定必须记录审计**(§9)。"没观察到送达"不等于"确认未送达"。
5. **`blocked` 的处置按状态区分(D8):**
   - `QUEUED`、`WAITING_TARGET`、`RETRYING` 期间发现 `blocked` → 立即转 `TARGET_BLOCKED`。
   - `RETRYING` 期间发现目标不存在 → 立即转 `TARGET_MISSING`。
   - `DISPATCHING` 期间收到 `agent_blocked`:已实测证实是写入前拒绝,转 `TARGET_BLOCKED`(见 §5)。
   - **`DELIVERY_UNCERTAIN` 期间发现 `blocked`:只记入 `detail` 和审计,不改变状态。** 因为目标此刻 `blocked` 也可能正是消息已送达的结果(收到 prompt 后开始干活,然后停在审批界面);把它标成"确定被 blocked 而未送达"是错的。
6. **目标状态复核放在进入 `DISPATCHING` 之前。** `DISPATCHING` 是写前标记,所以没有 `DISPATCHING → WAITING_TARGET`:复核发现目标不再 READY,就留在(或回到)`WAITING_TARGET`,根本不进入 `DISPATCHING`。**调用 herdr 之后结果不明,一律进入 `DELIVERY_UNCERTAIN`。**
7. **只有能证明 prompt 未提交的错误才能安全重试。** 不能只按错误名分类:每个错误码的语义都要在真实 herdr 上实测确认,未确认的(含未知错误码)按"不确定"处理。当前状态见 §5:`agent_blocked`、`agent_not_ready`、`agent_not_found`、`server_not_running` 已在 herdr 0.9.3 上实测为"未写入"并已定类;`agent_prompt_failed` 实测为"已写入";升级 herdr 后须重测,结果不一致就退回"不确定"。
8. **`unknown` 不是 READY。** 只能等待,等到超时 → `TIMEOUT`。
9. **`idle` 和 `done` 都是 READY(D9)。** 实测:`done` 不会自己变回 `idle`,只等 `idle` 会永远等不到。
10. **同一目标按 `msg_id` 顺序串行投递(FIFO)。** 实测:同时发给忙碌的 agent 的多条 prompt 都会被接受,但处理顺序不确定,所以顺序必须由 Broker 保证。
11. **所有 ID 严格校验。** `msg_id` 必须匹配 `^[0-9a-f]{16}-[0-9a-f]{6}$`,目标 `agent_id` 必须匹配 `^[a-z][a-z0-9_-]{0,31}$`,不合格一律 `InvalidIdError`(防路径穿越)。
12. **每个目标只处理队列头;队列头没有放行,后续消息一律不投递。** 暂停**只作用于这一个目标**,其他目标、其他 IP 照常运行。暂停的依据是"队列头尚未放行",不是某一时刻的状态名:队列头从 `DELIVERY_UNCERTAIN` 经裁定转 `RETRYING` 后仍占住队列头,再次进入 `DELIVERY_UNCERTAIN` 时暂停连续保持。只有 `DELIVERED` 自动放行;不确定态和确定失败终态都要经操作员动作才放行。**没有任何由时间触发的放行。** 详见 §7。
13. **时间只能触发告警,不能触发 `DELIVERY_UNCERTAIN` 的状态迁移。** 见 §6。

## 5. herdr 返回值的分类(prompt 调用阶段)

一次 `agent prompt` 调用之后,Broker 必须先回答:**这条 prompt 有没有可能已经提交?** 能**证明**没有提交的,才可以走 `RETRYING`;不能证明的,走 `DELIVERY_UNCERTAIN`。未知的错误码一律按"不能证明"处理。

| herdr 返回 | 能否证明 prompt 未提交 | 依据与状态 | 处置 |
| --- | --- | --- | --- |
| 客户端在**发出请求之前**就失败(找不到 herdr 可执行文件、无法启动子进程) | **是** | 请求从未发出,逻辑上成立 | `RETRYING`(命令行用法错误属于配置缺陷,转 `FAILED`) |
| Broker 在**调用 herdr 之前**的独立复核(`agent get` / `agent list`)发现目标不存在 | **是** | prompt 尚未发出,根本没有进入 `DISPATCHING` | `TARGET_MISSING` |
| `agent_not_found`(调用 `agent prompt` 之后才收到) | **是(实测)** | 静态目标、agent 退出后、以及"连续发送时关闭 pane"的竞态 20 次,共 22 次,**全部没有写入**;竞态中写到一半被打断的情况返回的是另一个错误码 `agent_prompt_failed`(见下)。说明 herdr 先查找目标、找不到即返回 | `TARGET_MISSING` |
| `agent_blocked` | **是(实测)** | 两种构造(识别为 pi 的 agent / 前台不是 pi 但声明为 pi),均在 `blocked` 时返回,**没有写入**,耗时 2~6 毫秒;与 herdr 文档"拒绝时不发送任何输入"一致 | `TARGET_BLOCKED` |
| `agent_not_ready`("agent … is no longer the pane foreground process") | **是(实测)** | 前台进程不是该 agent 时返回,**没有写入**,耗时 6 毫秒。注意:agent 进程退出后 herdr 会立即注销它,此时返回的是 `agent_not_found` 而不是本错误 | `RETRYING`(退避后复核目标:agent 回到前台就投递,不存在就 `TARGET_MISSING`) |
| `server_not_running` | **是(实测)** | 连不上会话的 socket 时返回,请求根本没有发出,**没有写入**。**未测**:服务在处理请求中途退出(那种情况若出现其他错误码,按"未知"处理) | `RETRYING`(退避后复核) |
| `agent_prompt_failed`("PTY actor closed during input submission") | **否(实测:已写入)** | 写入过程中 pane 被关闭时返回;实测 17 次**全部已经写入了文字**。这是"返回错误但消息已送达"的直接证据 | `DELIVERY_UNCERTAIN` |
| `agent_prompt_stalled` | **否** | herdr 文档:不代表没有发出 | `DELIVERY_UNCERTAIN` |
| `timeout` / `client_timeout`(等待 herdr 返回超时) | **否** | 超时可能发生在提交之后 | `DELIVERY_UNCERTAIN` |
| `agent_name_taken` / `invalid_agent_name` | 不适用 | 这是 `agent rename` 的错误,不属于 prompt 路径 | 不会在投递中出现;出现即视为实现缺陷 → `FAILED` |
| 其他未知错误码、无法解析的输出 | **否** | 语义未知 | `DELIVERY_UNCERTAIN` |

**验证记录(2026-10-09,herdr 0.9.3):** 上表"是(实测)/否(实测)"各行的依据见 `herdr/claude/09-herdr-error-probe-plan.md` §7。方法:用一个把收到的每个字节写进日志的假 agent 当目标(不调用模型),错误返回后查日志里有没有这条 prompt 的文字。
**适用范围:** 结论只对 **herdr 0.9.3** 成立。升级 herdr 后必须重跑 `review-probes/probe_herdr_errors.py`,结果不同就回到保守处理(`DELIVERY_UNCERTAIN`)。实测样本有限(每种 2~22 次),说明"观察到的行为",不是对 herdr 源码的证明。
**实现提醒:** `agent_prompt_failed` 目前不在 `HerdrClient` 的错误码映射里,会被当作未知错误码,按本表的通用规则也会进入 `DELIVERY_UNCERTAIN`,结果正确;Broker 阶段应把它显式列入。

## 6. `DELIVERY_UNCERTAIN` 的处置

herdr 文档明确说:超时或 `agent_prompt_stalled` **不代表没有发出**。此时盲目重发会让目标收到两遍同样的话。

**默认不自动重发,也不自动判定已送达。** 离开该状态的方式只有:

| 方式 | 迁移 | 要求 |
| --- | --- | --- |
| 操作员裁定"已送达" | `→ DELIVERED` | 记审计,`evidence = operator_confirmed` |
| 操作员裁定"未送达,可以重发" | `→ RETRYING` | 记审计;之后 `RETRYING → DISPATCHING` 重发 |
| 操作员裁定"放弃" | `→ FAILED` | 记审计 |
| 经真实 herdr 验证的机制**确认已送达** | `→ DELIVERED` | 记审计,`evidence = verified_mechanism` |
| 经真实 herdr 验证的机制**确认未送达,且允许重试** | `→ RETRYING` | 记审计。**不是 `FAILED`**:确认没送达就应该重发 |

经验证的机制必须先在真实 herdr 上验证,验证结论写进本文后才可启用;**目前没有任何机制满足此条件**。机制超时、异常或无法判断时,消息保持 `DELIVERY_UNCERTAIN`。

在 `DELIVERY_UNCERTAIN` 下,`FAILED` **只能来自操作员放弃**:不用于"确认未送达"(那应转 `RETRYING`),也不来自任何自动的错误处理——此时 Broker 已不再调用 herdr,遇到的配置、存储、拓扑问题都不能证明原消息没送达(§7.2)。其他状态下,`FAILED` 用于操作员放弃或确定不可恢复的错误。

**没有由时间触发的出口。** v1 **不提供**"超时后自动放弃并放行"的开关。时间只能触发告警,不改变 `DELIVERY_UNCERTAIN` 状态,也不解除该目标的队列暂停(§4 规则 12)。

**告警策略(暂定,可配置;只是告警,不是状态迁移):** 进入 `DELIVERY_UNCERTAIN` 满 **1 小时**发出提醒,满 **24 小时**升级。告警内容至少包含:目标是谁、哪条消息不确定、其后被暂停的消息数量、可用的裁定方式。告警重复发送的间隔另定。

**为什么不能自动核实(目前):**
- "目标输出里找到这句话"不可靠:消息是固定句式,同一条边反复发送的文字完全一样,找到的可能是**上一次投递**留下的。
- "没找到 / 没看到目标开始处理"不是"确认未送达":全屏界面可能读不到历史,状态采样可能漏掉短暂的 `working`,目标也可能已处理完并回到 `idle`。

**候选依据,待验证,不得作为自动核实机制使用:** herdr 返回的 agent 信息里有 `state_change_seq`,每次状态变化递增(探测中见过 4→5→6)。比较发送前后的序号,也许比找文字更接近"目标是否开始处理过"。但它是否可靠(同状态重复变化是否递增、server 重启后是否重置、与某条具体消息能否关联)**尚未验证**。验证并写进本文之前,它**只能作为人工裁定时的参考信息,不能自动触发任何迁移**。

**队列影响(已确认):** 不确定的消息作为队列头,在获得裁定之前一直挡住该目标的后续消息(§7)。原因:原消息可能已经送达,如果在它没有定论时放行后续消息,会乱序或重复做同一件事。代价:无人裁定时这个目标的流水线会停住——这是有意的,停住是可见的,乱序不可见。

## 7. 目标队列:队列头、暂停与操作员动作

本节规定"同一目标的下一条消息何时可以投递"。它**不改变迁移表**。

### 7.1 队列头模型

1. 每个目标一条 FIFO 队列,按 `queue_seq`(§7.4)排序。Broker 对每个目标**只处理队列头**:队列头没有**放行**,`queue_seq` 更大的消息一律不调度。
2. 暂停的依据是"**队列头尚未放行**",不是某一时刻的状态名。队列头从 `DELIVERY_UNCERTAIN` 经裁定转 `RETRYING` 后,它仍是队列头;再次进入 `DELIVERY_UNCERTAIN` 时,暂停连续保持。
3. 队列头何时放行:

   | 队列头的结局 | 是否放行 |
   | --- | --- |
   | `DELIVERED`(证据已落盘) | 自动放行 |
   | 非终态(含 `DELIVERY_UNCERTAIN`、`RETRYING`) | 不放行 |
   | 确定失败终态 `FAILED`、`TIMEOUT`、`TARGET_BLOCKED`、`TARGET_MISSING` | **不自动放行**。暂停并告警,等操作员动作(§7.3)。原因:这些只说明"传输结果确定",不等于"业务上可以跳过"——流水线有阶段依赖,前一步没完成就放行后一步,可能跳过前置条件 |
   | 操作员"放弃并继续"生效后 | 放行 |

4. 暂停**只作用于该目标**,其他目标、其他 IP 照常运行。v1 的暂停范围是**该目标的整条队列**:多条边汇入同一目标时也一起停,因为没有依赖分组信息,无法判断哪些消息可以绕过。
5. `REJECTED` 不入队,不占队列头。**消息到终态会归档到 `done/`(§4 规则 1),但这不等于放行**:确定失败终态的消息归档后,它的槽位仍然挡住该目标,直到操作员动作放行。因此"槽位是否已放行"必须**持久记录**,不能从"消息还在不在 `pending/`"推断;重启后必须能恢复。只有槽位已放行的终态消息才不占队列头。记录方式在 Broker 阶段设计(§10)。
6. **没有任何由时间触发的放行**;时间只触发告警(§6)。

### 7.2 不能证明未送达的事件

以下事件**都不能**证明队列头没送达,**不得**让它自动进入任何终态,也**不得**放行队列。Broker 应告警、保持暂停,交操作员处理:

| 事件 | 处理 |
| --- | --- |
| Broker 崩溃后重启 | `DISPATCHING` 一律转 `DELIVERY_UNCERTAIN`(§8);已是不确定态的保持不变。重启后从该目标 `queue_seq` 最小的未放行消息继续,**不得跳到下一条** |
| spool 文件损坏、读写失败 | fail-closed:隔离、报错、保持该目标暂停;不能当作失败跳过。**如果连它属于哪个目标都读不出来,Broker 全局停止投递**、隔离损坏数据并告警;恢复出目标归属后,才能缩小为只暂停该目标 |
| 审计写入失败 | 见 §7.3 规则 4 |
| 拓扑删除 IP、角色或边 | 不改写已入队的消息 |
| 目标 pane 关闭或从注册表注销 | 已不确定的消息保持不确定。(在**调用前**独立复核发现目标不存在的消息,按 §5 转 `TARGET_MISSING`,它是确定失败,仍按 §7.1 暂停等操作员) |
| Broker 内部的通用异常处理 | 不得把 `DELIVERY_UNCERTAIN` 改为 `FAILED`。调用 herdr 过程中的异常,只有能证明没有副作用时才走确定失败路径,否则进入 `DELIVERY_UNCERTAIN` |

### 7.3 操作员动作(v1)

针对队列头的动作:

| 动作 | 队列头是 `DELIVERY_UNCERTAIN` | 队列头是确定失败终态 |
| --- | --- | --- |
| 裁定已送达 | `→ DELIVERED`(`evidence = operator_confirmed`),放行 | 不适用(终态不可变)。若事后确认其实已送达,记 `ANNOTATION`,再用"放弃并继续"放行 |
| 重试 | 原消息 `→ RETRYING`,**不新建消息**,继续占住队列头 | **新建消息**:新的 `msg_id`,`retry_of` 指向原消息,**继承原 `queue_seq`**,经**受信任的重试入队路径**以 `QUEUED` 入队,占住队列头;原消息不变 |
| 放弃并继续 | 原消息 `→ FAILED`,放行 | 原消息状态不变,放行 |

规则:

1. **重试与放弃并继续互斥。** 同一次暂停中只能选一个;放行之后不能再对该槽位重试,避免把消息插回已放行的历史位置。
2. **重试 `TARGET_BLOCKED` 不违反 D8。** D8 约束的是 Broker **不自动**重试、不排队等待 blocked 的目标、不自动批准。操作员手动重试是允许的,但新消息照常在进入 `DISPATCHING` 前复核:目标仍然 `blocked`,就立即再次转 `TARGET_BLOCKED`,不排队、不自动批准。
3. **新建的重试消息继承原消息的授权**:原样复制 `edge_id`、`src`、`dst`、`text`、`topology_revision`,**不按当前拓扑重新鉴权、不重新渲染模板**。这与"拓扑变更不改写已入队消息"(§7.2)一致:授权在入队时完成一次。投递前仍照常复核目标是否存在、是否可投递(§4 规则 6),目标不在了就按 §5 转 `TARGET_MISSING`。 新消息由**受信任的重试入队路径**创建(v1 拟由 Router 提供,模块归属在 Broker 阶段定):它接收已落盘的 `OPERATOR_RULING`(含 `ruling_id`、`retry_msg_id`)和原消息的授权快照,用裁定里确定的 `retry_msg_id` 入队;操作员和 Broker 都不直接写消息状态。
4. **裁定先落盘,再生效。** `OPERATOR_RULING` 审计事件必须先可靠写入,然后才改变消息状态或放行队列。审计写不进去时,这个动作失败,消息状态和队列暂停都不变。
5. **裁定必须可以幂等补做。** 每次裁定有稳定的 `ruling_id`;终态后重试的新消息 `msg_id` 在**裁定时就确定**并写进该审计事件(`retry_msg_id`)。裁定已落盘、但状态变更或新消息还没写入时崩溃,Broker 重启后(§8 第 3 步)按 `ruling_id` 找出"已裁定未生效"的记录并补做:新消息已存在就不再创建,状态已变就不再迁移,队列已放行就不再放行。补做本身也记审计。
6. **裁定的所有副作用都以 `ruling_id` 幂等。** 由裁定触发的状态迁移、新消息入队、`QUEUE_RELEASED` 等事件都**必须**带上 `ruling_id`(§9);补做前先查该 `ruling_id` 的效果是否已持久化,已有的不重复执行、不重复记账。`RULING_APPLIED` **只在全部效果都可靠持久化之后**写入,它是"这次裁定已完成"的唯一标志。
7. **裁定记录读不出来时 fail-closed。** 恢复时如果相关的 `OPERATOR_RULING` 审计记录损坏或不可读,Broker 不得猜测裁定结果,也不得放行队列:保持该目标暂停(读不出目标时按 §7.2 全局停止)并告警,交操作员重新裁定。
8. **解除 fail-closed 的人工流程。** 自动流程不能解除,只能由操作员执行并记审计:
   - **作废旧裁定**:操作员先核实旧裁定的哪些效果已经持久化(例如 `retry_msg_id` 对应的消息是否已入队、队列是否已放行),再执行"作废",记 `RULING_VOIDED`(`voided_ruling_id`;读不出 ID 时记损坏数据在 `.corrupt` 中的位置)、`actor`、`reason`、核实结果。作废**只停止补做,不撤销已持久化的效果**,也**不把目标"回到"任何状态**:作废之后,Broker 以**已持久化的实际状态**(消息状态、槽位放行记录、`queue_seq`)重新确定该目标的队列头,操作员对**实际的**队列头按 §7.3 处理(新的 `ruling_id`)。按实际状态分以下几种:
     - 旧裁定的效果都没有生效:队列头仍是原消息,原状态不变,操作员正常重新裁定。
     - 原消息已迁到 `DELIVERED`:按 §7.1 自动放行,无需再裁定。
     - 原消息已迁到 `RETRYING`,或重试消息(`retry_msg_id`)已入队:由它继续占住队列头,按正常流程投递,不再新建重试消息。
     - 原消息已迁到 `FAILED`(放弃),但槽位没有放行记录:队列头是一条未放行的终态消息,操作员可选"放弃并继续"。若改选"重试",必须参考作废时的核实结果:原消息曾经是不确定态,重试可能造成重复投递。
     - 槽位已有放行记录:该槽位已结束,队列头是下一条消息,不能再对旧槽位重试(§7.3 规则 1)。
   - **恢复全局投递**:§7.2 的全局停止,只有在损坏数据已隔离、目标归属已恢复或已逐个作废之后,由操作员执行"恢复投递",记 `DISPATCH_RESUMED`(`actor`、`reason`、隔离数据的位置)。

### 7.4 排序键 `queue_seq`

1. 每个目标的消息有**持久、单调递增**的 `queue_seq`,入队时在锁或事务内**原子分配**;重启后不重排、不复用。
2. `msg_id`(含 `time_ns`)和入队时间都**不作为**严格顺序键:时钟回拨和并发入队会破坏它们的顺序。
3. 重试已终结的消息时,新消息有新的 `msg_id`、带 `retry_of`,但**继承原消息的 `queue_seq`**,占住原槽位。
4. 同一槽位任何时刻**最多一条未终结的消息**(即最多一个活动重试)。
5. 该槽位放行之前,不投递 `queue_seq` 更大的消息。

**实现现状:** 当前 `Spool.pending()` 按 `msg_id` 排序,没有 `queue_seq`。实现留到 Broker 阶段(需改 `Router` / `Spool`),见 §10。

### 7.5 将来扩展(v1 不做)

- **取消后续消息**:需要新增终态 `CANCELLED`,会改迁移表(协议版本 3)。只允许取消尚未开始派发的消息,且必须明确列出被取消的 `msg_id`,不得按目标或边批量推断。
- **按边配置 `continue_on_failure`**:某些边确认失败后可以继续时使用。
- **`workflow_id` / 依赖分组**:用来缩小暂停范围,不必停掉整个目标。

## 8. 崩溃恢复规则

Broker 启动时必须按顺序做:

1. `Spool.recover()`:清理"已有终态记录却还留在 `pending/`"的重复文件和遗留临时文件。
2. 把所有停在 `DISPATCHING` 的消息迁到 `DELIVERY_UNCERTAIN`。`DISPATCHING` 是写前标记,Broker 上次崩溃时 prompt 可能已经发出、还没记账,**不能当作没发过**。
3. 对账操作员裁定:找出已落盘、既没有对应 `RULING_APPLIED` 也没有被 `RULING_VOIDED` 作废的 `OPERATOR_RULING`,按 `ruling_id` 幂等补做(§7.3 规则 5、6);裁定记录不可读时 fail-closed,等操作员按 §7.3 规则 8 处理。
4. 恢复每个目标的槽位放行记录(§7.1 第 5 条);读不出目标归属的损坏数据按 §7.2 全局停止投递。
5. 每个目标从 `queue_seq` 最小的未放行槽位(队列头)继续,**不得跳到下一条**(§7.1、§7.2)。不确定的消息按 §6 处置:默认不自动重发,等待操作员裁定。

即使没有调用 `recover()`,`Spool.pending()` 也会过滤掉已在 `done/` 有终态记录的消息,所以崩溃留下的重复文件不会被再次投递。

## 9. 审计事件与证据种类

追加写的 JSON Lines,每行一个事件。通用字段:`ts`(UTC)、`msg_id`、`edge_id`、`src`、`dst`、`state`、`detail`、`session`、`topology_revision`。

**`DELIVERED` 的证据种类(`evidence` 字段)**——不把推断写成"已确认":

| `evidence` | 含义 |
| --- | --- |
| `accepted_and_observed` | 自动路径:herdr 接受了 prompt,**并且**观察窗口内观察到目标开始处理(进入 `working`)。这是**操作性证据**:它**不是**与 `msg_id` 绑定的送达回执——观察到 `working` 不能证明是这条消息引起的(目标可能本来就在被别的原因触发)。适用条件在真实验证前保持保守,窗口内的并发因素需在 Broker 实测时评估 |
| `operator_confirmed` | 操作员明确裁定 |
| `verified_mechanism` | 经真实 herdr 验证的机制确认(目前没有) |

herdr 接受了但观察窗口内没看到目标开始处理 → **不记 `DELIVERED`**,进入 `DELIVERY_UNCERTAIN`(规则见 §3)。观察窗口长度**暂定 30 秒,可配置,待 Broker 实现时实测**(此前设计值为 5 秒,探测中模型开始处理通常不到 1 秒)。30 秒是实验值,不是可靠送达保证。

| 事件 | 说明 |
| --- | --- |
| `state = QUEUED` | 鉴权通过,已入队 |
| `state = REJECTED` + `reject_code` | 鉴权失败。身份未核实时 `src` 为空,环境变量声称的身份记在 `claimed_project` / `claimed_role` / `claimed_ip` / `claimed_pane`(不可信,仅供排查) |
| Broker 的每次状态迁移 | 记录新状态、原因,以及(迁到 `DELIVERED` 时)`evidence` |
| `OPERATOR_RULING` | **人工裁定必须记录**,**先落盘,动作才生效**(§7.3 规则 4、5)。公共字段:`ruling_id`(稳定、唯一,用于幂等补做)、`actor`(谁裁定的)、`ruling`、`reason`、`msg_id`(被裁定的队列头)、`queue_seq`、`previous_state`、`new_state`。`ruling` 的取值与字段语义见下表 |
| `RULING_VOIDED` | 操作员作废一条无法补做的裁定(§7.3 规则 8):`voided_ruling_id` 或损坏数据位置、`actor`、`reason`、作废前核实到的已生效效果 |
| `DISPATCH_HALTED` / `DISPATCH_RESUMED` | 全局停止投递与人工恢复(§7.2、§7.3 规则 8):原因、`actor`(恢复时)、隔离数据的位置 |
| `RULING_APPLIED` | 一次裁定的**全部效果都已持久化**后才写入:`ruling_id`、实际执行了哪些步骤(状态迁移 / 创建新消息 / 放行);重启补做时 `detail` 注明"恢复时补做"。裁定引起的其他事件(状态迁移、`QUEUE_RELEASED`)也都带 `ruling_id` |
| `QUEUE_PAUSED` / `QUEUE_RELEASED` | 目标队列暂停与放行:`dst`、队列头的 `msg_id` 与 `queue_seq`;**由裁定触发时必须带 `ruling_id`**,与该裁定的 `RULING_APPLIED` 对应;原因(例如"队列头 DELIVERY_UNCERTAIN"、"队列头 TIMEOUT"、"操作员放弃并继续") |
| `DELIVERY_UNCERTAIN` 期间观察到 `blocked` | 事件 `state = DELIVERY_UNCERTAIN`,`detail` 写明"目标当前 blocked(可能已收到消息后停在审批)";**不改变状态** |
| 终态之后才发现的真相 | 例如 `FAILED` 之后确认其实已送达:记一条注解事件(`ANNOTATION`),**不修改消息状态**(终态不可变) |
| `state = AUDIT_REPAIRED` | 上次写入中途崩溃,不完整的尾行已隔离到 `audit.jsonl.corrupt` |
| `state = CORRUPT_LINE` | 只会出现在 `read()` 的返回值里:日志中间有一行无法解析,`detail` 是该行前 200 个字符 |

`ruling` 的取值(与 §7.3 的动作一一对应):

| `ruling` | §7.3 的动作 | 队列头状态 | `previous_state` → `new_state` | 附加字段 | 是否放行 |
| --- | --- | --- | --- | --- | --- |
| `delivered` | 裁定已送达 | `DELIVERY_UNCERTAIN` | `DELIVERY_UNCERTAIN` → `DELIVERED` | `evidence = operator_confirmed` | 是 |
| `not_delivered_retry` | 重试 | `DELIVERY_UNCERTAIN` | `DELIVERY_UNCERTAIN` → `RETRYING` | — | 否(仍占队列头) |
| `abandon` | 放弃并继续 | `DELIVERY_UNCERTAIN` | `DELIVERY_UNCERTAIN` → `FAILED` | — | 是 |
| `retry_terminal` | 重试 | 确定失败终态 | 原状态 → **原状态(不变)** | `retry_msg_id`(新消息,裁定时确定)、`retry_initial_state = QUEUED`、新消息继承的 `queue_seq` | 否(新消息占住同一槽位) |
| `abandon_and_continue` | 放弃并继续 | 确定失败终态 | 原状态 → **原状态(不变)** | — | 是 |

`reject_code` 取值:

| 代码 | 含义 |
| --- | --- |
| `identity` | 发送方身份不成立(缺环境变量、冒充、pane 未登记、非 running、不在拓扑里、会话不对) |
| `unknown_edge` | 拓扑里没有这条边 |
| `wrong_direction` | 这条边不允许当前角色作为发送方 |
| `target_not_in_topology` | 目标节点不在拓扑里(纵深防御) |
| `target_missing` | 目标没有登记;或登记在**另一个 herdr 会话**;或登记信息与发送方不属于同一 IP / 项目 |
| `target_not_running` | 目标已登记,但 `lifecycle` 不是 `running` |
| `bad_message` | 渲染后的消息为空、过长(默认 > 1000 字符)或含控制字符 |

## 10. 待确认与待验证

| # | 事项 | 状态 |
| --- | --- | --- |
| 1 | 不确定态超时自动 `FAILED` / 自动放行 | **已否决(用户确认)**:v1 没有此开关;超时只告警 |
| 2 | 队列头模型:队列头未放行时暂停该目标整条队列;只有 `DELIVERED` 自动放行(§7.1) | **已确认(用户)** |
| 2a | 确定失败终态默认暂停并告警,等操作员"重试"或"放弃并继续"(§7.1、§7.3) | **已确认(用户)** |
| 2b | 取消后续消息(`CANCELLED`)、`continue_on_failure`、`workflow_id` 依赖分组 | v1 不做,列为将来扩展(§7.5) |
| 3 | 告警策略:1 小时提醒、24 小时升级;重复间隔 | 暂定,可配置;只告警,不是状态迁移 |
| 4 | §5 中 `agent_blocked`、`agent_not_ready`、`server_not_running`、调用后 `agent_not_found` 的"未提交"语义 | **已实测(2026-10-09,herdr 0.9.3)**:四者均未写入;另发现 `agent_prompt_failed` 会在已写入后返回。升级 herdr 后需重测 |
| 5 | `state_change_seq` 作为核实依据的可靠性 | 待在真实 herdr 上验证;验证前不得自动使用 |
| 6 | 观察窗口长度 | 暂定 30 秒,可配置,待 Broker 实测 |
| 7 | 状态查询的退避与上限(1 秒起、每次加倍、单次 ≤ 30 秒;连续 5 次;总等待 300 秒) | 暂定,可配置,待 Broker 实测 |
| 8 | 操作员裁定的命令行形式(例如 `a2a resolve <msg_id> ...`)与 `actor` 的来源 | 待设计 |
| 8a | 终态后重试新建的消息是否重新鉴权 | **已定**:继承原授权,不重新鉴权,投递前照常复核目标(§7.3 规则 3) |
| 8c | 槽位放行记录、裁定对账的持久化方式(§7.1 第 5 条、§7.3 规则 5) | Broker 阶段设计 |
| 8b | `queue_seq` 的实现(改 `Router` / `Spool`,§7.4) | Broker 阶段实现 |
| 9 | `a2a` 的迁移表对齐(`messages.py`)与 `a2a_codex` 的对齐 | `a2a` **已对齐**(2026-10-09);`a2a_codex` 由 Codex 自行对齐,**`a2a` 已对齐不代表 `a2a_codex` 已对齐** |

## 11. 修订记录

- **版本 2(2026-10-09)**,采纳 `herdr/codex/对08协议的复核与a2a_codex对齐方案.md` 的复核意见,经用户确认:
  - `DELIVERY_UNCERTAIN` 默认不自动重发;出口加守卫(操作员裁定,或经真实 herdr 验证的机制);人工裁定必须记审计。
  - `RETRYING` 期间发现 `blocked` / 目标不存在,立即转 `TARGET_BLOCKED` / `TARGET_MISSING`(新增两条边);`DELIVERY_UNCERTAIN` 期间发现 `blocked` 只记入 `detail` 和审计,不改变状态。
  - **删除** `DISPATCHING → WAITING_TARGET`:目标复核放在进入 `DISPATCHING` 之前;调用 herdr 之后结果不明就进入 `DELIVERY_UNCERTAIN`。
  - 明确只有能证明 prompt 未提交的错误才能安全重试,新增 §5 错误分类表;`agent_not_ready` 等先确认实际语义,不能只按错误名分类。
  - `state_change_seq` 作为待验证的候选依据,不写成可靠的自动核实机制。
  - herdr 接受但观察窗口内没看到目标开始处理 → `DELIVERY_UNCERTAIN`。
  - 新增 `DELIVERED` 的证据种类(`evidence`)和 `OPERATOR_RULING` 审计事件。
- **版本 2 补充决议(2026-10-09,迁移表不变)**,依据 Codex 的复核与用户确认:
  - **不确定态不因超时转 `FAILED`,也不提供"超时自动放弃并放行"的开关(v1)。** 超时只触发告警(1 小时提醒、24 小时升级,暂定,只告警不迁移)。同一目标的后续消息等当前消息经操作员裁定并处理到终态后再投递(规则 12、13;补充决议二细化为队列头模型)。
  - 经验证确认未送达且允许重试 → `RETRYING`;`FAILED` 只用于操作员放弃或确定不可恢复(补充决议二收窄:在 `DELIVERY_UNCERTAIN` 下只能由操作员放弃)。
  - `agent_not_found` 拆成两种:调用前独立复核发现目标不存在(可证明,`TARGET_MISSING`);调用后收到(待验证,证实前走 `DELIVERY_UNCERTAIN`)。
  - 目标状态查询临时错误:留在当前状态,加退避,受"连续失败 5 次"与"总等待时限"约束,达到即 `TIMEOUT`;`TIMEOUT` 也涵盖状态查询持续失败。
  - `accepted_and_observed` 是操作性证据,不是与 `msg_id` 绑定的送达回执;观察窗口暂定 30 秒,可配置。
- **版本 2 补充决议二(2026-10-09,迁移表不变)**,经与 Codex 三轮复核并由用户确认:
  - 新增 §7 目标队列:队列头模型;只有 `DELIVERED` 自动放行;暂停依据是"队列头未放行",不是状态名,`RETRYING` 期间继续占住队列头。
  - 确定失败终态(`FAILED` / `TIMEOUT` / `TARGET_BLOCKED` / `TARGET_MISSING`)默认也暂停并告警,等操作员"重试"或"放弃并继续";两者互斥。
  - `DELIVERY_UNCERTAIN → FAILED` 只能由操作员放弃;§2 的通用规则不覆盖它;崩溃恢复、spool 损坏、拓扑变更、目标注销、通用异常处理都不能证明未送达(§7.2)。
  - 不确定态的重试直接转 `RETRYING`,不新建消息;终态后的重试新建消息,带 `retry_of`,继承原 `queue_seq`。重试 `TARGET_BLOCKED` 不违反 D8(只禁止自动重试),新消息照常复核。
  - 裁定先落盘再生效;新增 `QUEUE_PAUSED` / `QUEUE_RELEASED` 审计事件。
  - 规定 `queue_seq` 语义(§7.4),实现留到 Broker 阶段。
  - 取消后续消息(`CANCELLED`)、`continue_on_failure`、依赖分组列为将来扩展。
  - 原 §7–§11 顺延为 §8–§12。
- **版本 2 补充决议三(2026-10-09,迁移表不变)**,依据 Codex 对补充决议二的审查(1 条阻断、5 条应修)和 Claude 自查:
  - D8 的表述统一为"Broker 不自动重试;操作员显式裁定的重试是例外"(§1、页首,与 §7.3 一致)。
  - 裁定带 `ruling_id`,终态重试的新 `msg_id` 在裁定时确定;崩溃后按 `ruling_id` 幂等补做(§7.3 规则 5、§8 第 3 步);新增 `RULING_APPLIED` 事件。
  - 终态重试继承原授权,不按当前拓扑重新鉴权(§7.3 规则 3)。
  - `OPERATOR_RULING` 增加 `ruling` 取值表,写明终态重试"原消息状态不变"的表示法。
  - spool 损坏读不出目标时全局停止投递(§7.2)。
  - Claude 自查:终态消息归档到 `done/` 不等于放行;槽位放行状态必须持久记录(§7.1 第 5 条、§8 第 4 步)。
  - 测试增加跨章节一致性检查;队列排序、暂停与放行、重试继承序号、恢复幂等的**行为测试**留到 Broker 实现后。
  - 依据 Codex 对补充决议三的复审(无阻断,3 条应修):终态重试经 Router 的重试入队路径创建,操作员不直接写状态(§1、§7.3 规则 3);裁定副作用都以 `ruling_id` 幂等,`RULING_APPLIED` 只在全部效果持久化后写入,裁定记录不可读时 fail-closed(§7.3 规则 6、7);测试改为逐项断言每个 `ruling` 的语义。
  - 依据 Codex 的再次复审(2 条应修):新增解除 fail-closed 的人工流程——作废旧裁定(`RULING_VOIDED`,只停止补做、不撤销已生效效果;作废后以已持久化的实际状态重新确定队列头,不"回到"任何状态,按实际状态分 5 种情况处理)与恢复全局投递(`DISPATCH_RESUMED`)(§7.3 规则 8);裁定触发的 `QUEUE_PAUSED` / `QUEUE_RELEASED` 必须带 `ruling_id`;"Router 的重试入队路径"改为"受信任的重试入队路径",模块归属留到 Broker 阶段。
- **定稿(2026-10-09)**:Codex 复审结论"可以定稿";`herdr/a2a/src/a2a/messages.py` 迁移表已对齐。Broker 阶段待办:故障注入测试,覆盖作废前后各副作用已持久化的崩溃点(Codex,非必要)。
- **错误码实测(2026-10-09,迁移表不变)**:§4 规则 7 同步改为引用实测结果(Codex 指出遗漏)。§5 的四个"待验证"项在 herdr 0.9.3 上实测为"未写入":`agent_blocked` → `TARGET_BLOCKED`,调用后的 `agent_not_found` → `TARGET_MISSING`,`agent_not_ready` 与 `server_not_running` → `RETRYING`。新增 `agent_prompt_failed`(实测已写入)→ `DELIVERY_UNCERTAIN`。结论绑定 herdr 版本。详见 `09` 号文档。
- **版本 1(2026-10-09)**:初版。

## 12. 与 `herdr/a2a_codex` 对齐

`a2a_codex/messages.py` 目前没有 `DELIVERY_UNCERTAIN`、`NON_TERMINAL_STATES` 和迁移表,`Spool.update` 只校验状态名、不校验迁移。按 `herdr/codex/对08协议的复核与a2a_codex对齐方案.md` 的顺序:先冻结本协议语义(版本 2 即此),再在 `a2a_codex` 镜像迁移表并由 `Spool` 强制,然后做 Broker 与恢复,测试由 `codex-test` 在虚拟机执行。两个实现各自维护一份迁移表和一致性测试,不在运行时互相导入。
