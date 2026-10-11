# 阶段 8 方案:按 IP 动态增删(草案,待用户确认、Codex 审核)

## 1. 目标

一条命令新增一个 IP(连同它的各角色 agent),或删除一个 IP(按正确顺序清理)。现在要分别执行 `a2a topology add-ip` 和每个角色一次 `a2a agent spawn`,删除时还要自己保证先 purge 再改拓扑(`TopologyStore.remove_ip` 明确不检查是否还有 agent 在运行)。

## 2. 现状与约束

- 边只在**同一 IP 内**(Router 的同 IP 铁律),所以一个 IP 的所有消息往来都在这个 IP 自己的 agent 之间,增删一个 IP 不影响其他 IP。
- 拓扑是动态的:Router 每次发送都读当前拓扑;IP 不在拓扑里时,该 IP 身份的发送被拒(`identity`),发往该 IP 的也被拒。
- 已入队的消息不随拓扑改写(08 §7.2);目标不在了时队列头判 `TARGET_MISSING` 并暂停,等操作员裁定。
- 有些角色还没装(拓扑里 `launcher: null`,例如 spec / arch / rtl),新增 IP 时不能启动它们。
- spawn 已经是"先启动、最后登记";同一 agent 的操作有文件锁串行。

## 3. 命令

### 3.1 `a2a ip add <ip> [--roles r1,r2,...] [--cwd 目录]`

0. 全程持有 IP 操作锁;该 IP 有"排空中"标记时拒绝。
1. IP 不在拓扑里就 `add_ip`(写 `TOPOLOGY_CHANGED` 审计);已在就跳过这一步。
2. 依次为每个角色 spawn(默认是拓扑里所有**配置了启动脚本**的角色,按拓扑中的角色顺序;`--roles` 可指定子集)。
   - 已登记且 running 的跳过(可重复执行,用于失败后补齐);
   - 已登记但 stopped / closed 的不自动处理,报出来让操作员决定 `restore`;
   - 没有启动脚本的角色跳过并在结果里列出。
3. `--cwd` 支持占位符 `{ip}`、`{role}`,例如 `--cwd /work/{ip}/{role}`;目录不存在就创建。不给时沿用 spawn 的默认(家目录)。
4. 某个 spawn 失败就停下(不再启动后面的角色),**不回滚**已成功的部分和拓扑;输出每个角色的结果,修好后再执行一次即可补齐。
5. 结束写一条 `IP_ADDED` 审计(含每个角色的结果)。

### 3.2 IP 级隔离(Codex 两轮审核后修订)

两把锁,作用不同:

- **IP 操作锁** `<状态目录>/locks/ip-<ip>.op.lock`(独占):`ip add`、`ip remove`、`ip undrain` 全程持有,同一 IP 的这三种操作串行;不同 IP 互不影响。
- **IP 排空锁** `<状态目录>/locks/ip-<ip>.drain.lock`(读写锁,`flock` 的共享 / 独占):把"检查排空标记"和"随后的写入"绑成一个原子操作。
  - **Router**:持**共享锁**完成"检查标记 → `Spool.enqueue`"。标记存在就拒绝(新拒绝码 `ip_draining`,不入队、写审计)。
  - **broker**:投递引擎在写 `DISPATCHING` 前,持**共享锁**完成"检查停止条件(含该消息目标 IP 的标记)→ 写 `DISPATCHING`"(引擎新增一个按消息取锁的钩子,只包住这两步;发 prompt、等结果都在锁外)。
  - **写标记 / 撤销标记**:持**独占锁**。独占锁要等所有共享锁释放才能拿到,所以标记一旦写入:之前已经开始的入队、写 `DISPATCHING` 都已经落盘(之后的检查能看到);之后的 Router / broker 一定能看到标记。这就消除了"Router 先读到未排空、remove 检查完空队列、Router 才入队"和"broker 检查通过、remove 认为没有在途、broker 才写 DISPATCHING"两个窗口。
  - 共享锁只包住几次本地文件操作,不会长时间持有;不同 IP 的锁互不影响。
- **"排空中"标记** `<状态目录>/ip_draining/<ip>.json`(原子写入;内容含操作 ID、原因、时间、pid;持久化,崩溃后仍在)。存在时:Router 拒绝该 IP 的发送;broker 扫描跳过该 IP 的目标,已在等目标空闲的 worker 在下一次检查(等待循环每一轮、写 DISPATCHING 前、重试退避后)停下、消息保持原状态,**并且 worker 的处理循环(`process_target`)每处理一条消息前检查目标 IP 是否排空,是就退出、不原地重试**(Codex 第三轮复核:否则引擎原样返回后 worker 会立即再检查、形成忙循环);排空撤销后 broker 的下一次扫描会重新为它启动 worker;`ip add` 拒绝。已经发出的 prompt 照常收尾。
- **`a2a ip undrain <ip>`**:持操作锁(所以不会与正在进行的 `ip remove` 并发)和排空锁的独占锁,撤销标记、恢复正常,写 `IP_UNDRAINED` 审计。用于放弃一次删除。

### 3.3 `a2a ip remove <ip> [--force]`(Codex 两轮审核后修订)

全程持有 IP 操作锁。每次删除有一个操作 ID(首次写标记时生成,保存在标记里;续做时沿用)。

1. **写标记**(排空锁独占):标记不存在就写入并记 `IP_DRAIN_STARTED`(含操作 ID);已存在(上次中断)就沿用其中的操作 ID,从下面继续。
2. **等在途结束**:等该 IP 的消息里没有 `DISPATCHING`,上限为观察窗口 + 30 秒;再等该 IP 的已登记 agent 都 idle / done(或 stopped / closed),上限可配置(默认 60 秒)。`--force` 只跳过"agent 空闲"这一项。
   - **超时 → fail-closed**:不 purge、不改拓扑,**标记保留**(IP 保持隔离,不会有新消息进出),报告卡在哪条消息 / 哪个 agent,退出码非零。操作员可以稍后再执行 `ip remove`(从这里继续),或 `ip undrain` 放弃删除。
3. **检查队列**(标记已生效、在途已结束,不再有竞态):该 IP 每个角色的目标 `<role>_<ip>` 都必须 `Spool.pending(dst)` 为空,且不在 `Spool.queue_targets()` 里(包含有待投递、有未放行槽位(含已归档到 done/ 但未放行的终态消息)、队列状态文件读不出)。
   - 不满足 → **撤销标记、恢复正常**(这些消息需要操作员用 `a2a resolve` 裁定,裁定后的重试要靠 broker 投递,排空期间做不了),记 `IP_DRAIN_CANCELLED`,列出 msg_id 与需要的裁定,退出码非零。
4. **purge**:依次 purge 该 IP 的已登记 agent;某个失败就停下并报告,标记保留,再次执行从这里继续(已注销的跳过)。
5. **改拓扑**:IP 还在拓扑里就 `remove_ip`(写 `TOPOLOGY_CHANGED`);**已经不在了就跳过**(上次在这一步之后中断)。
6. **收尾**:如果审计里还没有该操作 ID 的 `IP_REMOVED`,就写入;然后撤销标记(排空锁独占)。先写审计再撤销标记:中断在两者之间时,下次执行发现已有该操作 ID 的 `IP_REMOVED`,只撤销标记,不重复记。

**每一步都可以重复执行**:中断后再执行 `ip remove`,按"标记是否存在、该 IP 的 agent 是否还登记、IP 是否还在拓扑、审计里是否已有该操作 ID 的 IP_REMOVED"判断从哪一步继续;不会出现"拓扑没有该 IP 却有它的 agent 在跑"(purge 全部完成前不改拓扑,排空期间 `ip add` 被拒),审计事件不漏记、不重复。

### 3.4 不变的部分

- spool 里该 IP 的历史(`done/`、队列状态文件)保留,不删除;同名 IP 以后再加回来时,队列序号接着往下排(沿用现有的持久单调规则)。
- 不改协议的状态迁移。Router 增加拒绝码 `ip_draining` 与"检查标记 + 入队"的共享锁;broker 扫描跳过排空中的 IP,投递引擎的停止检查改为按消息判断,并在"检查 + 写 DISPATCHING"外加按消息的共享锁钩子。复用已有的 spawn / purge / 拓扑修改。
- 插件不受影响:新 IP 的 agent 启动时读取自己的边;其他 IP 的边不变。

## 4. 测试

1. **单元测试**(假 HerdrClient,Mac / VM):
   - add:拓扑加入 IP 并按角色顺序 spawn;没有启动脚本的角色被跳过;重复执行跳过已 running 的;中途失败后停下、不回滚,补齐后再执行完成;stopped 的被报出而不是自动 restore;`--cwd` 占位符展开并创建目录;`IP_ADDED` 审计。
   - 排空标记:Router 拒绝该 IP 的发送(`ip_draining`,不入队、写审计),其他 IP 不受影响;broker 扫描跳过该 IP 的目标;已在等目标空闲的 worker 在标记写入后不发 prompt,**且 worker 线程随即退出、不忙循环**(断言线程结束,标记存在的一段时间内对 herdr 的查询次数不再增长);撤销标记后下一次扫描恢复投递;`ip add` 在排空期间被拒;`ip undrain` 恢复,且不能与进行中的 `ip remove` 并发。
   - **临界窗口**(用屏障精确卡住):① Router 已拿到共享锁、检查完标记、尚未入队时,写标记的一方必须等它入队完成,随后的队列检查能看到这条消息(于是撤销排空);② broker 已拿到共享锁、检查完、尚未写 DISPATCHING 时,写标记的一方必须等它写完,随后"等在途结束"会等这条 DISPATCHING;③ 写标记之后开始的 Router 入队被拒、broker 不写 DISPATCHING。
   - remove 的队列检查:非终态待投递消息、已归档到 done/ 但槽位未放行的终态消息、队列状态文件读不出,三种情况都拒绝,撤销标记、什么都不改。
   - remove 的超时:DISPATCHING 等待超时、agent 空闲等待超时都 fail-closed(不 purge、标记保留、退出码非零);`--force` 跳过空闲等待但不跳过 DISPATCHING。
   - remove 的崩溃恢复(用注入的失败点模拟):在 purge 中途、`remove_ip` 之后、写 `IP_REMOVED` 之后撤销标记之前中断,再次执行都能完成;拓扑里已没有该 IP 时不报错;`IP_REMOVED` 恰好一条。
   - 并发:两个进程同时对同一 IP 执行增删时串行。
2. **真实 herdr 集成测试**(VM,假 agent,不调用模型):
   - add 一个新 IP 的全部角色 → 在新 IP 内发一条消息,broker 送达;
   - remove:有待投递消息时被拒(并恢复正常);处理掉后删除成功 → pane 关闭、注册表清空、拓扑没有该 IP,用该 IP 身份发送被 Router 拒绝;
   - 同名 IP 再 add 回来能正常工作,队列序号接着排;
   - 其他 IP 的 agent 和消息不受影响。
3. **真实 fnx**(VM,不发提示词、不调用模型):`ip add` 启动 fnx_dv / fnx_sw,`ip remove` 清理干净(没有残留进程,fnx 只为临时目录建了空会话目录)。

## 5. 不在范围内

- 增删角色(5 个角色是固定的);
- 注册表自动恢复(VM / herdr 重启后批量拉起):下一步单独做,会复用这里的批量逻辑;
- 并行批量 spawn(用户很少整体重启,暂不做)。

## 6. 实现后 Codex 审核的修改(阻断 2、应修 2)

- 等 agent 空闲:只有 herdr 明确返回 idle / done 才算空闲;查不到状态(未识别、暂时查不到、进程已退出)按忙处理,超时 fail-closed;进程确已退出时用 `--force`。
- `a2a agent spawn` / `restore` 也受 IP 排空约束:持 IP 排空锁的共享锁并检查标记,排空中拒绝;写标记的一方要等进行中的启动结束,随后 `ip remove` 的 purge 会包括这个新 agent。
- broker 排空期间只"不开始新投递":已到终态的队列头照常处理(DELIVERED 自动放行、确定失败暂停),否则排空期间完成的投递不会放行槽位,`ip remove` 会误判队列未清空。
- `ip add` 发现注册表 running 但 herdr 里没有该 agent 时报 `registered_running_but_missing`(需要 `a2a agent restore`),不当作已运行。

## 7. Codex 复核实现后的第二轮修改(阻断 2、应修 1)

- `spawn` / `restore` 先拿 IP 门禁(排空锁共享锁 + 检查标记),**之后**才读拓扑、解析身份与启动脚本:避免调用方先读到 IP 存在、门禁前 `ip remove` 已完成,再用过期拓扑拉起 agent。测试用持独占锁的线程把 spawn 卡在门禁上,期间完成删除,放开后 spawn 因"不在拓扑里"被拒、没有登记。
- 排空标记检查区分"明确不存在"与"检查失败":`is_draining` 只有 `FileNotFoundError` 才返回 False,其他错误(权限、路径)按正在排空处理;`read_marker` 同样把读取失败记为 unreadable。测试把 `ip_draining/` 设为无权限后,spawn 被拒。
- `ip add` 对 running 记录按**登记的名字**查 agent,并核对它在登记的 pane 上;找不到报 `registered_running_but_missing`,在别的 pane 报 `registered_running_but_mismatched`(需要人工确认)。

## 8. Codex 第三轮复核后的修改(阻断 1、应修 1)

- `ip remove` 在等在途投递之后、等 agent 空闲之前**核对身份**:每个 running 记录,pane 上若有 agent,必须就是登记名字的那个,且该名字就在这个 pane 上;agent 已退出、pane 上没有 agent 时放行(purge 只关 shell)。对不上就拒绝删除,**排空标记保留**、列出不符的记录,等人工确认后再执行或 `ip undrain` 放弃。`--force` 只跳过"等 agent 空闲",**不跳过身份核验**。
- 按名字查 agent 时只有 `HerdrNotFound` 算不存在,herdr 服务不可用等其他错误照常上报(`ip add` 不再把它们误报为 `registered_running_but_missing`)。

## 9. Codex 第四轮复核后的修改(阻断 1)

purge 会关闭每个非 closed 记录的**整个 pane**,所以删除前对这些 pane 都用 `pane process-info` 检查前台:
- pane 已不在,或前台只有 shell(前台进程组就是 shell,或没有前台进程)→ 放行;
- 前台有进程时,只有"记录是 running、按登记名字找到的 agent 就在这个 pane 上、pane 上的 agent 也是这个名字"才放行;
- 其他情况(stopped 记录的 pane 被别的进程占用、herdr 认不出的前台进程、换成了别的 agent)都拒绝删除,排空标记保留,等人工确认。`--force` 不跳过。

## 10. Codex 第五轮复核后的修改(阻断 1)

"前台只有 shell"改为严格判定(`_shell_only`):字段齐全且类型正确(前台进程组 ID、shell_pid 为整数,前台进程列表非空且每项有整数 pid),并且 **进程组 ID == shell_pid 且列表里只有 shell 自己(pid == shell_pid)** 才算只剩 shell。依据:herdr 0.9.3 实测,只剩 shell 时就是这样;前台跑 `sleep` 时进程组 ID 是 sleep 的 pid、列表里只有 sleep。字段缺失、类型不对、或彼此矛盾(如进程组是 shell 但列表里有别的进程)都无法确认,拒绝删除、标记保留。
