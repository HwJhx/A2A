# 对 Claude 复核意见的独立判断

> 日期：2026-10-09
> 范围：仅核对 `herdr/a2a_codex`、`herdr/codex/实施路线图-v1.md`、决策原文及 Claude 提供的复核探针。
> 初始复核时未修改 `herdr/a2a_codex`、`herdr/a2a` 或 `herdr/claude`；后续实施与验证状态见第五节。测试继续交由 `codex-test` 在 VM 执行。

## 总结

Claude 的复核整体可靠，但并非每条都应原样接受：

- C1–C5、C7–C10、C13 的核心事实成立；C4、C5、C10 的修复边界要按下文补充。
- C6、C11、C12 是部分成立：存在真实风险/缺口，但原意见将适用范围或影响说得过满。
- C14 不成立：`preflight_launcher` 确实尝试读取启动脚本，并将读取失败报告为错误。
- R1、R2、R4、R5、R8 成立；R3、R6、R7 部分成立，需精确化而非照抄原结论。
- D8–D11 在决策原文中清楚，无需向用户确认：blocked 立即失败；done 可投递；agent_id 不带项目名前缀；接收方暂不校验消息来源、去重在 Broker。

复核探针已由 `codex-test` 在 OrbStack Ubuntu VM 执行，退出码 0；报告环境为 Ubuntu 22.04.5 LTS / Python 3.10.12，使用 `PYTHONPATH=herdr PYTHONDONTWRITEBYTECODE=1`，未改仓库文件。探针输出确认 Q1–Q10 的行为。另据用户提供且已由 `codex-test` 在 VM 实跑确认，当前 47 项测试 47/47 通过；这不改变其中没有真实 Herdr 集成测试的事实。

## 一、代码问题 C1–C14

| 编号 | 判断 | 独立核实与证据 | 处置 / 优先级 |
|---|---|---|---|
| **C1（高）** | **同意，附边界说明** | `cli.py` 的 `send-prompt` 接受任意 target 和任意 text，直接调用 `HerdrClient.send_prompt`；`pyproject.toml` 将它发布为 `a2a-herdr` 命令。VM 探针 Q10 输出两项均为 `True`。这确实给 agent-facing API 增加了绕过 Router/模板的直接入口，违背 D1、D2。需要准确说：同一 OS 用户若可运行 `herdr agent prompt`，仅移除此命令不能构成强安全隔离；它是必须纠正的接口/部署策略违规，不是完整的操作系统权限边界。证据：`herdr/a2a_codex/cli.py:11-37`、`pyproject.toml:12-13`；决策见 `04-design-python-framework.md:770-771`。 | **P0：接入 agent 前处理。** 从 agent 可访问的安装入口移除自由文本发送能力；保留仅供 Broker 内部调用的底层 HerdrClient 方法。另明确部署边界，避免宣称此 API 单独提供安全隔离。 |
| **C2（中）** | **同意** | `Registry.default_state_dir()` 对 `A2A_STATE_DIR` 直接 `resolve()`，未要求绝对路径；Registry 与 TopologyStore 的显式路径也会按当前 cwd 解析。Spool/AuditLog 则拒绝相对路径。VM 探针 Q1 在两个 cwd 下得到两个不同目录。与 D15“路径必须绝对”相冲突。证据：`registry.py:74-95`、`topology.py:154-156`、`spool.py:26-31`、`audit.py:14-19`；决策见 `04-design-python-framework.md:784`。 | **P0：部署前修。** 状态目录错位会让后续注册和消息状态彼此不可见；统一要求 `A2A_STATE_DIR` 及显式持久化路径为绝对路径。 |
| **C3（中）** | **同意，限定为“当前仓库没有回归测试”** | `a2a_codex/tests/` 只有 client、router、topology/registry 三个测试文件，没有启动真实 Herdr session/pane/fnx 的集成测试。路线图记载过真实 VM 手动验证，因此不能说历史验证没发生；但代码库没有可重复测试和回归保护。证据：测试文件清单及 `实施路线图-v1.md:65-85`、`:211`。 | **P1：阶段 7 真实 pane 投递前补。** 增加可在 VM 显式运行/跳过的集成测试，覆盖真实 CLI、Herdr、fnx 启动/识别/等待/prompt/read；保留手动验证记录作为补充。 |
| **C4（中）** | **部分同意** | `AuditLog.read()` 对每个非空行直接 `json.loads`，中间完整行损坏会使整个读取抛错；VM 探针 Q3 确认。`read()` 也会先调用 `_repair_tail_unlocked()`，该函数会截掉最后一个没有换行符的尾段。后者是当前崩溃恢复设计，不应简单移除；但读接口因此不是只读，且有效 JSON 但缺最终换行的记录也会被截掉。证据：`audit.py:22-48`、`:71-84`；测试 `test_router.py:327-340`。 | **P2：明确恢复策略。** 保留尾部崩溃修复，但把副作用写入 API 契约或拆为显式 repair；中间坏行不能静默吞掉，宜报告行号并隔离/保留损坏数据，避免整份日志不可诊断。 |
| **C5（中）** | **同意** | `_ERRORS` 只映射 server/agent 状态与 timeout；探针 Q7 确认 `agent_name_taken`、`invalid_agent_name`、`agent_prompt_stalled` 都退化为 `HerdrError`。官方文档说明 `agent_prompt_stalled` 不证明输入未发送，重试前应先读取 agent，故 Broker 必须区分“结果不确定”，不能按普通可重试错误盲目重发。证据：`errors.py:51-65`；[Herdr Agent automation 文档](https://herdr.dev/docs/agent-automation/)、[CLI reference](https://herdr.dev/docs/cli-reference/)。 | **P0：Broker 实现前处理。** 添加明确错误类型/映射和“投递结果不确定”状态；不得把 stalled 直接归入安全重试。名称类错误可一并映射。 |
| **C6（低–中）** | **部分同意** | `AgentRecord.session` 和 `register()` 允许 `None`；Router 默认将无环境变量的会话归为字符串 `default`，随后以该会话查 sender，因此 `session=None` 的记录匹配不到。探针 Q5 确认。`resolve_sender(session=None)` 的确不限定 session；若同 pane_id 在多 session 重复会报歧义，但只有一条匹配时会解析到该记录。证据：`registry.py:42-50`、`:182-192`、`:226-245`；`router.py:46-47,91-110`。 | **P1：身份/Registry 固化前处理。** Registry 记录应要求规范化的显式 session（无环境变量时写 `default`）；身份解析默认采用 Router 当前 session，避免省略参数时跨 session 宽泛匹配。 |
| **C7（中）** | **同意** | Registry 实现有 `flock`、临时文件、`fsync`、`os.replace`，但 Registry 测试没有多进程读改写、写入中断/崩溃或损坏记录恢复测试。现有多进程拓扑测试不能替代 Registry 测试。证据：`registry.py:98-106,135-156`；测试 `test_topology_registry.py:179-290`；路线图 `:126,:140`。 | **P1：宣称 Registry 持久化可靠前补。** 加 Registry 专属并发、原子写入故障注入、损坏 JSON/损坏记录行为测试。测试仍交 `codex-test` VM 执行。 |
| **C8（低）** | **同意** | `argparse` 对 `--until` 使用 `action="append"` 且默认列表非空；探针 Q6 实际捕获 `['idle', 'done', 'blocked', 'idle']`，因此显式参数不能替换默认值。证据：`cli.py:24-27,35-36`。 | **P2：小修。** 默认设为 `None`，解析后再选择默认状态列表，并加显式单状态回归测试。 |
| **C9（低）** | **同意** | `TopologyStore.current` 仅在成功解析后更新 `_signature`；文件保持损坏时每次读取都会再次尝试解析。探针 Q4 连读五次解析五次。`revision` 返回 `inode:mtime_ns:size`，探针 Q9 证明只 touch 即变化。证据：`topology.py:163-165,218-244`。 | **P2：动态拓扑稳定后改进。** 记住最后一次失败签名以避免重复解析；修订号改为配置内容摘要/单调版本，避免 touch 产生伪变更。 |
| **C10（低）** | **部分同意（状态回退属实，清理属运维策略）** | `Spool.update()` 只校验状态名，不阻止 `DELIVERED → QUEUED`；探针 Q8 实际接受并读回 QUEUED。终态文件仍留在 `done/`，会破坏终态/队列语义，必须在 Broker 前修。done 无清理策略也属实，但无限保留可能是审计要求，不能未经保留期/归档策略就直接删除。证据：`spool.py:99-122`；探针 Q8。 | **P0：Broker 前禁止终态回退、定义合法状态迁移。** **P3：** 单独确定审计保留期、归档/清理工具和磁盘告警，不做无策略删除。 |
| **C11（低）** | **部分同意** | `Spool.__init__()` 在独占锁内扫描全部 `done/*.json` 做恢复；`get/pending/pending_targets/done` 都拿独占锁。探针 Q2 在 VM 测出 500/3,000/10,000 条约 0.01/0.03/0.10 秒，支持随数据量增长；Claude 报告的 0.26 秒不是本次 VM 复测值，绝对耗时会受环境影响。当前 `a2a send` 尚不存在，故“每次 a2a send 都构造 Spool”不能当成已观察事实。Broker 等待 Herdr 时不应持 Spool 锁，这一设计警告成立。证据：`spool.py:26-58,136-166`、路线图 `:208-211`；VM 探针 Q2。 | **P1：实现 Broker 时必须保证锁只覆盖短的文件操作，绝不跨 Herdr 等待/调用。** 恢复移至 Broker 启动或显式 `recover()`；只有基准测试证明需要时，再考虑读写锁/增量恢复。 |
| **C12（低–中）** | **部分同意（API 扩展项，不全是当前缺陷）** | 当前客户端没有 `pane process-info`、pane `send-keys`、`wait-output` 等封装；`rename_agent()` 不支持清除名称；`send_prompt(wait=True)` 允许不传 timeout；`start_agent()` 实际执行 `pane run`；模型没有 READY 常量。这些源码事实成立，但当前阶段 3 的明确 API 清单不要求所有 pane 原始控制接口，`create_pane(pane_id)` 是以现有 pane 为 split 锚点，也不单独证明设计错误。无 timeout 时 HerdrClient 的 subprocess 仍用默认 30 秒超时，故不等于无限等待。官方 Herdr 支持 `agent rename ... --clear`。证据：`herdr_client.py:142-178,194-219`、`models.py:18-28`；[Herdr CLI reference](https://herdr.dev/docs/cli-reference/)。 | **P2：按实际编排需求选择性补齐。** 优先加清除 agent 名称、process-info 和 `wait=True` 超时约束/清晰语义；terminal 原始控制 API、READY 归一化常量另按 Broker 需要决定。可以把 `start_agent` 内部/文档命名改为 `run_agent`，但不是阻断项。 |
| **C13（低）** | **同意** | 文件锁/原子写分别在 Registry、TopologyStore、storage.py 实现；AuditLog 与 Spool 为默认目录引入 Registry。代码重复和模块依赖属实，但短期功能没有直接失败证据。证据：`registry.py:98-106,135-156`、`topology.py:167-194`、`storage.py:12-40`、`audit.py:9,14-18`、`spool.py:10-12,26-31`。 | **P3：维护性重构。** 稳定行为和测试后提取共用路径/文件原语，避免与状态路径默认值耦合。 |
| **C14（低）** | **不同意** | 原意见称 `preflight_launcher` 不检查可读性，但实现先确认文件存在，再 `open(path, "r", ...)` 读取；`OSError` 会返回“无法读取启动脚本”。所以“没有任何可读性检查”与代码直接矛盾。探针没有覆盖这一项。证据：`launcher.py:38-50`。 | **不改。** 如需更严格的权限位/执行方式校验，应另提需求；当前以实际打开读取判断可读性比只检查 mode 位更贴近运行结果。 |

## 二、路线图问题 R1–R8

| 编号 | 判断 | 独立核实与证据 | 处置 |
|---|---|---|---|
| **R1** | **同意** | 阶段 6 把 blocked 写成保留队列、把 unknown 写成等待或转人工；决策设计明确 blocked 立即失败、不排队，unknown 不作为 READY，等待到超时失败。done 也已确定可投递。证据：路线图 `:217-224`；设计 `04-design-python-framework.md:477-509`，D8/D9 在 `:777-778`。 | 修阶段 6：`blocked → 立即失败，不排队/重试`；`unknown → 只等待，超时失败`；`done → READY`。 |
| **R2** | **同意** | 阶段 7 要求软件 agent “校验消息来源”，与 D11 冲突。设计明确接收方拿到固定句式、暂不验证来源，去重只在 Broker；不能把来源验证放回 agent。证据：路线图 `:260-265`；设计 `:552-558`、D11 `:780`。 | 删除接收方来源验证步骤；保留对任务 IP/报告内容的业务处理，但不伪称文本本身可验证发送身份。 |
| **R3** | **部分同意** | 阶段 7 示例确实用 `soc-a/uart/verification → soc-a/uart/software`，容易和 D10 的 `agent_id={角色码}_{ip}` 混淆；但 D13 明确保留 project_id 并进入完整身份/Registry，因此带 project 的复合身份元组本身不违反 D10。问题是表示不清且 role 仍用长名。证据：路线图 `:256-258`；设计 D10/D13 `:779-781`。 | 改写为明确字段：`project_id=soc_a, ip_id=uart, role=dv, agent_id=dv_uart` → `role=sw, agent_id=sw_uart`；project_id 独立保留，不拼进 agent_id。 |
| **R4** | **同意** | 阶段 1 仍将 done/blocked 作为待确认；阶段 6 将 done 标为建议。D8/D9 已定，且设计记录了 VM 实测：done 不会自动回 idle；只等 idle 会超时，等 done 可返回。证据：路线图 `:21-24,:219-223`；设计 `:511-516`、D8/D9 `:777-778`。 | 将问题从待确认改为决定/验收项；保留并引用实测依据。 |
| **R5** | **同意** | 阶段 1 产物列出独立 `a2a_message_templates.yaml`；设计和实现都把每条有向边的 template 放在拓扑中，模板是同一配置的唯一事实源。证据：路线图 `:26`；设计 `:348-400`；`topology.py:197-216`。 | 阶段产物改成拓扑配置（包含 edges/template）和协议文档，不再列独立模板文件。 |
| **R6** | **部分同意** | 阶段 3 记载的 fnx_dv 真实 VM 验证是已发生的手动验证，路线图也区分 fnx_sw 的 walkthrough；不应把历史验证改写成“未做”。但 `a2a_codex/tests` 没有真实 Herdr 集成测试，记录也没有足够的复现脚本/完整命令，无法回归。 | 将阶段 3 状态标成“历史手动 VM 验证，非自动集成测试”，列出复现前置条件/步骤；另在阶段 7 增加 VM 集成测试和可复制命令。 |
| **R7** | **部分同意** | “哪些组件覆盖、哪些没有”确实没说清，Registry 并发/崩溃测试缺失。但“拓扑、Spool、Audit 全部覆盖”也要限定：拓扑有跨进程更新和坏配置保留快照测试；Spool 有并发、注入崩溃恢复测试；Audit 有并发追加和尾部半行恢复测试。拓扑本身没有同类崩溃注入测试，Registry 的损坏 JSON 只覆盖顶层类型，不等于语法损坏/记录损坏恢复。证据：路线图 `:126,:140,:152-164,:203-207`；测试 `test_topology_registry.py:152-167`、`test_router.py:261-340`。 | 按组件拆开阶段 4/5 的测试矩阵；准确标注拓扑并发/坏配置已测、拓扑崩溃原子性未测、Registry 并发/崩溃/损坏记录未测、Spool/Audit 已覆盖的具体场景。 |
| **R8** | **同意** | 阶段 5 明确 agent 只能调用 `a2a send <edge_id>`，但当前 `pyproject.toml` 已安装 `a2a-herdr send-prompt <target> <text>`。不过未来的 `a2a send` 尚未实现，所以这里是当前 agent-facing 发布入口与目标接口的冲突，不是两个已实现命令相互冲突。证据：路线图 `:168-172,:208-209`；`cli.py:15-31`、`pyproject.toml:12-13`。 | 与 C1 合并按 P0 处理：在将包放到 agent 可访问环境前收紧安装入口；路线图明确允许 agent 使用的仅是受 Router 控制的 `a2a send <edge_id>`。 |

## 三、改动顺序建议

1. **P0，Broker/agent 接入前：** C1/R8 收紧 agent-facing 命令入口；C5 区分 `agent_prompt_stalled` 的不确定投递结果；C10 禁止终态回退。否则会有自由文本绕路、重复 prompt 和队列状态失真的风险。
2. **P1，进入多 agent/Broker 前：** C2 强制绝对状态路径；C6 统一 session 规范；C7 补 Registry 并发/损坏/崩溃测试；C11 设计 Broker 短锁和启动恢复；C3/R6 建立可复现的真实 Herdr VM 集成测试。
3. **同步修路线图：** R1–R8 按上述判断修订，尤其将 D8–D11 当作既定决策，不重新开放确认；R3/R7 保留必要限定，避免把不同身份字段或不同组件测试混成一项。
4. **P2/P3 后续：** C4 明确审计修复语义；C8 修 argparse；C9 改 revision/坏文件缓存；C12 按实际需求补 API；C10 的 done 清理策略和 C13 公共文件工具重构后置。
5. **不改：** C14 当前判断不成立。

## 四、验证边界

- VM 探针确认：相对 `A2A_STATE_DIR`、Spool 恢复扫描开销、审计中间行损坏、TopologyStore 坏文件重复解析/mtime 修订号、`session=None` 路由失败、`--until` 追加默认值、缺少错误映射、终态回退、自由文本 CLI 入口。
- 探针 Q8 同时确认路径穿越被 Spool 拒绝；这是正向安全行为，不应被 C10 的状态回退问题掩盖。
- 探针没有验证 C3、C7、C12、C13、C14 的所有细节；这些条目按源码和测试清单核查。C14 的源码证据与 Claude 结论相反。
- 初始复核时的 47/47 是真实执行的 VM 单元/文件系统/多进程测试结果，不等价于 47 项真实 Herdr/pane 端到端测试；真实 Herdr 集成测试仍缺。

## 五、后续实施与 VM 验证（2026-10-09）

按用户要求，先处理不依赖 Broker 状态迁移表的问题；状态迁移表尚未收到/定稿，因此 C10 的迁移约束没有擅自实现。

- **已实现：** C1/R8 移除 `a2a-herdr send-prompt <target> <text>`，保留 HerdrClient 作为 Broker 底层 API，并明确这不是 OS 级隔离；C2 统一拒绝相对状态/存储路径；C5 映射 name 错误并新增 `HerdrPromptOutcomeUnknown`，Herdr stalled、prompt timeout 和本地 subprocess timeout 都按可能已送达处理；C6 禁止 `session=None` 注册并要求 sender lookup 显式 session；C8 修复 `--until` 默认值追加；C9 revision 改为内容 SHA-256 摘要并缓存失败文件签名；C12 增加 pane process-info、agent 名称清除支持，并要求 `send_prompt(wait=True)` 提供 timeout；C13 中 Spool/AuditLog 默认目录已从 Registry 类依赖中解耦。
- **C4 的安全补充：** 有效 JSON 但尾部缺换行时保留记录并补换行；半行崩溃尾记录仍按既有恢复策略截断。中间损坏行仍会报错，不做静默跳过；`read()` 的尾部恢复副作用需要在 API 契约中说明。
- **新增回归：** Registry 六进程并发注册、`os._exit` 替换前崩溃注入、损坏 JSON/损坏记录 fail-closed、相对路径拒绝；TopologyStore touch revision 稳定与损坏文件只解析一次；CLI 不接受自由文本发送；prompt outcome-unknown；AuditLog 有效无换行尾记录；pane process-info CLI 参数/结果映射。
- **真实 Herdr 集成测试：** 新增 opt-in `tests/test_integration_herdr_vm.py`，对指定已有 session 创建/关闭一个临时 tab，以 `exec -a pi sleep` 假 agent 验证识别、argv0、重命名、wait、read 和 prompt，不调用模型。本次 VM 没有 `test1` session；按约束未拿正在运行的 `a2a_codex_verify` 替代，也未创建 session，因此该集成测试本次 skipped，真实 Herdr E2E 仍未验证。
- **VM 测试结果：** `codex-test` 在 Ubuntu 22.04.5 LTS / Python 3.10.12 执行最终普通完整套件：`Ran 67 tests`，66 项通过、1 项集成测试 skipped、0 失败。路线图阶段 3/4/5 已由测试 pane 更新。没有在本机自行运行测试。
- **仍待处理：** C10 等共享状态迁移表定稿后实现；C3 的真实 Herdr 集成测试已写但需在指定隔离 session 实跑；C7 Registry 并发/崩溃测试已由 VM 回归通过；C11 的恢复扫描应结合 Broker 启动生命周期优化，且 Broker 不可持锁等待 Herdr；C12 其余 pane 原始控制 API 是按需扩展；C4 中间损坏的可诊断/恢复政策仍保守 fail-closed；C13 文件锁/原子写工具进一步合并属 P3。
