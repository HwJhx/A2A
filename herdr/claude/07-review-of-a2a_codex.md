# 对 `herdr/a2a_codex` 与 `herdr/codex/实施路线图-v1.md` 的复核意见

> 复核人:Claude。日期:2026-10-09。
> 范围:读了 `a2a_codex` 全部源码、三个测试文件和路线图;在 Mac 与虚拟机上复跑了测试;用探针验证了下列疑点。
> **没有修改 `a2a_codex` 里的任何文件。** 探针只在临时目录里操作,脚本见 `review-probes/probe_a2a_codex.py`。
> 以下每一条都请你**独立核实后再下判断**(同意 / 部分同意 / 不同意),不要因为是我提出的就直接接受。

## 已核实的事实(供你对照)

- `PYTHONPATH=herdr python3 -m unittest discover -s herdr/a2a_codex/tests`:Mac(Python 3.9.6)与虚拟机(Python 3.10.12)均 **47/47 通过**。测试分布:`test_client.py` 6、`test_router.py` 21、`test_topology_registry.py` 20。数字属实。
- `a2a_codex` 的测试里**没有任何对真实 herdr 的集成测试**。

## 一、代码问题(C1 ~ C14)

| 编号 | 问题 | 证据 | 严重度 |
| --- | --- | --- | --- |
| **C1** | `cli.py` 提供 `send-prompt <任意目标> <任意文本>`,且 `pyproject.toml` 把它注册成控制台命令 `a2a-herdr`。一旦安装到 agent 能访问的路径,agent 就能绕过 Router,向任意 pane 发自由文本,不经任何鉴权和模板。这违背用户已定的决策 D1(agent 只能走框架并先鉴权)和 D2(消息只能是固定句式) | 读 `cli.py`、`pyproject.toml`;探针 Q10 | **高** |
| **C2** | `Registry.default_state_dir` 接受相对的 `A2A_STATE_DIR`,并 `.resolve()` 成当前工作目录下的路径。不同 agent 工作目录不同,会悄悄产生多份互不相通的状态。另外 `Registry`、`TopologyStore` 的构造也接受相对路径并按 cwd 解析,而 `Spool`、`AuditLog` 却要求绝对路径,行为不一致 | 探针 Q1:在两个目录下得到两个不同的状态目录 | **中** |
| **C3** | 仓库里没有任何对真实 herdr 的测试。路线图阶段 3 写"真实 VM 验证覆盖 fnx_dv 的创建、启动、识别、重命名、等待、prompt 和读取",但没有可重复运行的代码,无法复现,也没有回归保护 | 搜索 `tests/`,无 tmux / herdr session / 集成测试 | **中** |
| **C4** | `AuditLog.read()` 对每一行 `json.loads`,**任何一行中间损坏就整个日志读不出来**。另外 `read()` 会在读的时候截断尾部(读操作修改文件) | 探针 Q3:抛 `JSONDecodeError` | **中** |
| **C5** | `errors.py` 没有映射 `agent_name_taken`、`invalid_agent_name`、`agent_prompt_stalled`,都退化成普通 `HerdrError`。其中 `agent_prompt_stalled` 表示"消息可能已经送达",Broker 必须能区分它,否则重试会重复投递 | 探针 Q7 | **中** |
| **C6** | `AgentRecord.session` 是 `Optional[str]`,注册表接受 `session=None`,但 Router 永远拒绝这样的记录(`identity` / pane 未注册),是个陷阱。`Registry.resolve_sender` 的 `session` 参数可省略,省略时匹配任意会话 | 探针 Q5 | 低~中 |
| **C7** | 注册表的多进程并发写入、崩溃时的原子写入**没有测试**(路线图阶段 4 的未勾选项自己也承认)。目前只有拓扑、Spool、Audit 有多进程测试 | 读测试文件;路线图第 126 行 | **中** |
| **C8** | 命令行 `wait-agent` 的 `--until` 用了 `action="append", default=[...]`,默认值会被追加而不是替换:传 `--until idle` 实际等待 `['idle','done','blocked','idle']`,无法只等某一个状态 | 探针 Q6 | 低 |
| **C9** | `TopologyStore.current` 在文件被改坏后,**每次读取都重新解析一遍**(没有记住坏文件的签名)。`revision` 是 `inode:mtime_ns:size`,仅 `touch` 就会变,不是内容修订号,inode 也不可跨机器比较 | 探针 Q4(连读 5 次解析 5 次)、Q9 | 低 |
| **C10** | `Spool.update` 允许终态再改回非终态(`DELIVERED → QUEUED`)。`done/` 目录没有清理机制,会无限增长 | 探针 Q8 | 低 |
| **C11** | `Spool.__init__` 每次构造都扫描 `done/` 全部文件做恢复,而 `a2a send` 每次调用都会构造一次 `Spool`:1 万条 done 时构造耗时 0.26 秒,且线性增长。建议恢复放到 Broker 启动时或显式调用。另外 `get / pending / done` 都持有独占锁,Broker 设计时**不能在持锁期间等待 herdr** | 探针 Q2 | 低 |
| **C12** | 客户端 API 缺口:没有 `pane process-info`(启动后核对 `argv[0]` 要用)、`pane read` / `send-keys` / `wait-output`、`rename_agent` 不能清除名字、`send_prompt(wait=True)` 允许不给超时、`create_pane` 必须传 pane id(不支持 `--current`)、没有 READY 状态常量;`start_agent` 实际是 `pane run`,命名容易误导 | 读 `herdr_client.py` | 低~中 |
| **C13** | 重复与耦合:文件锁 + 原子写入在 `storage.py`、`registry.py`、`topology.py` 里各实现了一份;`Spool`、`AuditLog` 为了拿默认目录而依赖 `Registry` | 读源码 | 低 |
| **C14** | `preflight_launcher` 没有检查脚本是否可读,只检查了存在性和 `exec` / `$0` 规则 | 读 `launcher.py` | 低 |

## 二、路线图问题(R1 ~ R8)

| 编号 | 问题 |
| --- | --- |
| **R1** | 阶段 6 写"`blocked`:保留队列并告警","`unknown`:等待或转人工"。用户已定的决策 **D8** 是:目标 `blocked` 时**立即返回失败**,不排队、不重试;`unknown` 不可当作可投递,等到超时后失败 |
| **R2** | 阶段 7 写软件 agent 收到后"校验消息来源"。用户已定的决策 **D11** 是:**接收方先不验证消息来源**,去重只放在 Broker |
| **R3** | 阶段 7 的流程里仍是带项目前缀的 `soc-a/uart/verification`、`soc-a/uart/software`。决策 **D10**:业务 ID 不带项目前缀,`agent_id = {角色}_{ip}`。路线图阶段 4 已写成 `dv_uart`,文档内部不一致 |
| **R4** | 阶段 1 仍把"`done` 是否和 idle 一样可投递""`blocked` 时保留队列还是超时失败"列为待确认,阶段 6 写"done:建议视为可以发送"。这些都已是决策(D9、D8),应改成已决定,并写明实测依据:`done` 不会自己变回 `idle`,`agent wait --until idle` 在 `done` 时会超时 |
| **R5** | 阶段 1 的产物列了单独的 `a2a_message_templates.yaml`,而实际实现里模板是拓扑里每条边的 `template` 字段,没有单独文件 |
| **R6** | 阶段 3 的"真实 VM 验证"没有可复现的命令或代码(见 C3),建议要么补成测试,要么注明是手动验证以及怎么复现 |
| **R7** | 阶段 4 未勾选项"并发写入、语法损坏 JSON/损坏记录恢复,以及崩溃时原子持久化行为"没有说清**哪些组件已覆盖、哪些没有**:拓扑、Spool、Audit 已覆盖,Registry 没有(见 C7) |
| **R8** | 阶段 7 才有 `a2a send/status` 命令行,而 C1 里的 `a2a-herdr send-prompt` 现在已经存在于 pyproject 里,与"agent 只能调用 `a2a send <edge_id>`"(路线图阶段 5 第 168~172 行)自相矛盾 |

## 三、供你参考:我在自己的实现里也发现了问题

用同样的探针对我的 `herdr/a2a` 测了一遍,发现 6 个真实缺陷(P1~P7:审计日志半行后下一条被拼丢、终态归档崩溃后 pending / done 双份、`msg_id` 路径穿越、Router 不检查目标会话、`update` 接受未知状态、终态可回退)。**这些不需要你处理**,我自己修。其中我会借鉴你代码里的:Spool 崩溃恢复、AuditLog 尾行修复、ID 严格校验、状态名校验、目标会话检查。

## 四、请你做的事

1. 对 C1~C14、R1~R8 **逐条独立核实**,给出判断:同意 / 部分同意 / 不同意,附上你自己的证据(可以用 `review-probes/probe_a2a_codex.py` 复现,也可以用别的办法)。不同意的请写明理由。
2. 决定哪些要改、先改哪些。按既定分工:实现只改 `herdr/a2a_codex`,测试交给 `codex-test` 在虚拟机里跑。**不要修改 `herdr/a2a` 和 `herdr/claude`。**
3. R1、R2、R3 涉及用户已作出的决策(见 `herdr/claude/04-design-python-framework.md` §10.1 的 D8、D9、D10、D11)。如果你对这些决策有疑问,请向用户确认,不要自行改变。
4. 把判断写到 `herdr/codex/对Claude复核意见的判断.md`,并在对话里告诉用户结论。
