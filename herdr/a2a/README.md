# a2a:基于 herdr 的多智能体编排框架

当前阶段:herdr 调用层、启动命令构造、拓扑(含动态修改和热加载)、身份、注册表、**Router(鉴权 + 固定模板 + 入队)**。
broker、lifecycle、`a2a send` 命令行后续按 `../claude/04-design-python-framework.md` 逐层添加。

唯一的第三方依赖是 PyYAML(`>=5.4`,已在 `pyproject.toml` 声明)。代码兼容 Python 3.9(Mac)和 3.10(虚拟机)。
拓扑与注册表代码移植自 `../a2a_codex`(该目录未被改动)。

## 目录

```
a2a/
├── pyproject.toml
├── src/a2a/
├── config/topology.example.yaml
│   ├── errors.py         # 异常类型(按 herdr 的 error.code 映射)
│   ├── herdr_client.py   # HerdrClient:herdr CLI 的薄封装
│   ├── launcher.py       # 覆盖 exec 的启动命令 + 启动脚本预检
│   ├── paths.py          # 状态目录与默认文件路径
│   ├── _fsutil.py        # 跨进程文件锁 + 原子写入
│   ├── topology.py       # Topology(不可变快照)+ TopologyStore(热加载 / 动态修改)
│   ├── identity.py       # AgentIdentity + resolve_sender(发送方判定唯一入口)
│   ├── registry.py       # 业务 ID <-> 当前 herdr 位置
│   ├── messages.py       # 消息模型、状态常量、合法迁移表(协议见 ../claude/08-protocol.md)
│   ├── spool.py          # 持久队列(pending/<目标>/ 与 done/);迁移校验、ID 校验、崩溃恢复
│   ├── audit.py          # 审计日志(追加写 JSON Lines;崩溃残片隔离到 .corrupt)
│   └── router.py         # 鉴权 + 拓扑校验 + 模板渲染 + 入队
└── tests/
    ├── test_herdr_client.py   # 单元测试(假 runner,不需要 herdr)
    ├── test_launcher.py
    ├── test_topology.py       # 校验、动态修改、热加载、跨进程并发、写入中途崩溃
    ├── test_registry.py       # 同上
    ├── test_identity.py       # resolve_sender 的每个失败分支
    ├── test_router.py         # 正向流程、全部反向用例、动态拓扑、FIFO、多进程并发发送
    ├── test_spool_audit.py    # 队列与审计日志:原子性、崩溃、并发、迁移表、ID 校验
    ├── test_protocol.py       # 迁移表的性质;协议文档与代码必须一致
    ├── test_paths.py
    └── test_integration_vm.py # 集成测试(需要真实 herdr,默认跳过)
```

## 运行测试

单元测试(Mac 或虚拟机都可以):

```bash
cd herdr/a2a
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
```

集成测试(在装了 herdr 和 tmux 的虚拟机里)。会自己起一个一次性的命名会话 `a2at<进程号>`,结束时停止并删除,不碰默认会话:

```bash
orb -m ubuntu
cd /Users/jhx/Documents/code/personal/pi_agent/A2A/herdr/a2a
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 A2A_INTEGRATION=1 python3 -m unittest discover -s tests -v
```

默认只用普通 shell 和 `exec -a pi sleep`,**不调用模型**。再加上 `A2A_INTEGRATION_FNX=1`,会启动真实的 `fnx_dv` / `fnx_sw` 界面(不向模型发送任何提示词):

```bash
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 A2A_INTEGRATION=1 A2A_INTEGRATION_FNX=1 \
  python3 -m unittest discover -s tests -k TestRealFnxLaunch -v
```

> 注意:虚拟机通过 OrbStack 共享 Mac 的目录。刚在 Mac 上改完文件,立刻在虚拟机里运行,偶尔会读到同步中的残缺文件(出现莫名的语法错误),等一两秒再运行即可。

## 用法示例

```python
from a2a import HerdrClient, READY_STATUSES, build_launch_command, preflight_launcher

c = HerdrClient("walk1")                       # 始终显式指定命名会话
assert c.is_server_running()

# 建 tab,注入身份;创建时就拿到 pane_id,不要事后去猜
created = c.tab_create(label="dv", cwd="/home/jhx/A2A_Test",
                       env={"A2A_ROLE": "dv", "A2A_IP": "uart"})
pane = created.pane_id

# 启动 fnx_dv:不能用 `agent start`,要用覆盖 exec 的命令(见 04 号文档 §2.3)
launcher = "/home/jhx/.forenyx/fnx_dv/bin/fnx_dv"
assert preflight_launcher(launcher) == []
c.pane_run(pane, build_launch_command(launcher))

# 等 herdr 识别 -> 改名 -> 等到可投递
c.wait_for_agent_detected(pane, timeout_s=60)
c.agent_rename(pane, "dv_uart")                # 重启后名字会丢,需要重新设置
c.agent_wait("dv_uart", until=READY_STATUSES, timeout_ms=30000)

# 发消息(原子写入文字+回车,并检查对方状态)
c.agent_prompt("dv_uart", "只回复两个字:收到")
```

## 状态目录

| 内容 | 位置(优先级从高到低) |
| --- | --- |
| 状态目录 | `A2A_STATE_DIR` > `$XDG_STATE_HOME/a2a` > `~/.local/state/a2a` |
| 拓扑文件 | `A2A_TOPOLOGY` > `<状态目录>/topology.yaml` |
| 注册表 | `<状态目录>/registry.json` |
| 消息队列 | `<状态目录>/spool/`(`pending/<目标>/`、`done/`) |
| 审计日志 | `<状态目录>/audit.jsonl` |

路径必须是绝对路径。agent 们的工作目录各不相同,相对路径会悄悄产生多份互不相通的状态,所以一律拒绝。

## 拓扑、身份、注册表用法

```python
from a2a import (AgentIdentity, Registry, TopologyStore, identity_env, resolve_sender)

store = TopologyStore("/home/jhx/.local/state/a2a/topology.yaml")
topology = store.current()                    # 文件被改动过会自动重新加载;store.revision 是修订号

# 动态修改(校验失败时整体拒绝,文件不变;多进程同时修改不会互相覆盖)
store.add_ip("spi")
store.add_edge("sw_ack", "sw", "dv", "{ip}驱动开发完成。")
store.set_template("dv_done", "{ip}已完成 UVM 验证，请开始驱动开发。")
store.remove_role("rtl", cascade_edges=True)  # 默认拒绝删除仍被边引用的角色

# 创建 tab / pane 时注入身份环境变量
identity = AgentIdentity.create(topology, "dv", "uart")        # agent_id = dv_uart
env = identity.env                                               # {A2A_PROJECT_ID, A2A_ROLE, A2A_IP}

# 登记(session 必须是非空字符串,默认会话写 "default")
registry = Registry()                                            # 默认 <状态目录>/registry.json
registry.register(identity, session="walk1", workspace_id="w1", tab_id="w1:t2", pane_id="w1:p2")

# 判定发送方:环境变量 + HERDR_PANE_ID 与注册表交叉核对,要求 running 且节点仍在拓扑里
record = resolve_sender(os.environ, registry, store.current(), session="walk1")
```

注意:程序写回拓扑会整体重写 YAML,**不保留注释**;上一版保存在同目录的 `.bak`。

## Router 用法

```python
from a2a import AuditLog, Registry, Router, SendRejected, Spool, TopologyStore

router = Router(TopologyStore(), Registry(), Spool(), AuditLog())   # 全部用默认状态目录
# 会话名默认取发送方 pane 的环境变量 HERDR_SESSION(没有则按 "default");也可显式传 session="walk1"

try:
    receipt = router.send("dv_done")          # 发送方身份只能来自环境变量(默认 os.environ)
except SendRejected as exc:
    print(exc.code, exc.reason, exc.msg_id)   # 原因代码见下表;msg_id 可在审计日志里追踪
else:
    print(receipt.msg_id, receipt.dst, receipt.text)   # 已入队;发完即走,不代表对方已收到
    print(router.status(receipt.msg_id).state)          # QUEUED(阶段 5 的 broker 投递后会变化)
```

`send` **没有**目标 IP、目标名字、自由文本这些参数:目标 = (边的 to 角色, 发送方自己的 IP),消息 = 边上的模板。
同 IP 铁律因此在接口形态上无法违反。Router 不碰 herdr,所以也不会因为目标忙或卡住而阻塞;那些由阶段 5 的 broker 处理。

| 拒绝代码 | 含义 |
| --- | --- |
| `identity` | 发送方身份不成立:缺环境变量、冒充、pane 未登记、非 running、不在拓扑里、会话不对 |
| `unknown_edge` | 拓扑里没有这条边 |
| `wrong_direction` | 这条边不允许当前角色作为发送方 |
| `target_not_in_topology` | 目标节点不在拓扑里(纵深防御) |
| `target_missing` | 目标没有登记(或登记信息与发送方不属于同一 IP / 项目) |
| `target_not_running` | 目标已登记,但不是 running |
| `bad_message` | 渲染后的消息为空、过长或含控制字符 |

每次拒绝在审计日志里恰好留下一条记录;身份未核实时,日志里的 `src` 为空,声称的身份记在 `claimed_*` 字段。

## 队列与审计的保证

- 消息状态只能沿 `messages.ALLOWED_TRANSITIONS` 前进,终态之后不可再修改;新消息只能以 `QUEUED` 入队。定义见 [`../claude/08-protocol.md`](../claude/08-protocol.md),`tests/test_protocol.py` 会对照文档与代码,不一致即失败。
- `msg_id`、目标 `agent_id` 严格校验(防路径穿越);`a2a status <msg_id>` 的参数来自 agent,不可信。
- 归档分两步(先写 `done/`,再删 `pending/`),两步之间崩溃不会重复投递:`pending()` 读取时就过滤已有终态记录的消息,**Broker 启动时再调用 `Spool.recover()` 清理残留**。
- 审计日志写到一半崩溃时,残片在下次追加前被隔离到 `audit.jsonl.corrupt`,并补记一条 `AUDIT_REPAIRED`;`read()` 容忍中间坏行,且不修改文件。
- 新增状态 `DELIVERY_UNCERTAIN`:prompt 可能已送达(`agent_prompt_stalled` 或客户端超时),**核实前不得重发**。

## 实测得到的、写代码时必须记住的行为(herdr 0.9.3)

| 行为 | 对代码的影响 |
| --- | --- |
| `fnx_*` 的进程名是 `forenyx-cli`,herdr 不认 | 不能用 `agent start`;用 `build_launch_command` 覆盖 `exec`,让 `argv[0]` 变成 `pi` |
| `report-agent` 上报后 `agent list` 能显示,但 `agent prompt` 报 `agent_not_ready` | 不用上报 |
| `done` 不会自己变回 `idle`;`agent wait --until idle` 在 `done` 时会超时 | 等"可投递"一律用 `READY_STATUSES`(`idle` + `done`) |
| 刚被识别的一瞬间状态可能是 `unknown` | "识别到了"不等于"可以投递",还要再 `agent_wait` 一次 |
| agent 退出后名字失效,重启后不会恢复 | 每次启动成功后都要重新 `agent_rename` |
| tab / pane 的编号不一定是数字(例如 `w1:tC`) | 不要假设 ID 格式 |
| `pane process-info` 必须用 `--pane`,位置参数会报未知选项 | 已在 `pane_process_info` 里处理 |
| `agent list` 本身输出 JSON,不接受 `--json` | 已处理 |
| `send-text` 再 `send-keys enter` 不是原子操作,TUI 需要约 0.5 秒间隔 | 发消息用 `agent_prompt`,不要自己拼 |
| `agent prompt` 对 `blocked` 的 agent 拒绝发送;超时或 `agent_prompt_stalled` 不代表没发出去 | 抛 `HerdrAgentBlocked` / `HerdrPromptStalled`;重试前先读状态和输出 |
| 同时发两条给忙碌的 agent,都会被接受,按两轮依次处理,但顺序不确定 | 保证顺序要靠上层的队列,不是 herdr |
| 虚拟机里 pane 默认工作目录是 Mac 的共享路径 | 创建 tab / pane 时用 `cwd=` 明确指定 |

## 还没做 / 没验证

- `blocked` 状态下 `agent prompt` 的真实行为(代码里按文档映射到 `HerdrAgentBlocked`,没有实测)。
- 另外三个智能体(Spec / 架构 / RTL)的启动脚本是否满足覆盖 `exec` 的前提(`preflight_launcher` 会在启动前检查)。
- 文本以 `-` 开头的 `agent prompt` 是否会被 herdr 当成选项。目标(pane ID / 名字)已在客户端拦截,但 prompt 文本没有处理。
- Broker、lifecycle、`a2a send` / `a2a status` 命令行还没写。目标 `blocked` 时的立即失败属于 broker(要向 herdr 查实时状态)。
- 默认会话里 pane 的 `HERDR_SESSION` 取值未测;命名会话里已实测会被注入。
- 拓扑里"声明额外模板字段并用正则白名单"的设想没有实现,第一版模板只允许 `{ip}`。
- `TopologyStore.remove_ip` / `remove_role` 不检查是否还有对应 agent 在运行,lifecycle 应先清除 agent。
