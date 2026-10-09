# 对 08-protocol 的复核与 a2a_codex 对齐方案

日期：2026-10-09

## 结论摘要

我认可协议的主方向：显式状态迁移、终态不可变、按目标 FIFO，以及崩溃恢复时不把未完成的 `DISPATCHING` 当成“肯定没发出”。`DELIVERY_UNCERTAIN` 是必要状态，能表达 Herdr prompt stalled、客户端等待超时和 Broker 崩溃窗口中的真实不确定性。

但我不建议在当前文字不变的情况下直接照表实现。协议 §4、§5 有一项安全语义冲突：没有观察到送达证据，并不能证明没有送达；而 §5 第 4 步却允许“没有证据且目标 READY”进入 `RETRYING`。此外，“任一阶段发现 blocked 都转 `TARGET_BLOCKED`”与迁移表不完全一致。应先收紧这两处规则，再把表作为 a2a_codex 的唯一迁移契约。

本次只做了文档和代码阅读，没有运行测试，也没有修改 `herdr/a2a`、`herdr/claude` 或 `herdr/a2a_codex`。按既定分工，之后的测试由 `codex-test` 在虚拟机执行。

## 1. 对协议设计的独立判断

### 1.1 状态表与终态规则：同意

11 个状态的职责总体清楚：`QUEUED` 是唯一入队初态，`REJECTED` 只进入审计，投递期间用 `WAITING_TARGET` / `DISPATCHING` / `RETRYING` 表达过程；`DELIVERED` 等终态没有出口。`DELIVERY_UNCERTAIN` 不应被合并到 `RETRYING` 或 `FAILED`，因为它保留了“是否产生外部副作用未知”这一关键信息。

代码证据：`herdr/a2a/src/a2a/messages.py:14-41` 把状态分类并集中定义 `ALLOWED_TRANSITIONS`；`herdr/a2a/tests/test_protocol.py:40-94` 检查状态分类、终态无出口、不可回到 `QUEUED`、不确定态不能直接转 `DISPATCHING` 等性质。文档机器可读表与代码一致性测试位于 `test_protocol.py:108-128`。

这张表适合作为 Broker 的**候选契约**，但迁移合法不等于迁移条件满足：例如表允许 `DELIVERY_UNCERTAIN → RETRYING`，Broker 仍必须证明此次重发安全。

### 1.2 `DELIVERY_UNCERTAIN`：同意引入，部分不同意当前自动核实规则

Herdr stalled 或客户端等待超时都可能发生在 prompt 已提交之后。`herdr/a2a_codex` 已有对应的异常语义：`errors.py:59-70` 将 `agent_prompt_stalled` 映射为 `HerdrPromptOutcomeUnknown`；`herdr_client.py:215-234` 将 stalled 和 Herdr 调用超时按“可能已送达”向上传递。因此用 `DELIVERY_UNCERTAIN` 暂存结果、阻止直接重发，是合理且必要的。

我不同意 §5 当前第 4 步把“未看到输出 / 未观察到 working，且目标当前 READY”当成“确认未送达”。这是负面证据，不是未送达证明：全屏 TUI 可能不呈现历史，状态采样可能漏掉短暂的 `working`，目标也可能已处理完并回到 `idle`。协议自己已在 §5:103 标注核实方法未经真实 Herdr 验证，且 §5:109 采用“宁可失败，也不重复投递”。所以，在没有可关联到该 `msg_id` 的 Herdr 确认机制前：

- 有可靠的正面证据可确认送达时，才转 `DELIVERED`。
- 只有可靠地证明 prompt 没有提交时，才可转 `RETRYING`。
- 其余情况转 `FAILED` / 人工处置，不自动重发。单凭“没有看到”不能满足安全重试条件。

这不否定迁移表中的 `DELIVERY_UNCERTAIN → RETRYING`；它要求协议把这条边的**守卫条件**写成“确认未提交”，而不是“没有观察到送达”。在真实 Herdr 上验证 §5 的观察能力之前，Broker 应默认 fail closed。

### 1.3 D8 `blocked` 规则：原则同意，状态覆盖有缺口

“blocked 立即失败、不自动批准、不等待”符合用户 D8。协议 §4.4 说“任何阶段”发现 `blocked` 都直接进入 `TARGET_BLOCKED`（`08-protocol.md:93`），但迁移表只有 `QUEUED`、`WAITING_TARGET`、`DISPATCHING` 有到 `TARGET_BLOCKED` 的边，没有 `RETRYING` 或 `DELIVERY_UNCERTAIN` 的边（文档 `:45-49`）。对照测试也只验证前三个状态（`herdr/a2a/tests/test_protocol.py:76-82`）。

`RETRYING` 时如检查目标发现 blocked，按 D8 应立即失败，当前表无法表达。`DELIVERY_UNCERTAIN` 时则不能简单改成 `TARGET_BLOCKED`：prompt 可能已经送达；协议应明确如何同时满足“不再投递”和不误报“确定未送达”。建议让协议作者明确此情形的语义（例如进入一个保留不确定性的人工失败路径），并同步迁移表/性质测试；不要在 a2a_codex 中私自补一条边。

### 1.4 迁移触发条件：总体同意，需防止把调用后状态检查误当作未投递

明确未提交的临时错误进入 `RETRYING`、`agent_blocked` 进入 `TARGET_BLOCKED`、崩溃遗留的 `DISPATCHING` 进入 `DELIVERY_UNCERTAIN`，逻辑成立。需要补充边界：`DISPATCHING → WAITING_TARGET` 只能用于 prompt 调用尚未产生副作用、且发送前复核发现目标不再 READY 的场景；prompt 命令已经调用后若响应丢失/超时，应进入 `DELIVERY_UNCERTAIN`，不能因当前状态不是 READY 就转回等待并再发一次。

另外，“`DELIVERED` 表示观察到目标进入 `working`”仅是当前协议采用的确认定义，不等同于 Herdr 提供了与该消息绑定的送达回执。状态变化可能被其他操作触发，也可能采样遗漏；若没有事件与消息的关联键，应在审计里区分“Herdr 接受”“观察到 working”“推断已送达”，避免把推断写成端到端 exactly-once 保证。

### 1.5 FIFO、恢复顺序、审计：同意，Broker 阶段再落地

按目标串行化有实际依据：路线图记录同时对忙碌 agent 发送 prompt 会被接受、但处理顺序不确定。因此锁/worker 必须按目标维度串行，而且 Spool 锁不能跨越 Herdr 等待或 prompt 调用。

恢复顺序“先清理队列文件，再把遗留 `DISPATCHING` 隔离到 `DELIVERY_UNCERTAIN`，最后核实”合理。它避免把崩溃窗口中的副作用当作未发生。审计每次状态变化也合理；`REJECTED` 只留审计、不入队，与既定 D11/D1 边界一致。

## 2. `a2a_codex` 当前差距

1. `herdr/a2a_codex/messages.py:10-22` 只有 10 个状态，没有 `DELIVERY_UNCERTAIN`、`NON_TERMINAL_STATES` 或迁移表。
2. `herdr/a2a_codex/spool.py:100-123` 的 `Spool.update()` 仅检查新状态是否属于 `ALL_STATES`，允许任意状态跳转；终态记录也可能被更新或改回 pending 状态，尚未实现协议 §2 的终态不可变。
3. `HerdrPromptOutcomeUnknown` 已能从 HerdrClient 向上抛出，但尚无 Broker 消费它并将消息写成 `DELIVERY_UNCERTAIN`。
4. Spool 初始化恢复目前清理临时文件及 pending/done 重复记录；没有 Broker 启动流程把遗留 `DISPATCHING` 转为不确定态。见 `spool.py:26-58`。
5. 现阶段没有 Broker；`Router` 只鉴权并入队。因此不能声称已有真实 Herdr 投递或不确定态核实能力。阶段路线图也把 Broker 留在未完成项。

## 3. 对齐方案与顺序

### 阶段 A：先冻结协议语义（不改其他目录）

在代码对齐前，请协议维护者确认并更新协议文档/对照测试；本轮我不修改这两个目录。至少明确：

- `DELIVERY_UNCERTAIN → RETRYING` 的守卫是“可靠确认未提交”，不是“没观察到送达”；真实 Herdr 无法确认时必须 fail closed。
- `blocked` 在 `RETRYING` 与 `DELIVERY_UNCERTAIN` 的处置，保持 D8 且避免把不确定投递误标为确定的 `TARGET_BLOCKED`。
- `DISPATCHING → WAITING_TARGET` 只允许在 prompt 调用前；调用后结果不明必须走 `DELIVERY_UNCERTAIN`。
- `DELIVERED` 的确认证据及审计措辞，区分 Herdr 接受与仅观察到状态变化。

若协议维护者决定保持当前表不变，也应先把这些情况写成明确限制/优先级，避免 Broker 实现者各自推断。

### 阶段 B：在 `herdr/a2a_codex` 镜像状态模型和转移约束

确认协议后，只修改 `herdr/a2a_codex`：

- 在 `messages.py` 加入 `DELIVERY_UNCERTAIN`、`NON_TERMINAL_STATES`、协议确认后的 `ALLOWED_TRANSITIONS`、`can_transition()`；保持 `REJECTED` 仅用于审计且不进入 Spool。
- 由 Spool 强制状态迁移：拒绝未知状态、非法边、终态任何字段修改/回退；同状态更新仅按协议允许的非终态字段处理。合法迁移到终态时继续原子归档到 `done/`。
- 明确旧队列兼容策略：现有消息仅含既有状态；新增状态不需改写旧 JSON，但未知/损坏状态必须 fail closed。若未来需要状态枚举迁移，应另做显式、可审计的数据迁移。
- 增加协议表性质测试：状态分类无交集且并集完整、终态无出口、所有非终态能到失败、无路径绕过 `DISPATCHING` 进入 `DELIVERED`、不确定态不能直接回 `DISPATCHING`、终态不可变。把文档逐行同步检查用于 a2a_codex 自身的协议副本/测试夹具；不直接导入 `herdr/a2a`，避免两条实现主线形成运行时耦合。

### 阶段 C：Broker 与崩溃恢复

- 单目标串行 worker/锁；锁只保护本地队列操作，不持锁等待 Herdr。
- 发送前检查目标状态；prompt 一旦调用，任何超时、stalled、进程中断或无法证明未提交的错误都进入 `DELIVERY_UNCERTAIN`，禁止自动重发。
- 启动顺序：Spool 文件恢复 → 把残留 `DISPATCHING` 转成 `DELIVERY_UNCERTAIN` 并记审计 → 核实/人工处置。恢复步骤必须幂等。
- 只有协议允许且有可审计的“明确未提交”证据时，才从 uncertain 重试；单纯未读到输出或当前 READY 不足以证明安全。
- 在目标锁释放前完成当前消息的确定分类，避免同一目标后续消息越过尚未分类的投递。

### 阶段 D：交由 codex-test 在 VM 验证

至少覆盖：每条合法迁移成功、每条非法迁移失败、所有终态字段不可改、`REJECTED` 不入队、`DELIVERY_UNCERTAIN` 不会被自动重新投递、Herdr stalled/客户端超时映射 uncertain、启动恢复把 DISPATCHING 转 uncertain 且可重复运行、崩溃窗口与并发同目标消息 FIFO/串行。测试报告须区分纯单元/真实文件系统/真实 Herdr pane E2E；核实规则未经真实 Herdr 验证时，不将其标成真实投递验证。

## 4. 建议结论

可以接受 `DELIVERY_UNCERTAIN` 以及“崩溃后先隔离、不盲目重投”的主设计；不能把“没有看到送达”当作“确认没有送达”。在协议把这条守卫和 `blocked` 边界说清楚后，再按同一状态表对齐 `a2a_codex`，由 Spool 保证迁移约束，后续 Broker 负责迁移触发条件、恢复、审计和投递串行。实现后的测试仍由 `codex-test` 在虚拟机执行。
