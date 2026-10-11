# 阶段 8 方案:注册表自动恢复(草案,待用户确认、Codex 审核)

## 1. 目标

VM 重启、或 herdr 会话停止后重新启动时,注册表里还记着各 agent(lifecycle = running、旧的 pane ID),但 agent 已经不在了。现在只能对每个 agent 手工执行 `a2a agent restore`。目标是**一条命令把应该在运行的 agent 全部恢复**,并清楚报告每个 agent 的结果和需要人工处理的事项。

## 2. 现状

- `a2a agent restore <role> <ip>` 已能恢复单个 agent:记录不是 closed 且原 pane 还在,就在原 pane 重启;否则新建 pane;重新改名、更新注册表、写 `AGENT_RESTORED`。已受 IP 排空门禁约束(17 号方案)。
- broker 启动时已有恢复流程(08 §8):停在 DISPATCHING 的消息转 DELIVERY_UNCERTAIN、补做未生效的裁定等。
- agent 不在时,broker 投递给它的队列头会判 `TARGET_MISSING` 并暂停,等操作员裁定(08 §7.2)。所以**先恢复 agent、再启动 broker** 可以少产生这类暂停。

## 3. 先探测(不调用模型):herdr 会话重启后还剩什么

两个未知数决定方案的走向,先用独立会话实测(假 agent + 真实 fnx,不发提示词):

1. `herdr session stop` 再 `attach` 后:workspace / tab / pane 是否还在、pane ID 是否不变、pane 里的进程是否还在、agent 名字是否还在。
2. herdr 是否会用 `report-agent` 的 `RESUME_ARG` 在重启后自动重新执行命令(即 herdr 自己能否拉起 agent)。

另外记录:VM 重启与 `session stop` 在这方面是否等价(VM 重启无法在测试里自动做,只记为假设,由用户在合适时机手工验证)。

### 3.1 探测结果(2026-10-11,herdr 0.9.3,假 agent + 真实 fnx_sw,未调用模型)

| 观察 | 结果 |
| --- | --- |
| `herdr session stop` 后 | 原 pane 里的前台进程(假 agent、fnx)**全部退出** |
| 重新 `attach` 后的布局 | workspace / pane **全部保留,pane ID 不变** |
| 重新 `attach` 后 pane 里 | 每个 pane 都是**新的 shell**(进程信息:进程组 = shell_pid、前台只有 `/bin/bash`,与 17 号方案"只剩 shell"的严格判定一致) |
| agent 名字 | **全部丢失**(`agent list` 为空) |
| `report-agent ... -- <命令>`(RESUME_ARG) | **会被执行**:重启后 herdr 在该 pane 里自动运行了这条命令 |

结论:
- 采用 §4.1。会话重启后,记录为 running 的 agent 都会被分类为"需要恢复"(名字找不到、原 pane 在且只剩 shell),`restore` 走"原 pane 重启",布局不变。
- 不使用 RESUME_ARG 让 herdr 自动拉起 fnx:那样会绕过 a2a 的 IP 门禁、改名与注册表更新(本插件上报状态时没有带 RESUME_ARG,所以不会被自动拉起)。列为将来考虑。
- VM 重启与 `session stop` 是否等价仍是假设(VM 重启后 herdr 会话本身也要重新 attach),由用户方便时手工验证。

## 4. 设计(§3.1 探测后确定采用 §4.1)

### 4.1 新命令 `a2a agent recover --session <会话> [--ip <ip>] [--dry-run] [--include-stopped]`(Codex 审核后修订)

1. **前提**:
   - herdr 会话在运行(否则提示先 `herdr session attach`,不自动启动会话);
   - **该状态目录没有运行中的 broker**(Codex 审核):recover 以非阻塞方式拿 broker 的单实例锁(`<状态目录>/broker.lock`,与 `a2a broker run` 相同),拿不到就拒绝并提示先停 broker;拿到后**全程持有**,恢复期间 broker 也启动不了。否则 broker 可能在恢复过程中把目标缺失的队列头判为 `TARGET_MISSING` 并暂停,恢复 agent 不会自动解除暂停。`--dry-run` 只检查不拿锁。
2. **只处理本会话的记录**(Codex 审核):只看 `record.session == --session` 的记录;其他会话的记录列为"属于其他会话,未处理",不查询、不恢复(避免把别的会话的记录误判为缺失后拉到当前会话,或因 pane ID / 名字碰巧一致误判为正常)。可再用 `--ip` 限定。
3. **分类**(pane 核验沿用 17 号方案的判定:进程信息字段齐全且一致才算"只剩 shell"):
   - **正常**:running,按登记名字找到的 agent 就在登记的 pane 上 → 不动;
   - **需要恢复**:running,但 agent 不在(名字找不到),且 pane 不在或明确只剩 shell → `restore`;
   - **不确定**:running,但 pane 上有别的前台进程、进程信息不完整 / 矛盾、或名字在别的 pane 上 → 不动,报出,人工处理;
   - **按原意保留**:stopped / closed → 不动。`--include-stopped` 时 stopped 也恢复,但**同样先核验 pane**(Codex 审核):只有 pane 不在或明确只剩 shell 才恢复,否则归为不确定(避免把启动命令写进被别的进程占用的 pane);
   - failed → 报出,不自动重试;
   - 所在 IP 正在排空 → 跳过并报出。
4. `--dry-run` 只输出分类,不做任何改动;它不持 broker 锁,结果只是检查那一刻的快照,不保证随后执行 recover 时状态相同(Codex 复核提醒)。
5. 恢复按拓扑的角色顺序、逐个进行;某个失败不影响其他(恢复的目标是尽量多恢复),最后汇总;有失败、不确定时退出码非零。
6. 输出里附带:这些 agent 的队列里处于 `TARGET_MISSING` / `DELIVERY_UNCERTAIN` 的队列头及建议的 `a2a resolve` 命令(只提示,不自动裁定)。
7. 写 `AGENTS_RECOVERED` 审计(每个 agent 的分类与结果)。

另外修补 `Lifecycle.restore` 本身(`a2a agent restore` 也会用到):原 pane 还在、准备在原 pane 里重启时,先核验 pane 明确只剩 shell,否则拒绝(不往被占用的 pane 写启动命令)。这样 recover 与手工 restore 都受保护。

### 4.2 恢复时是否保留 fnx 的对话上下文(需要用户决定)

- **默认(建议)**:像现在的 `restore` 一样启动新会话。简单、可预期;对话上下文丢失,但消息和队列都在 a2a 这边,不受影响。
- **可选**:恢复时给 fnx 加 `--continue`(pi 支持,接着同一工作目录下最近的会话)。能保留上下文,但需要先实测 fnx 的 `--continue` 在重启后能否找到正确的会话、插件状态上报是否正常;而且同一工作目录下有多个会话时可能接错。作为将来考虑或单独开关。

### 4.3 如果探测发现 herdr 会自动保留 pane / 拉起 agent

那么 `recover` 主要是"核对 + 重新改名 + 更新注册表",而不是重新启动;或者改用 `report-agent` 的 `RESUME_ARG` 让 herdr 自己恢复。具体在探测后写入方案再审。

## 5. 测试

1. **单元测试**(假 herdr):每种分类各一例;`--dry-run` 不改任何东西;部分失败不影响其他、退出码非零;排空中的 IP 跳过;`--ip` 限定;队列提示的内容;**其他会话的记录不查询、不恢复**(含 pane ID / 名字碰巧一致的情况);**broker 在运行(锁被占)时拒绝**,recover 持锁期间 broker 拿不到锁;**`--include-stopped` 时 pane 被占用的 stopped 记录归为不确定**;`agent restore` 在原 pane 被占用时拒绝。
2. **真实 herdr 集成测试**(VM,假 agent,不调用模型):spawn 若干 agent → 停 broker → `herdr session stop` + 重新 attach → `a2a agent recover` → agent 全部恢复、名字正确、注册表更新;之后 broker 投递正常。另一例:stopped 的不被恢复。
3. **真实 fnx**(VM,不发提示词):同上流程用 fnx_dv / fnx_sw 走一遍。

## 6. 不在范围内

- 自动启动 herdr 会话本身(`herdr session attach` 需要终端,由用户或 systemd 负责);
- 自动裁定 TARGET_MISSING / DELIVERY_UNCERTAIN(只提示);
- 并行批量恢复(用户很少整体重启;沿用逐个恢复)。

## 7. 实现(2026-10-11)与实现中的新发现

实现:`src/a2a/recovery.py`(`Recovery.recover`、`broker_lock`)、`a2a agent recover --session ... [--ip] [--dry-run] [--include-stopped]`(broker 在运行时退出码 3;有失败 / 不确定时退出码 6);`Lifecycle.restore` 在原 pane 重启前用 `pane_shell_only` 核验(严格判定从 ipops 移到 lifecycle 共用)。

**实现中发现的缺陷(已修):会话重启后,持久化 pane 里新起的 shell 不再带创建 pane 时注入的环境变量。** 实测:创建时 `A2A_ROLE / A2A_IP / A2A_STATE_DIR` 都在;`session stop` + 重新 attach 后只剩 `HERDR_PANE_ID / HERDR_SESSION`。因此在原 pane 恢复的 agent 会**丢失身份**(插件不生效、发送被 Router 拒绝);手工 `agent restore` 同样受影响。修法:`Lifecycle._launch` 把 `_agent_env`(身份 + 插件所需变量)作为 `KEY=值`(shell 转义)写在启动命令前面,spawn 与 restore 都不再依赖 pane 的环境。集成测试先因此失败(恢复后的假 dv 不发送),修复后通过;真实 fnx 用例断言恢复后 fnx 进程的环境里有正确的身份。

测试:`tests/test_recovery.py` 11 个(会话重启后全部原 pane 恢复并改名、正常的不动、各分类、`--include-stopped` 仍核验 pane、其他会话不查询不恢复、排空中的 IP 跳过、failed 报出且单个失败不影响其他、`--dry-run` 不改动、队列提示、broker 持锁时拒绝、recover 持锁时 broker 起不来);`test_lifecycle` 新增"原 pane 被占用时 restore 拒绝""启动命令自带身份变量";变异检查(不按会话过滤、stopped 不核验 pane、不拿 broker 锁、restore 不核验原 pane)各自被发现。`tests/test_integration_recovery.py`(真实 herdr,真的 stop / attach 会话):假 agent 恢复后原 pane、名字正确,stopped 的不恢复,broker 运行时 recover 被拒,恢复后消息正常送达;真实 fnx 恢复后名字、pane、进程环境里的身份都正确。Mac 单元 517 通过(跳过 42);VM 全套 517 通过、0 跳过。

### 7.1 Codex 实现审核后的修正(应修 3 条)

1. **pane 丢失时的工作目录**:注册表记录新增 `cwd`(旧记录没有该字段,读作 None)。spawn 登记绝对路径的工作目录(默认家目录也登记);restore 需要新建 pane 时优先用 `--cwd`、其次用登记的,两者都没有就拒绝(不新建 pane,也不把记录改成 failed),不再退回家目录猜着启动;新建 pane 后登记新的 cwd。recover 中 pane 不在且记录没有 cwd 归为 uncertain(说明里提示用 `a2a agent restore --cwd`)。原 pane 重启不需要 cwd:实测 herdr 会话重启后 pane 的 shell 仍在原工作目录。
2. **分类出错只影响单个 agent**:`_classify` 捕获查询 herdr 的异常,记为 `classification_failed`(说明里带异常),继续处理其他 agent,`AGENTS_RECOVERED` 照写;dry-run 同样。输出新增 `notes`(uncertain / classification_failed / restore 的原因)。
3. **集成测试的竞态**:改为非阻塞试锁 `broker.lock`,确认锁真的被 broker 持有后再断言 recover 退出码 3(锁文件在前面的 recover 时已存在,不能用"文件存在"判断)。

新增测试:`test_recovery` 5 个(pane 不在时按登记目录新建 pane、旧记录无 cwd 归为 uncertain 且不新建 pane、原 pane 重启不依赖 cwd、单个分类出错其他继续且审计照写、dry-run 也报分类错误);`test_lifecycle` 5 个(spawn 登记 cwd、新 pane 用登记目录、`--cwd` 覆盖并登记、旧记录无 cwd 拒绝、原 pane 重启保留 cwd)。假 herdr 改为:pane 不在时 `pane_process_info` 报 not found(与真实一致),并记录新 pane 的 cwd。变异(recover 不看 cwd、restore 退回家目录、分类异常不按单项处理、新 pane 不用登记目录)各自被发现。集成测试加上 pane 丢失的情况:关掉 sw 的 pane 后 recover 新建 pane,两种恢复方式下 agent 进程的工作目录(/proc/<pid>/cwd)都是登记的目录。Mac 单元 527 通过(跳过 42);VM 全套 527 通过、0 跳过。
