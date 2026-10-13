# 路线图遗留项处理方案(Codex 审核第 1 轮后修订:阻断 2、应修 3;第 2 轮后修订:应修 4;第 3 轮后修订:应修 3)

2026-10-11。处理路线图里没有外部阻碍、此前被推后的 6 条(05 号第 189、293、313、327、336、338 行)。探测 5、`HERDR_WRITE` 两条已在文档中处理,不在本方案内。

| 节 | 路线图条目 | 改什么 | 是否调用模型 |
| --- | --- | --- | --- |
| A | 293 stop 的进程替换窗口 | `lifecycle._terminate` | 否 |
| B | 313 插件每个 agent 一份副本 | 新命令 `a2a plugin install / uninstall / status`(软链接) | 否 |
| C | 338 插件加载时缓存边 | 插件每轮开始前重新读边 | 否 |
| D | 336 发送防重复(幂等) | 插件生成运行 ID,Router 按键去重 | 否 |
| E | 189 `done/` 保留与磁盘告警 | 新命令 `a2a spool archive`、`a2a status` 显示用量 | 否 |
| F | 327 发送后多出的总结 + 探测 8 | 模板加约束;一次真实 fnx 测试 | **是,需用户同意** |

建议顺序:A → B → C → D → E → F。A、B、C 相互独立且小;D 改插件与 Router 的接口,放在 C 之后避免同时改插件同一处;E 只是新命令;F 最后一次性调用模型。

---

## A. stop 的进程替换窗口(293)

**现状**:`_terminate` 先按名字核对 agent 在登记的 pane 上、读 `pane process-info` 拿前台进程组 pgid、核对 argv0 是 `pi`,然后 `killpg(pgid, SIGTERM)`。核对与发信号之间,如果 pi 恰好退出、pane 里又起了别的前台进程组且复用了同一个 pgid,信号会发给别人。窗口极短,但存在。

**改法**(仅 Linux,VM 上运行的就是这条路径;Mac 没有 /proc,保持现状):
1. 读 process-info 时同时记下进程组组长(pid == pgid 的那个进程)的**启动时间**(`/proc/<pgid>/stat` 第 22 个字段)和 argv0。组长不在前台进程列表里 → 拒绝(和现在"前台进程组不明确"一样处理)。
2. 用 `os.pidfd_open(pgid)` 打开组长的 pidfd,**再读一次** `/proc/<pgid>/stat` 核对启动时间与快照一致(pidfd 打开后,即使组长退出,它也不会被复用为别的进程的 pidfd)。
3. 先用 `signal.pidfd_send_signal(pidfd, SIGTERM)` 给组长发信号(这一步对"同一个进程"是无竞态的)。**它失败(如 ESRCH:组长已退出)就不再按旧 pgid 调 `killpg`**(Codex 审核:否则可能命中复用了该 pgid 的新进程组)。成功后再 `killpg(pgid, SIGTERM)` 覆盖组内其他进程(pi 启动的 bash 工具子进程)。
   - **"已停止"的判定改严**(Codex 第 2 轮):不再只看"herdr 已看不到 agent"。组长退出后它启动的子进程可能还活着(同一进程组),herdr 看不到 agent 也不代表进程组都没了。stop 只有在 herdr 看不到 agent **且** pane 明确只剩 shell(`pane_shell_only` 为 True)时才登记 stopped;否则(含 pidfd 返回 ESRCH 后组里仍有进程、进程信息不完整)抛 `LifecycleError`,记录保持 running,提示人工处理。这条对正常路径同样生效。
4. **剩余限制**(不声称彻底消除):pidfd 发信号成功与 `killpg` 之间,组长退出、被回收、整个进程组变空且 pgid 被新进程组复用,`killpg` 仍会命中新组。窗口极窄,写进 README 的已知局限。
5. Python 3.9(Mac)没有 `pidfd_open` 时保持现状;VM 是 Python 3.10,内核支持 pidfd(5.3+)。

**测试**:单元测试用可注入的"读启动时间 / pidfd"函数模拟:快照后启动时间变了 → 拒绝发信号;组长不在前台列表 → 拒绝;正常 → 先 pidfd 发信号再 killpg;**pidfd 发信号失败(ESRCH)→ 不调 killpg**;**组长退出、子进程仍在(pane 前台不是只剩 shell)→ stop 报失败、记录保持 running**;herdr 看不到 agent 但进程信息不完整 → 同样报失败。VM 集成加一例:假 agent 启动一个同进程组、忽略 SIGTERM 的子进程,组长退出后 stop 报失败、状态不变。VM 集成:真实假 agent 的 stop 仍通过;用一个短命进程模拟"核对后组长被替换"(启动时间不一致)→ 拒绝。

### A 的实现(2026-10-11)

- `src/a2a/procsignal.py`:`ProcSignaler`(快照 /proc 的启动时间、进程组、argv0;pidfd 打开与发信号;`group_members` 扫描进程组里活着的进程,排除僵尸)。没有 pidfd 时 `supported=False`。
- `lifecycle._terminate`:组长必须在前台进程列表里且 argv0 为 pi;Linux 上 快照 → pidfd_open → 再快照核对 → pidfd 发信号(ESRCH 即不再 killpg)→ killpg。注入了假 kill 的单元测试默认不用 pidfd。
- **实现中的新发现**:VM 集成测试显示,组长退出后,pane 的 shell 认为前台任务已结束并**收回终端**,残留的子进程变成后台进程,pane 看起来"只剩 shell"。所以只看 pane 前台判断不出子进程还在。改为:Linux 上还要求**原进程组里已没有活着的进程**(扫描 /proc)。
- 由此还发现 `stop()` 原有的旁路:herdr 已看不到 agent 时直接登记 stopped,不做任何核验。改为同样核验(pane 只剩 shell、原进程组为空);为此 spawn / restore 在注册表登记 agent 的进程组 `pgid`(新字段,旧记录为 None;按 Codex 实现审核,Linux 上旧记录 agent 已不在时不登记 stopped,见下)。
- 测试:单元 `StopVerified` 8 个(pidfd 正常顺序、核对期间被替换、快照不是 pi 组长、组长不在前台列表、三种"组长先退出"不 killpg 且子进程在时不登记、组长退出且干净时登记、残留子进程在后台时不登记、登记 pgid)、`test_procsignal` 5 个真实进程用例(VM)。变异 6 处各自被发现。VM 集成:启动脚本留下一个忽略 SIGTERM 的同组子进程 → stop 失败(退出码 6)、状态保持 running;再 stop 仍拒绝;人工结束残留进程后 stop 成功。Mac 单元 543 通过(跳过 48);VM 全套 543 通过、0 跳过。
- **Codex 实现审核(阻断 1、应修 2)已修**:① 旧记录没有 pgid 时,Linux 上 agent 已不在的 stop 无法核对进程组 → 不登记 stopped,提示人工确认后 close 或 restore(restore 会登记新的 pgid);② 区分"进程已不在"与"无法检查":读 /proc 出现除"不存在"以外的错误、或 /proc 以 hidepid 挂载(读 `/proc/self/mountinfo` 判断)时抛 `ProcInspectError`,stop 一律 fail-closed(不发信号 / 不登记 stopped);③ 启动后取不到前台进程组(或等于 shell)时 spawn / restore 不算成功(spawn 关 pane 不登记,restore 记为 failed;复核后 restore 也关 pane,见下),不会留下旧的 pgid。新增单元 7 个(含假的 /proc 目录模拟权限错误、hidepid、僵尸),4 处变异各自被发现。Mac 单元 550 通过(跳过 48);VM 全套 550 通过、0 跳过(VM 的 /proc 未用 hidepid)。
- **Codex 复核(应修 1)已修**:在原 pane 里 restore 时,启动命令发出后的任何失败(未进入可投递状态、改名后取不到进程组等)原先不关 pane、只把记录标 failed,会留下一个记录不是 running、stop 也管不了的 agent。改为原 pane 同样关闭(pane 里的 agent 随之结束),记录标 failed,之后 restore 用登记的工作目录新建 pane;关 pane 也失败时报错写明 agent 可能仍在运行、请人工确认后 `a2a agent close`。新增单元 2 个,变异(恢复旧行为)被 3 个测试发现。Mac 单元 552 通过(跳过 48);VM 全套 552 通过(真实 fnx 的 6 个另带 `A2A_INTEGRATION_FNX=1` 补跑,通过),0 未跑。
- **Codex 第 2 次复核(应修 1)已修**:失败分支原先先写注册表再关 pane,注册表写失败(磁盘、权限)会跳过关 pane。改为先关 pane、再写记录,两步各自尝试、互不跳过;任一步失败时报错分别写明 pane 是否已关闭、注册表是否已标 failed(未标时写出仍是的旧状态)。新增单元 1 个(注册表写失败仍关 pane,之后 restore 新建 pane),变异(恢复旧顺序)被发现。Mac 单元 553 通过(跳过 48);VM 全套(带 `A2A_INTEGRATION_FNX=1`)553 通过、0 跳过。
- **Codex 第 3 次复核(应修 2)已修**:① spawn / restore 的启动失败清理改为捕获 `BaseException`,Ctrl-C(KeyboardInterrupt)中断启动时也先关 pane、写 failed(restore)再抛出;清理有失败时同样分别报告。新增单元 1 个(同一用例覆盖 spawn 与 restore 的中断),两处分别变异(改回只捕获 `Exception`)都被发现;测试替身 `FakeHerdr.pane_close` 同时清除该 pane 的进程信息(原先遗留,会让后续 stop 找错 pane)。Mac 单元 554 通过(跳过 48);VM 全套(带 `A2A_INTEGRATION_FNX=1`)554 通过、0 跳过。② 本节第 37 行旧记录"只核验 pane"的说法与实现不符,已改为指向 fail-closed 的说明。
- **真实环境复现(用户要求核实审核问题是否真实存在)**:在 VM 真实 herdr 上用修复前代码(HEAD 28b38dc)复现了三种情况——原 pane restore 超时后 pi 仍在运行而记录为 failed、stop 拒绝处理;spawn 等就绪时 Ctrl-C 留下未登记的 pane 与 pi;restore 等就绪时 Ctrl-C 后 pi 已在原 pane 运行而记录仍是 stopped。修复后三种情况都不再出现。其余几条审核问题(旧记录、权限错误、取不到 pgid、注册表写失败)只在代码层面确认,真实环境中需要多个条件同时满足,未复现。三种情况已收为 VM 集成测试(`test_integration_lifecycle.py`,假 agent 新增 `stuck` 模式并写出自己的 pid;测试结束时清理自己留下的 pane 与进程);改回修复前写法后 5 轮 15 次全部失败。
- **测试环境问题(实测)**:OrbStack VM 读取 Mac 共享目录时,文件内容是即时的,但 stat 得到的大小与修改时间可能滞后数秒到 29 秒以上;Python 只按这两项判断 `__pycache__` 是否可用,于是会加载修改前的字节码。变异检查曾因此"漏抓"两次(实际跑的是修复后的代码)。属性滞后还会让 VM 按旧的大小读文件,把变长的源码读成截断的(实测出现过 `SyntaxError: '(' was never closed`,出错行并未修改)。此后 VM 测试一律先等 VM 看到的各 .py/.ts 文件大小与 Mac 一致(通常 0–1 秒),再删 `__pycache__` 并设 `PYTHONDONTWRITEBYTECODE=1`(交替修改 6 次均加载到最新版本);此前"刚在 Mac 改完就在 VM 跑"的结果可能部分跑的是旧代码,以本次重跑为准:VM 全套(含真实 fnx)557 通过、0 跳过,两次。Mac 单元 557 中有一次 1 个失败(与 VM 全套并发时,未保存完整输出,无法确定是哪个测试),此后单独 4 遍、与 VM 并发 8 遍、并发删除 `__pycache__` 3 遍均通过,未能复现,记为待观察的偶发失败。
- **Codex 集成测试审核(应修 2)已修**:① 残留子进程测试取得 pgid 后立即登记清理(按 pid + 启动时间 + argv0 核对身份后再 SIGKILL,避免 pgid / pid 被复用时误杀);② `interrupt()` 启动的 a2a 命令登记清理(未回收时 kill 并回收)。注入失败验证:三处提前失败后 VM 上无残留。对照(去掉这两个钩子再注入失败)同样无残留——模块结束时会停止并删除专用 herdr 会话,pane 内进程随之结束;钩子的作用是让单个测试失败不把进程留到模块结束、干扰后面的测试。Mac 单元 557 通过(跳过 51);VM 全套(先等文件大小一致、无字节码缓存、含真实 fnx)557 通过、0 跳过、无残留进程。
- **Codex 复核(应修 1)已修**:`interrupt()` 的 a2a 命令改在独立进程组启动(`start_new_session=True`),清理时 `killpg` 整组并回收(未回收前 pid = 组号不会被复用),连同它启动的 `herdr agent wait` 一起结束。注入失败对照:旧写法(只 kill a2a 进程)每个测试留下 1 个 `herdr agent wait`,而且测试模块结束、专用会话删除后仍会多活几秒(更正上一条"模块结束即全部结束"的说法:会话删除只结束 pane 内进程);新写法为 0。将来考虑:`reap_at_end()` 快照与 kill 之间的 pid 复用窗口可用 pidfd 消除(未做)。Mac 单元 557 通过(跳过 51);VM 全套 557 通过、0 跳过;结束 2 秒后有 2 个 `herdr agent wait` 短暂存在,随即退出,来源未能确认(可能是测试收尾或 Codex 自己的 VM 会话)。

## B. 插件改为软链接安装(313)

**现状**:没有安装命令;测试和压测脚本各自把 `pi-extension/a2a.ts` **复制**到 `~/.forenyx/fnx_<x>/agent/extensions/`,结束时删除。正式使用时每个智能体一份副本,修插件后容易漏更新。

**改法**:新命令(插件是框架的一部分,由 a2a 管理安装):

```bash
a2a plugin install   --agent-dir ~/.forenyx/fnx_dv     # extensions/a2a.ts -> 仓库里的 pi-extension/a2a.ts(软链接)
a2a plugin status    --agent-dir ~/.forenyx/fnx_dv     # 未安装 / 已链接到本仓库 / 链接到别处 / 是普通文件
a2a plugin uninstall --agent-dir ~/.forenyx/fnx_dv     # 只删除指向本仓库的软链接
```

- `extensions/a2a.ts` 已存在且**不是**指向本仓库的软链接(普通文件、指向别处)→ 拒绝,不覆盖(可能是别人的同名插件,对应阶段 7 发现的同名冲突)。
- `extensions/` 目录不存在就创建;uninstall 后目录为空且是本命令创建的才删除(用一个标记文件记录)。
- 只往 fnx 的 agent 目录加外部文件,不改 fnx 的程序、启动脚本、libexec(沿用约束)。
- 测试与压测脚本改用这个命令(结束时 uninstall),不再复制。

**需要实测**(不调用模型):fnx(pi 0.79.10)是否加载软链接形式的扩展。用真实 fnx 启动后看启动画面列出的扩展,并确认工具 `a2a_send` 已注册(与现有集成测试同样的检查)。若不支持软链接,改为"复制 + 记录校验值,status 检查是否与仓库一致",方案再审。

另外 3 个智能体(313 行的后半句)归入 75 行,暂不处理。

**实现说明(2026-10-12)**:
- 实测(VM,真实 fnx_dv / fnx_sw,不调用模型):`agent/extensions/a2a.ts` 为指向仓库插件的软链接时,fnx 正常加载,`a2a_send` 注册、各角色可用的边正确;启动画面 `[Extensions]` 显示链接名 `a2a.ts`。按原方案实现,不需要退回"复制 + 校验值"。
- `src/a2a/plugin.py` + `a2a plugin install/status/uninstall --agent-dir`(错误退出码 6)。`--agent-dir` 下必须已有 `agent/`(fnx 运行过);`extensions/` 不存在时创建,并在 `agent/.a2a-created-extensions` 记录(放在 extensions 之外,免得被当成扩展加载),uninstall 只在有该标记且目录已空时删除目录。
- 同名冲突:除了 `a2a.ts` 本身,还检查 extensions/ 里其他 .ts/.js 是否注册 `a2a_send`(按 `name: "a2a_send"` 文本匹配,尽力而为;只读取该工具的扩展,如测试用的检查扩展,不算冲突)。这一条原本在压测脚本里,移进安装命令。
- 集成测试 `RealFnxPlugin` 与压测脚本 `real_fnx_scale.py` 改用此命令;原本已安装(`changed=false`)的保持不动,结束时也不卸载。压测脚本核对启动画面扩展列表时改为只认 `a2a.ts`。
- 测试:新增单元 10 个(`test_plugin.py`,临时目录),三处变异(不核对链接指向、不看标记文件、改回文字匹配)各自被发现。Mac 单元 567 通过(跳过 51);VM 全套(先等文件大小一致、无字节码缓存、含真实 fnx)567 通过、0 跳过,结束后 fnx 目录无残留。压测脚本需要调用模型,未运行,只确认能加载(`test_stress_helpers`)。
- **Codex 审核(应修 3)已修**:① 标记文件改为记录 extensions/ 的 设备号:inode,卸载时核对;目录被外部删除重建后不再当成本命令创建的,作废标记被清掉;② `a2a plugin` 捕获 OSError(权限不足、只读目录等),按约定输出 JSON、退出码 6;③ 集成测试先按 `changed` 登记卸载再断言。新增单元 2 个,两处变异各自被发现。Mac 单元 569 通过(跳过 51);VM 全套(含真实 fnx)569 通过、0 跳过,fnx 目录无残留。按用户要求每条只审一次,不再复核。

## C. 插件每轮刷新可用的边(338)

**现状**:插件加载时调用一次 `a2a edges`,用结果注册 `a2a_send`(参数 `edge_id` 的 enum 与工具说明)。拓扑之后新增的边要重启 agent 才看得到;删除的边仍出现在 enum 里,调用时被 Router 拒绝。

**改法**:依据 pi 文档(`docs/extensions.md`:`pi.registerTool()` 在启动后调用也立即生效,同一会话内刷新;`pi.setActiveTools()` 可启停工具),在 `before_agent_start`(每次收到新输入、开始处理之前)重新调用 `a2a edges`:
- 边与上次不同 → 用同名重新 `registerTool`(新的 enum 与说明);
- 变为没有出边 → 停用 `a2a_send`;从无到有 → 注册并启用。**只增删 `a2a_send`**:先 `getActiveTools()` 取当前启用的工具,去掉或加上 `a2a_send` 后再 `setActiveTools()`,其他工具(bash 等)保持原样(Codex 审核);
- `a2a edges` 调用失败 → 保留上一次的工具,不影响这轮处理(Router 仍会完整鉴权,删除的边照样被拒)。
- 加载时仍注册一次(保证第一轮就有工具)。

**需要实测**(不调用模型):同名 `registerTool` 是替换还是报错 / 重复。不需要模型:用一个只在测试时安装的检查扩展,在命令处理函数里(用户输入 `/命令`,不触发模型)对比重新注册前后的 `pi.getAllTools()`,把结果写进文件。若同名重新注册不是替换,改为工具名带版本号、用 `setActiveTools` 只启用最新的那个,方案再审。

**测试**:插件单元(Mac / VM node):边变化时重新注册、无变化不重注册、出边变空时停用、`a2a edges` 失败时保留旧工具;**启停 `a2a_send` 时其他启用的工具(含 bash 等)不变**。真实 fnx(不调用模型):运行中 `a2a topology` 增加一条边,触发 `before_agent_start` 需要输入 —— 这一步会调用模型,所以真实验证并入 F 的那次模型测试;不调用模型的部分只验证"同名重新注册"的 API 行为。

**实现说明(2026-10-12)**:
- 没有另做"同名重新注册"的实测,改为直接读 fnx 自带的 pi 0.79.10 源码(编译进 `libexec/forenyx-cli`,只读):`registerTool` 把工具按名字存进 Map(同名 = 替换)并立即 `_refreshToolRegistry`;刷新时已存在的名字保持原启用状态,只有新名字自动启用(所以停用后再恢复必须显式 `setActiveTools`);`emitBeforeAgentStart` 在 `_runAgentPrompt` 之前执行,其中改的工具列表与重建的系统提示本轮生效;`getActiveTools` / `setActiveTools` 在扩展加载期间抛 not initialized,只能在事件处理函数里用;没有注销工具的接口。
- 插件:`makeSendTool(pi, cfg)` 的 `load()`(加载时有出边就注册;读取失败或没有出边先不注册,下一轮再试——原来加载失败就永远没有工具)与 `refresh()`(before_agent_start:出边与上次相同不重注册;不同就同名重注册;变空只停用;失败沿用;启停只增删 `a2a_send`)。工具定义抽成 `buildSendTool(cfg, edges)`。
- 测试:插件单元测试的假 pi 改为按上述源码行为模拟(同名替换、新名字自动启用、加载期间调用启停接口会抛错);新增 5 个(无变化不重注册且其他工具不变、加边下一轮可见、出边变空只停用 / 恢复后重新启用、读取失败保留旧工具、加载时没有出边或读取失败后续补注册);4 处变异各自被发现。另:插件测试的假 herdr 先空跑一次——macOS 首次执行新建的可执行文件会做安全扫描,可能超过插件 1 秒的上报超时导致重试(推测是之前 Mac 上偶发失败的原因,未证实)。
- Codex 审核:阻断 0、应修 0。源码核查只对 pi 0.79.10 成立,**升级 fnx / pi 后要重新核对**同名注册与启用状态的行为。将来考虑:每轮刷新用同步 `execFileSync` 调 `a2a edges`,最坏等 30 秒才开始本轮(低概率,不影响正确性)。
- 真实 fnx 的"运行中加边、下一轮可见"需要输入才会触发 before_agent_start,会调用模型,并入 327 那次模型测试。本次 VM 上跑了插件单元测试与真实 fnx 加载插件的集成测试(fnx 正常加载改动后的插件,`a2a_send` 与边正确)。

## D. 发送防重复(幂等)(336)

**问题**:模型可能对同一次通知调用两次 `a2a_send`(自己重试、或在"发送结果未知"后忍不住再发)。现在靠工具说明约束("每次通知只调用一次"),没有机制保证。toolCallId 不能用作幂等键:模型重试是一次新的工具调用,ID 不同。

**业务上的"同一次通知"**:一个 agent 处理**同一条输入**(人工 prompt,或 broker 投递来的一条消息)期间,沿**同一条边**只应发一次。处理新输入时再发同一条边,是新的通知。

**改法**:
1. 插件在 `before_agent_start` 生成一个随机的**运行 ID**(UUID),这次处理期间不变。**关键假设**:pi 的自动重试(429 等)发生在同一次处理之内,不会重新触发 `before_agent_start`,所以重试仍是同一个运行 ID。实现前先做**确定性验证**(Codex 审核,不调用真实模型):在 VM 上起一个本地假的 OpenAI 兼容服务(第一次返回 429,之后返回调用 `a2a_send` 的工具调用),fnx 用临时的 agent 目录(`FORENYX_CODING_AGENT_DIR` 指向临时目录,只放测试用的模型配置与检查扩展,不碰用户的配置)指向它;检查扩展记录每次 `before_agent_start` / `agent_start` / `agent_end` 与运行 ID。**必须证明发生的是 pi 层的重试**(Codex 第 3 轮:429 可能被模型服务的 SDK 在内部重试并成功,那样只看到一次 `before_agent_start` 证明不了什么):断言事件序列里确实出现 `agent_end`(带错误)之后又有新的 `agent_start`(pi 的自动重试),同时 `before_agent_start` 只出现一次、运行 ID 不变;假服务记录收到的请求次数,与 pi 层重试次数对得上。若 SDK 内部重试吞掉了 429(看不到 pi 层重试),调整假服务(例如连续多次 429,或返回 SDK 不重试的可重试错误),直到能观察到 pi 层重试为止;做不到就如实记录"未能验证",不据此实现 D。若 fnx 不支持自定义模型服务,退回读 pi 源码中重试路径作为依据并在方案中注明证据较弱;若假设不成立,改为在 `agent_end` 判断是否会重试来延续运行 ID,方案再审。
2. `a2a_send` 调用 `a2a send` 时带 `--idempotency-key <运行 ID>`。
3. Router 以 (发送方 agent_id, edge_id, 键) 去重。**幂等记录与入队不是一次原子写入,按"先写意图、再补齐"做成可恢复的**(Codex 审核阻断 1):
   - 持发送方的幂等锁 `<状态目录>/idempotency/<发送方>.lock`(防同键并发),依次:
   - ① 查幂等记录 `<状态目录>/idempotency/<发送方>/<sha256(edge_id, 键)>.json`。**没有** → 按当前拓扑完成鉴权,生成新的 `msg_id`,原子写入幂等记录 `{状态: intent, 键, 完整的已鉴权消息快照(msg_id、src、dst、edge_id、text、session、project_id、ip_id、topology_revision), 创建时间}`(Codex 第 2 轮:冻结消息内容,入队之前就固定了 msg_id 与内容);
   - ② 记录为 intent:按记录里的 `msg_id` 查 spool(`Spool.enqueue` 已在 spool 锁内拒绝重复 msg_id,入队本身是一次原子写文件)。**不在**(①② 之间崩溃后的补齐)→ **按当前拓扑重新鉴权**:被拒(目标已删除、IP 排空等)→ 照常拒绝,记录保持 intent;鉴权结果的 dst / text 与快照**不一致**(期间模板或目标变了)→ 拒绝补齐,拒绝码 `idempotency_conflict`,写审计,交操作员处理(不按新内容发,也不按旧内容发);一致 → **用快照**入队。**已在** → 不入队。之后把记录原子改为 `{状态: enqueued}`;
   - 记录为 enqueued → 重复调用,不入队(不再查 spool:消息可能已被归档);
   - ③ 审计:按 msg_id 检查是否已有该消息的入队审计,没有就补记(覆盖"入队后、写审计前崩溃");重复调用另记一条 `SEND_DEDUPLICATED`。
   - 返回:首次与补齐时是正常回执;重复时返回原 `msg_id` 与 `"duplicate": true`(退出码 0,回执格式不变,插件告诉模型"这条通知已经发过,不要再发")。
   - 各崩溃点:① 写记录前 → 重试当作首次;①② 之间 → 重试按快照补齐入队(内容有变则拒绝补齐),msg_id 不变;入队后、改 enqueued 前 → 重试在 spool 里找到该 msg_id,不入队,补改 enqueued;② 之后、③ 之前 → 重试判为重复并补记审计。都不会出现两条消息,也不会出现同一 msg_id 不同内容。
4. 没带键的调用(人工命令行 `a2a send`)行为不变。
5. **与"发送结果未知"三分类的关系**:插件现在在超时、崩溃、回执看不懂、入队后审计失败时报"结果未知",要求模型不要再调用。有了幂等后,同一次处理里再调用是安全的(只会补齐或判重)。本方案**不改**这部分提示(保守),只在工具说明里不再把"重复调用会重复投递"作为理由;是否放开留作将来考虑。
6. **键的有效期与过期行为**(Codex 第 2 轮:不能因消息到终态或被归档就失去判重依据):
   - 插件生成的键带时间:`<运行开始的 UTC 秒>-<UUID>`。Router 拒绝格式不对的键,拒绝**超过有效期**(默认 7 天,远大于一次处理的时长)的键(拒绝码 `idempotency_expired`,不入队)。
   - 幂等记录本身就是判重的"墓碑":只有键已过有效期的记录才可以被清理;清理之后同一个键再来,会因过期被拒,不会被当作首次发送。
   - 归档(E)不读也不删幂等记录;enqueued 状态的判重不依赖 spool 里还有没有那条消息。为了 ②"补齐"时能可靠判断 msg_id 是否已入队,`a2a spool archive` 的 `--older-than` 不得小于键有效期(否则拒绝执行)。

**取舍(请确认)**:同一次处理里**有意**对同一条边发两次会被去重。按目前的模板(每条边是一句固定通知),这是期望行为。

**测试**:Router 单元:同键第二次不入队、返回原 msg_id、审计一条;不同键、不同边、不同发送方各自入队;并发两个进程同键只入队一次;无键不变;**四个崩溃点**(用可注入的检查点在 ① 前、①② 之间、入队后改 enqueued 前、②③ 之间抛出)之后重试:只有一条消息、msg_id 不变、入队审计恰好一条;①② 之间崩溃后模板被改 → 拒绝补齐(`idempotency_conflict`)、不入队;目标被删 → 照常拒绝;消息被归档后同键重试仍判为重复;过期的键被拒;`archive --older-than` 小于键有效期被拒。插件单元:同一运行 ID 内两次调用带相同键、`before_agent_start` 后换新键、回执 `duplicate` 的提示文字。VM 集成(假 agent):同一次处理里发两次只投递一次。

**实现说明(2026-10-12)**:
- **关键假设的依据**:没有做假模型服务的实测,改为读 fnx 自带 pi 0.79.10 源码(只读):`prompt()` 先 `emitBeforeAgentStart` 再 `_runAgentPrompt`;后者 `agent.prompt()` 之后 `while (_handlePostAgentRun()) agent.continue()`;可重试错误(`_isRetryableError`:429、5xx、超时、连接错误等)由 `_prepareRetry` 退避后返回 true → `agent.continue()`,**不经过 `prompt()`,不再触发 before_agent_start**。所以自动重试仍是同一个键。
- **已知限制**(源码同一处):压缩后续跑(`_checkCompaction`)与 agent 忙时排队的输入(`hasQueuedMessages`)也走 `agent.continue()`。前者是同一次处理,符合预期;后者是人工在 agent 忙时插话,第二条输入与第一条共用一个键,同一条边上的第二次通知会被判重。broker 只在目标空闲时投递,不会造成这种情况。
- **Router**(`router.py`):`send(edge, env, idempotency_key=...)`。身份核实后按发送方加锁 `<状态目录>/idempotency/<发送方>.lock`,记录 `<状态目录>/idempotency/<发送方>/<sha256(边 \0 键)>.json`;流程按方案 3 实现(intent 快照含 msg_id 与已鉴权内容 → 查 spool → 补齐时重新鉴权、与快照不一致 `idempotency_conflict` → enqueued;重复时补记缺失的入队审计并记 `SEND_DEDUPLICATED`,不查 spool 是否还在)。第一次发送刚写下 intent 时不重复鉴权。键格式 `<10 位 UTC 秒>-<UUID>`,超过 7 天 `idempotency_expired`,超前 5 分钟以上 `bad_idempotency_key`。鉴权拆成 `_authorize()`,有键无键共用。新拒绝码与审计事件已写入 `08-protocol.md`。
- **CLI**:`a2a send <边> --idempotency-key K`,带键时回执多一个 `duplicate` 字段(`state` 仍为 `QUEUED`,插件照常识别)。
- **插件**:加载时生成一个键,每次 before_agent_start 换新;`a2a_send` 带键调用;回执 `duplicate: true` 时告诉模型"这条通知在本次处理中已经发过,没有重复发送,不要再调用"。"发送结果未知"的提示不改(方案 5)。
- **测试**:`test_idempotency.py` 12 个(同键一次、不同键/边/发送方各自入队、无键不变、坏键/过期/未来、第一次被拒后可重试、离开 spool 后仍判重、四个崩溃点、入队后写审计前崩溃、intent 后模板变了 → conflict、intent 后目标关闭 → 照常拒绝且恢复后补齐、8 线程与 6 进程同键并发);插件 3 个(同一次处理里第二次判重、新的一次处理再发、首次处理前用加载时的键)。Router 4 处、插件 2 处变异各自被发现。没有另做方案里的 VM 假 agent 集成测试:同键只入队一次已由多进程命令行测试覆盖,broker 对一条已入队消息只投递一次由原有测试覆盖。
- `a2a spool archive --older-than` 不得小于键有效期:归档命令属于 189,在那里实现。

## E. `done/` 与审计日志的保留(189)

**现状**:每条消息到终态后在 `spool/done/<msg_id>.json` 留一个文件,永不删除;`audit.jsonl` 一直追加。150 个 agent 压测约 600 条消息,单条约 1 KB,增长不快,但长期运行会一直累积。

**原则**:不做无策略删除;先归档、可查;只在用户设定的条件下动。

**改法**:
1. `a2a status` 增加一节"存储":`done/` 文件数与大小、`spool/archive/` 大小、`audit.jsonl` 与 `audit.d/` 全部分段的大小、幂等记录数与大小、整个状态目录合计,以及所在磁盘的剩余空间(Codex 第 3 轮:归档和轮转后的文件也要计入,否则会低估占用);超过阈值时给出提示(只提示,不自动处理)。
2. 新命令 `a2a spool archive --older-than <天数> [--dry-run]`:把到终态超过 N 天的 `done/` 记录打包进 `spool/archive/<年-月>.tar.gz`(追加用新的分卷文件,不改旧包),核对包内文件后再删除原文件。**不归档**:槽位仍未放行的、仍是某个队列头 `last_terminal` 的、被未作废裁定引用的消息。
3. 审计日志:`a2a audit rotate --max-size <MB>`,超过时把当前文件改名为 `audit-<时间>.jsonl` 并压缩成 `.jsonl.gz`,新建空文件。
   - **内部读取必须看到全部历史**(Codex 审核阻断 2)。核对代码,依赖审计历史的有:裁定补做(`rulings.apply` 按 ruling_id 去重)、未生效裁定(`rulings.unapplied`,broker 启动补做)、已送达裁定关联(`broker._delivered_ruling`,裁定可能很早)、IP 删除续做(`ipops` 按 op_id 去重)、`a2a resolve` 查拒绝记录。只挡住"未完成的裁定"不够,所以改为:`AuditLog.read()` 按时间顺序读**所有已轮转分段 + 当前文件**。轮转只用于压缩旧数据、节省磁盘,不改变任何内部逻辑看到的内容。
   - **单一归属的轮转状态机**(Codex 第 2 轮):每条记录在任一时刻只属于一个文件,读取不会重复也不会遗漏。分段按**代次**编号 `audit.d/<代次>.jsonl`(或压缩后的 `<代次>.jsonl.gz`),代次 = 已有最大代次 + 1。在审计写锁(独占)内:
     1. `os.rename(audit.jsonl, audit.d/<代次>.jsonl)`:一步原子改名,记录从"当前文件"整体转为"第 N 代",没有中间状态;
     2. 新建空的 `audit.jsonl`(崩溃在 1、2 之间也无妨:写入本来就以 O_CREAT 打开,下次追加时自动创建);
     3. 压缩:写 `<代次>.jsonl.gz.tmp` → fsync → 解压核对与原文件逐字节一致 → 改名为 `<代次>.jsonl.gz` → 删除 `<代次>.jsonl`。
   - **读取规则**:同一代次若 `.jsonl` 与 `.jsonl.gz` 同时存在(压缩到一半崩溃),只读 `.jsonl`,`.gz` 视为未完成,下次轮转时重做;`.tmp` 一律忽略并清理。按**代次升序**、再读当前文件,每个文件按追加顺序;**不按事件时间戳排序**(时间戳在拿锁之前生成,可能与追加顺序不同)。
   - **损坏的分段 fail-closed**(Codex 第 3 轮):`.jsonl.gz` 解压失败、gzip 校验(CRC)不符,或代次编号出现缺口(中间某代不见了),`read()` 抛 `AuditCorrupt`,不跳过、不返回不完整的历史。依赖它的地方随之停下:broker 启动恢复失败即拒绝启动(不投递);`a2a resolve`、`a2a ruling void`、`ip remove` 续做都报错退出;`a2a audit` 查询报出是哪一个分段。修复由人工处理(从备份恢复该分段),README 写明。当前文件中间的坏行仍按现有规则容忍(那是已有的、经过审核的行为,不在本方案改动)。
   - `read()` 在审计锁的共享模式下列出并读取;轮转持独占锁,读与轮转不交错。现有的"当前文件尾部残片隔离"只作用于当前文件,发生在追加前,不受影响;改名后的分段尾部如有残片,读取时按现在的规则容忍。
   - 裁定的"写入—生效"本身在 `rulings.lock` 内,轮转不拿这把锁也不会改变裁定能看到的内容(内容只增不减)。
   - 代价:读取量与现在相同(现在也是每次读整个文件),多了解压。数据量大到读取变慢时,再考虑给内部读取加索引(将来考虑)。
4. 不自动定时执行;要定时就由用户配 systemd timer / cron(README 给示例)。

**需要用户决定**:默认保留天数(建议 30 天)、审计日志轮转大小(建议 100 MB)、磁盘剩余空间提示阈值(建议 10%)。

**测试**:单元:归档后 `get` 查不到但包里有、不归档的三种情况、中途失败(打包后、删除前)可重跑且不丢不重;审计轮转在锁内、轮转后写入正常;**轮转后 `read()` 仍按顺序返回全部历史;OPERATOR_RULING 已写、RULING_APPLIED 未写时轮转,broker 启动仍补做该裁定;已送达裁定在旧分段里时自动放行仍关联 ruling_id;IP 删除中断后轮转再续做,不重复写 IP_REMOVED;读与轮转并发时读到的条数不减少;轮转三步之间各个崩溃点(改名后建新文件前、`.gz` 已发布但 `.jsonl` 未删)之后,读到的记录不重复、不遗漏、顺序与追加顺序一致,下次轮转补完;时间戳乱序的记录仍按追加顺序返回;**损坏的 `.gz` 分段(截断、改字节)与代次缺口:`read()` 报错,broker 启动恢复拒绝启动、resolve 报错**;**status 的用量数字包含归档与审计分段**。

**实现说明(2026-10-13)**:
- 用户确认的默认值:保留 30 天、审计 100 MB、磁盘剩余 10%。
- **审计**(`audit.py`):分段目录 `audit.d/`(由 `audit.jsonl` 得名);`rotate(max_bytes)` 在写锁内:先补完上次中断的压缩(清 `.tmp`、仍有 `.jsonl` 的代次重压),当前文件不超过阈值就返回;否则先隔离尾部残片(分段里只留完整行),`os.rename` 到下一代 `.jsonl`,建空的当前文件,压缩(`.tmp` → fsync → 解压逐字节核对 → 改名 `.gz` → 删 `.jsonl`)。`read()` 在审计锁的共享模式下按代次升序读分段(同代 `.jsonl` 与 `.gz` 并存读 `.jsonl`)、再读当前文件;`.gz` 读失败(截断、CRC、非 gzip)或代次缺口抛 `AuditCorrupt`。`size()` 计入全部分段。
- **fail-closed 落点**:CLI `main()` 统一捕获 `AuditCorrupt`,输出 JSON 错误、退出码 6(`broker run` 启动恢复、`resolve`、`ruling void`、`queue`、`ip remove` 等都经过这里);broker 投递循环里的 `_delivered_ruling` 已在"broker 内部错误"处理内,消息原样留着、冷却后再试。
- **归档**(`archive.py`):按到终态时间(`updated_at`)筛选;排除未放行槽位(读各目标队列状态,损坏即报错)、未作废裁定引用的 `msg_id` / `retry_msg_id`(CLI 从审计算出后传入)、pending 里仍有同一条的(终态归档中断,等 broker 恢复清理;方案未列,实现时补上);按月份新建分卷 `<年-月>-<n>.tar.gz`,`.tmp` → fsync → 逐个核对 → 改名 → 删原文件;重跑时已在旧分卷且内容一致的只补删,内容不同则停止。读不出的分卷报错(不当作空)。`a2a status <msg_id>` 在 spool 找不到时查归档(输出带 `archived: true`)。
- **存储用量**(`storage.py`):`a2a status` 不带 msg_id 时输出;提示三项:磁盘剩余低于 10%、审计当前文件超过 100 MB、有终态记录超过 30 天。状态目录不存在时按其最近的已存在上级取磁盘信息。
- **测试**:`test_audit_rotation.py` 13 个(阈值、轮转后全部历史按追加顺序、时间戳乱序、尾部残片先隔离、size、两个崩溃点后不重不漏且下次补完、`.tmp` 清理、截断 / 改字节 / 代次缺口 fail-closed、旧分段里未生效的裁定仍被找到、损坏时 `rulings.unapplied` 抛错与 `a2a queue` 退出 6、读与轮转并发时条数只增不减);`test_archive.py` 11 个(归档后 get 不到但包里有且 status 可查、按月分卷、三类不归档、作废裁定不算引用、保留天数下限、dry-run、打包后删除前中断重跑不重、新分卷不改旧包、坏分卷停止、用量计入归档 / 审计分段 / 幂等记录、CLI)。审计 5 处、归档 4 处变异各自被发现。方案里"OPERATOR_RULING 已写未 RULING_APPLIED 时轮转,broker 启动仍补做""已送达裁定在旧分段时放行仍关联 ruling_id""IP 删除中断后轮转再续做不重复写 IP_REMOVED"三项,都只依赖 `read()` 返回全部历史:用"旧分段里未生效的裁定仍被 `rulings.unapplied` 找到"与"读到的条数与顺序不变"覆盖,没有逐个写 broker / ipops 的端到端用例。
- **Codex 审核(应修 4)已修**:① `audit.d/` 里除规范分段名(代次无前导零)与压缩临时文件外的任何文件 → `AuditCorrupt`(被改名的分段、`01.jsonl` 之类不再被静默忽略);② 审计里有读不出、未作废的裁定记录时 `a2a spool archive` 拒绝执行(无法判断哪些消息仍被引用);③ 轮转时隔离撕裂尾行也记 `AUDIT_REPAIRED`;④ 存储统计容忍并发归档 / 轮转中文件消失。新增单元 4 个,4 处变异各自被发现。Mac 单元 617 通过(跳过 51);VM 相关测试 238 通过、0 跳过。将来考虑:归档全程持 spool 锁并读入全部旧分卷,规模很大时会增加 broker 等待与内存占用。
- **Codex 复核(阻断 1、应修 2)已修**:① `audit.d` 存在但不是目录 → `AuditCorrupt`(不再当作没有分段);② 补完压缩前先校验分段目录,只清理规范命名的 `<代次>.jsonl.gz.tmp`,其他文件(如 `notes.tmp`)保留并由校验 fail-closed;③ 归档改为 `archive_guarded()`:在裁定锁内完成"检查读不出的裁定 → 算引用集合 → 归档",与 resolve / ruling void / broker 补做串行,锁顺序同 resolve(裁定锁在外、spool 锁在内),消除"先算引用、新裁定放行队列头、再归档"的竞态。新增单元 3 个,3 处变异各自被发现。
- **VM 测试中发现的原有缺陷(已修)**:`Spool.pending()` 不持锁读取,列目录之后、读文件之前,消息可能刚被别的线程 / 进程归档到 done/,读取抛 `SpoolError`;broker 扫描遇到它会"读不出目标队列"而**全局停止投递**——正常并发就能触发(VM 上 `test_broker.DrainingIpPromptInFlight` 偶发失败暴露,单跑 10 次都通过)。改为:读取时文件不存在(终态归档先写 done 再删 pending,状态更新是原子替换,不会出现文件暂时不存在)就跳过,其他读取错误照常报出。新增单元 1 个,变异被发现。Mac 单元 621 通过(跳过 51);VM 相关测试 242 通过、0 跳过。
- **Codex 复核(应修 1)已修**:文件消失不再一概跳过,只有 done/ 里确有该消息的有效终态记录才跳过;外部误删、目录异常等没有终态记录的消失照常报 `SpoolError`(fail-closed)。刚到终态的消息槽位未放行、不会被归档,所以不必再查归档包。单元测试覆盖两种消失,变异(不核验终态记录)被发现。

## F. 发送后多出的总结(327)+ 探测 8(需要调用模型)

**327**:阶段 6 观察到两边调用 `a2a_send` 后又输出一段"已发送"的总结。不影响通信,但浪费 token、拉长处理时间。改法:工具成功时返回给模型的文字改为"已发送,不需要再说明或总结,直接结束这一轮"(插件改动,不改模板)。

**探测 8**:模型调用 bash 工具时,pi 二开版(fnx)会不会弹人工批准。目前测试都用 `--no-builtin-tools`(模型只有 `a2a_send`);正式使用需要 bash 时这关系到会不会卡住。

**一次真实 fnx 测试**(VM,独立会话,uart 一个 IP,预计 4–6 次模型调用,约 5 分钟):
1. fnx_dv 不加 `--no-builtin-tools`,人工 prompt"运行 `echo probe8` 并告诉我结果" → 观察是否弹批准、herdr 状态。
2. 恢复测试配置,走一遍 dv → sw → dv 的链路 → 观察 327 改动后是否还输出总结。
3. 链路中途 `a2a topology` 新增一条边 → 下一轮处理时 `a2a_send` 的 enum 出现新边(C 的真实验证)。

**需要用户同意**后执行。

---

## 不在范围内

- 75 行(另外三个智能体)、184 行(`a2a_codex`)、299 行(升级 herdr)、329 行(正式模板):有外部依赖,见 05 号。
- 366 行(并行 spawn)、383 行(socket):有依据地推后。
- 384 行(Monitor / GUI):下一阶段;E 的存储用量会是 Monitor 的一项数据来源。
