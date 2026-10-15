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

插件单元测试(`tests/test_pi_extension.py`)需要能直接加载 `.ts` 的 node(22.18 及以上),没有时自动跳过。虚拟机已在 `~/.local/lib/node-v22.20.0-linux-arm64` 安装 node 22.20.0(nodejs.org 官方包,SHA256 已校验),链接到 `~/.local/bin/node`(登录与非登录 shell 的 PATH 里都有)。装好后虚拟机上全套测试(含真实 herdr 集成)零跳过。

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

## 存储:归档与审计轮转(路线图 189)

不做无策略删除,不自动定时执行;只统计、提示,由操作员执行或自己配定时任务。

```bash
a2a status                                        # 不带 msg_id:done/、archive/、审计(含分段)、幂等记录、状态目录合计、磁盘剩余,以及提示
a2a spool archive [--older-than 30] [--dry-run]   # 到终态超过 N 天的记录打包进 spool/archive/<年-月>-<分卷>.tar.gz
a2a audit rotate [--max-size 100]                 # 当前文件超过 N MB 时改名为 audit.d/<代次>.jsonl 并压缩成 .jsonl.gz
a2a status <msg_id>                               # 已归档的消息也能查到(输出带 "archived": true)
```

- 归档不动:槽位未放行的(含队列头的 last_terminal)、被未作废裁定引用的、pending 里还留着同一条的;`--older-than` 不能小于幂等键有效期 7 天。每次新建分卷、不改旧包;核对包内每个文件与原文件一致后才删原文件;中途中断后重跑不丢不重。
- 审计轮转后,所有内部逻辑(补做裁定、resolve、ip remove 续做等)读到的仍是全部历史(分段按代次 + 当前文件,按追加顺序)。
- **分段损坏时停下**:某个 `.jsonl.gz` 解压失败、校验不符,或代次有缺口时,依赖审计历史的命令(broker 启动、`resolve`、`ruling void`、`queue`、`ip remove` 等)报"审计日志损坏"并以退出码 6 停止,不按不完整的历史行动。恢复:从备份还原 `audit.d/` 里报错的那个分段,再重试。
- 默认阈值:保留 30 天、审计 100 MB、磁盘剩余低于 10% 时提示。
- 定时执行示例(crontab,每天 3 点;按实际的状态目录与 a2a 路径修改):

  ```cron
  0 3 * * * A2A_STATE_DIR=/path/to/state PYTHONPATH=/path/to/herdr/a2a/src python3 -m a2a.cli audit rotate --max-size 100
  5 3 * * * A2A_STATE_DIR=/path/to/state PYTHONPATH=/path/to/herdr/a2a/src python3 -m a2a.cli spool archive --older-than 30
  ```

## 按 IP 增删(`a2a ip`,阶段 8)

```bash
a2a ip add gpio --session <会话> [--roles dv,sw] [--cwd /work/{ip}/{role}]   # 加入拓扑并启动各角色的 agent
a2a ip remove gpio --session <会话> [--force]                                  # 排空、清除 agent、从拓扑删除
a2a ip undrain gpio --session <会话>                                           # 放弃一次删除,恢复正常
```

- `add`:没有启动脚本的角色跳过;已 running 的跳过;已登记但停止 / 关闭的只报出(用 `a2a agent restore`);中途失败停下、不回滚,修好后再执行一次补齐。
- `remove`:先写"排空中"标记(该 IP 不再入队、不再开始新投递),等在途投递与 agent 工作结束,再检查队列。队列里还有未处理的消息时**撤销排空、恢复正常**并列出 msg_id,先用 `a2a resolve` 处理;等待超时时**保留排空**(IP 保持隔离),稍后再执行 `ip remove` 继续或 `ip undrain` 放弃。`--force` 只跳过"等 agent 空闲";删除前会检查每个将被关闭的 pane:前台只有 shell,或前台正是登记的 agent,才会关闭;否则拒绝(保留排空,等人工确认),`--force` 也不跳过这一项。任何一步中断后再执行都会从断点继续。
- 设计与竞态分析见 `herdr/claude/17-stage8-ip-add-remove-plan.md`(Router 的"检查标记 + 入队"、broker 的"检查 + 写 DISPATCHING"与写标记由 IP 排空锁互斥)。

## 会话重启后恢复 agent(`a2a agent recover`,阶段 8)

VM 或 herdr 会话重启后:先重新 attach 会话,**先恢复 agent、再启动 broker**。

```bash
a2a agent recover --session <会话> --dry-run     # 只看分类
a2a agent recover --session <会话> [--ip gpio] [--include-stopped]
```

- 只处理本会话的记录;broker 在运行时拒绝(退出码 3)。herdr 会保留 pane 与 pane ID,agent 在原 pane 里重启,布局不变。
- 分类:ok / restore / uncertain(pane 被别的进程占用、pane 不在且注册表没有工作目录等,人工处理)/ classification_failed(查询 herdr 出错,只影响这一个)/ kept_stopped / kept_closed / failed_record / skipped_draining / other_session;原因见输出的 `notes`。有失败或不确定时退出码 6。
- pane 不在时会新建 pane,工作目录用 spawn 时登记的(注册表 `cwd`);旧记录没有登记的,用 `a2a agent restore <role> <ip> --cwd <目录>` 手工恢复。
- 输出附带这些 agent 队列里 TARGET_MISSING / DELIVERY_UNCERTAIN 的队列头与建议的 `a2a resolve` 命令(不自动裁定)。
- 恢复的 fnx 是新会话(不保留对话上下文);消息与队列在 a2a 里,不受影响。

## pi 插件(`pi-extension/a2a.ts`)

在 a2a 管理的 fnx pane 里生效(由 `a2a agent spawn` 注入的 `A2A_*` 环境变量判断),普通使用 fnx 时什么都不做。

### `/a2a` 命令:手动发送,不经过模型(22 号方案)

在 agent 终端的输入框里:

| 输入 | 结果 |
| --- | --- |
| `/a2a` | 列出本角色可用的边(`边 → 接收方:「内容」`) |
| `/a2a dv_done` | 立即发送;显示 `[a2a] 已发送给 sw_uart(msg_id=…)` |
| `/a2a <不存在的边>` / 多个参数 | 红字提示并列出可用的边,**不发送** |

- 不调用模型、不耗 token;提示只显示在终端,**不进入模型上下文**(模型下一轮不知道你手动发过;对方的回复照常投递进来)。输入 `/a2a ` 后可补全边名。命令补全列表里的 `[u:git:github.com/HwJhx/A2A@a2a-plugin-v0.2.1]` 是 pi 按安装来源自动加的标签(`getAutocompleteSourceTag`;git 来源显示 主机/路径@标签,本地路径按安装级别显示 `[u]` / `[p]` / `[t]`)。没有设置项能关掉它;插件(v0.2.1 起)在 `session_start` 用 pi 支持的 `ctx.ui.addAutocompleteProvider()` 包装内置补全,**只把本插件 `/a2a` 这一项**的标签换成版本号,显示为 `a2a  [v0.2.1] 手动发送 a2a 消息…`(版本取仓库根 `package.json`);其他命令、别的扩展的同名命令、重复安装产生的 `/a2a:1`、参数补全都不改。
- 不去重:手动发送每次都真的发。提示"发送结果未知"时**不要直接重发**,先 `a2a queue <对方>` 或查审计日志核实。
- 模型正在工作时也能用(命令立即执行,不排队)。
- 状态协调:命令开始时先向 herdr 上报 working,发完再报 idle,broker 不会在命令进行中往这个 pane 投递;**上报 working 失败就不发送**(提示稍后重试)。模型、压缩与命令共用同一个判断:只有"模型确定已结束且没有命令在途"才报 idle。
- 只在 a2a 管理、且有 herdr pane 的 fnx 里注册;插件被装了两份时 pi 会生成 `/a2a:1`、`/a2a:2`(见下文防重复安装)。
- 已知限制(沿用阶段 8 状态上报):压缩(手动 `/compact` 或自动压缩)成功后若没有新的模型轮次,herdr 会一直认为该 agent 在 working,发给它的消息等到超时、告警;随便给它输入一句让模型跑完一轮,或重启该 agent 即可恢复。插件看不到 pi 交互模式压缩期间暂存的输入,无法可靠判断压缩之后是否真正收尾,所以宁可保持 working。

**拓扑模板不能以 `/` 或 `!` 开头**(去掉开头空白后,包括 BOM、全角空格等):消息原样打进对方的 pi,会被当成命令(`/quit`、`/a2a …`)或 bash(`!…`)执行。`a2a topology` 的新建、`add-edge`、`set-template` 拒绝这类模板;已有拓扑里若有(旧文件或手工编辑),照常加载,但发送时 Router 拒绝(`unsafe_template`,入队前,消息确定没发),用 `set-template` 修正即可。

边界:检查只作用于**新入队**的消息;升级到带此检查的版本之前就已在队列里的消息,broker 投递时不会再检查。沿用旧的状态目录前,先用 `a2a queue <对方>` 看待投递消息的内容。若有以 `/` 或 `!` 开头的:正在排队的消息不能直接裁定(`a2a resolve` 只处理队列头上结果不确定或已终态的消息),先 `a2a agent stop` 停掉接收方,让这条消息投递失败(`TARGET_MISSING`,终态),再 `a2a resolve <msg_id> abandon --reason …` 放弃,然后 `a2a agent restore` 恢复接收方。

有两种安装方式,**同一个 fnx 只能用其中一种**(两种都装会加载两份插件:`a2a_send` 注册两次,working / idle 也会上报两次)。防护:`a2a plugin status` 同时读 `<agent 目录>/settings.json` 的 `packages`,指向本仓库的在线包(各种 git 写法,或解析到本仓库的本地路径)报为 `online_package`,两种都装时给出 `duplicate`;已有在线包时 `a2a plugin install` 拒绝;settings 读不了时 install 也拒绝(fail-closed)。**反方向挡不住**:先装了软链接、再用 `fnx_dv install` 在线安装,pi 的命令不经过 a2a,只能靠 `a2a plugin status` 发现,请先 `a2a plugin uninstall`。

pi 还会加载**项目级**配置:fnx 启动目录下的 `.forenyx/`(fnx 的 configDir;`settings.json` 的 `packages`、`git/` 与 `npm/` 里的安装副本、`extensions/` 里的扩展,装法是 `fnx_dv install -l …`)。软链接装在 agent 级,对所有项目生效,a2a 无法知道有哪些项目,所以**不带参数时只保证 agent 级**(`status` 输出 `project_scope` 提示);用 `--project-dir <项目目录>`(可重复)让 `status` / `install` 一并检查这些项目:项目级已有本插件时 `install` 拒绝,`status` 列出 `project_packages`,与 agent 级同时存在时报 `duplicate`。建议:不要在项目级安装本插件。

检查是**宁多勿少**的,可能误报、保守地拒绝安装(不会因此重复加载):`extensions/` 里的入口按 pi 两套发现规则的并集算(fnx 实际用的包管理器规则,加上 SDK loader 的规则;点开头的文件、fnx 忽略规则排除的文件也算);在线安装副本按 `package.json` 的 name(`a2a-pi-plugin`)识别,无关的同名包也会被当成本插件;`git/` 下只扫到 4 层,更深的 git 命名空间镜像(如多级子组)扫不到。误报时请人工确认后处理。

**正式部署:在线安装(pi 包)**。仓库根目录的 `package.json` 是 pi 包清单(`"pi": {"extensions": ["./herdr/a2a/pi-extension/a2a.ts"]}`)。每个 fnx 有自己的 agent 目录,要**分别安装**:

```bash
fnx_dv install git:github.com/HwJhx/A2A@a2a-plugin-v0.2.1
fnx_sw install git:github.com/HwJhx/A2A@a2a-plugin-v0.2.1
fnx_dv list                                             # 查看已安装的包
fnx_dv install git:github.com/HwJhx/A2A@<新标签>        # 升级:替换 settings 里原来那一条(实测 v0.1.0 → v0.2.0),不会变成两条
fnx_dv remove  git:github.com/HwJhx/A2A                 # 卸载(带不带 @标签 都行,会删掉克隆目录)
```

- pi 把仓库克隆到 `<agent 目录>/git/github.com/HwJhx/A2A`,并在 `<agent 目录>/settings.json` 的 `packages` 里记一条;按标签固定版本,`fnx_dv update --extensions` 不会把它挪到新标签。
- git 标签是可以被移动的;要严格可复现,可改用提交号固定版本(`fnx_dv install git:github.com/HwJhx/A2A@<commit SHA>`),或在 GitHub 上保护 release 标签。
- 只装插件(TypeScript)这一半。插件发送时调用的 Python 后端(`a2a send`)的位置来自 pane 的环境变量(`A2A_SRC`、`A2A_PYTHON`,由 `a2a agent spawn` 注入),不随插件安装;**插件与后端的版本要对应**。
- 插件只用 Node 自带模块,没有 npm 依赖;但 pi 克隆后总会在仓库里跑一次 `npm install`,**PATH 里必须有 npm**,否则报 `Executable not found in $PATH: "npm"`、不记入 settings。VM 上 npm 不在登录 shell 的 PATH 里,安装时临时加上:`PATH=$HOME/.local/lib/node-v22.20.0-linux-arm64/bin:$PATH fnx_dv install …`(只有 install / update 需要,运行 fnx 不需要)。
- 推送前可用本地路径验证清单:`fnx_dv install /path/to/A2A`(不复制,只在 settings 里记路径),验证完 `fnx_dv remove /path/to/A2A`。2026-10-14 在 VM 上这样验证过:fnx_dv 启动画面 `[Extensions]` 里显示 `a2a.ts`。

**开发与自动测试:软链接**(链到仓库里的同一个文件,修改插件后所有智能体下次启动即生效;实测 fnx 能加载软链接形式的扩展,启动画面 `[Extensions]` 里显示为 `a2a.ts`):

```bash
a2a plugin install   --agent-dir ~/.forenyx/fnx_dv   # 建 agent/extensions/a2a.ts -> pi-extension/a2a.ts
a2a plugin status    --agent-dir ~/.forenyx/fnx_dv   # not_installed / linked / linked_elsewhere / regular_file,及同名工具冲突
a2a plugin uninstall --agent-dir ~/.forenyx/fnx_dv   # 只删指向本仓库的软链接;extensions/ 是 install 建的且已空才删
```

- `extensions/a2a.ts` 已存在但不是指向本仓库的软链接(普通文件、指向别处)时拒绝,不覆盖也不删除。
- `extensions/` 里已有别的扩展注册 `a2a_send`(按 `name: "a2a_send"` 文本匹配,尽力而为)时拒绝安装。
- 只往 fnx 的 agent 目录加文件,不改 fnx 的程序、启动脚本、libexec。集成测试与压测脚本也用这个命令,原本已安装的保持不动。
- 插件是在 fnx 启动时加载的:修改插件后,已在运行的 agent 要 restore 才会用上新版本。
- 发送成功后,工具结果告诉模型"不需要再说明或总结,直接结束这一轮"(路线图 327);pi 仍会再调一次模型,只是输出很短。
- fnx 的 bash 工具不需要人工批准(探测 8 实测):开放 bash 时不会卡在审批上,也没有人工把关。
- 防重复(路线图 336):插件每次开始处理新输入时生成一个幂等键,`a2a_send` 带着它调用 `a2a send --idempotency-key`。同一次处理里沿同一条边重复调用(模型自己重试、pi 遇到 429 自动重试)只入队一次,第二次返回第一次的 msg_id,并告诉模型不要再发。处理新输入时是新的键。已知限制:agent 忙时人工插入的输入与当前输入共用一个键。
- 可用的边每轮刷新(路线图 338):pi 每次开始处理新输入前,插件重新读取本角色的出边;有变化就更新 `a2a_send` 的可选边与说明,出边变空时停用 `a2a_send`,读取失败沿用上一次的。所以 `a2a topology add-edge / remove-edge` 之后不用重启 agent,下一轮就生效(本轮已开始处理的不受影响;Router 始终按当前拓扑鉴权)。

- `a2a_send(edge_id)`:调用 `a2a send`,鉴权、模板、目标都由 Router 决定。结果分三类:成功、明确拒绝(未入队)、结果未知(可能已入队,提示不要重试)。
- 拦截 bash 里直接调用 herdr 写入类命令(软限制)。**已知局限**:按正则匹配命令文本,可能误拦提到这些命令的文档 / 搜索命令,也能被变量、别名、脚本文件绕过;它防的是模型误用,不是安全边界。真正的隔离需要让 agent 用别的 OS 用户运行(L4),目前不做。
- 状态上报(阶段 8):pi 开始处理时上报 working;只在能确定 pi 已结束时上报 idle(`aborted`,或 `stop` 且上下文 token 低于压缩阈值的 90%;阈值按实际的 `compaction.reserveTokens` 设置计算),其他情况保持 working。设计与依据见 `herdr/claude/16-stage8-report-state-plan.md`。
- **部署约束**:a2a 管理的 fnx 只加载本插件,不加载其他会在 `agent_end` 里排消息的扩展。
- **卡在 working 时的恢复**:状态上报失败、模型出错后不再重试、或上下文接近压缩阈值时,目标会一直显示 working,broker 等待超时后暂停该队列并告警。恢复:重启这个 agent(`a2a agent stop <role> <ip>` 再 `a2a agent restore <role> <ip>`),再用 `a2a resolve` 处理被暂停的消息。不要手工 `report-agent` 补报(序号会与插件的计数脱节)。

## 实测得到的、写代码时必须记住的行为(herdr 0.9.3)

| 行为 | 对代码的影响 |
| --- | --- |
| `fnx_*` 的进程名是 `forenyx-cli`,herdr 不认 | 不能用 `agent start`;用 `build_launch_command` 覆盖 `exec`,让 `argv[0]` 变成 `pi` |
| 进程名不是 `pi` 时(早期直接运行 `forenyx-cli`),`report-agent` 上报后 `agent prompt` 报 `agent_not_ready` | 先用覆盖 `exec` 让进程名是 `pi`;此后上报可用(见下一行) |
| herdr 判断 pi 是否 working 只看屏幕(规则文件 `pi.toml`:出现 `Working...`);pane 太小(只剩 2–4 行)时看不到,投递会因 `agent_prompt_stalled` 判为不确定 | a2a 插件主动 `report-agent` 上报 working / idle;进程名为 `pi` 时上报的状态覆盖屏幕判断,进程退出后不残留,同 pane 重启后序号可从 1 重来(阶段 8 实测) |
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
| agent(进程组组长)退出后,pane 的 shell 收回终端,它启动的子进程变成后台进程,pane 看起来只剩 shell | `agent stop` 在 Linux 上还核对原进程组已没有进程,否则报错、状态保持 running;人工结束残留进程后再 stop。没有登记进程组的旧记录、或读不了 /proc(权限不足、hidepid 挂载)时,同样不登记 stopped(fail-closed),人工确认后用 `agent close` 或 `agent restore`。spawn / restore 启动失败(含 Ctrl-C 中断)会关掉该 pane,restore 的记录标为 failed,之后 restore 新建 pane。stop 用 pidfd 锁定组长再发信号;pidfd 发信号成功与 killpg 之间组被复用的极窄窗口仍存在(已知局限) |
| 会话 `stop` 后重新 attach:pane 与 pane ID 保留,进程退出,agent 名字丢失;**pane 的新 shell 不再带创建时注入的环境变量** | 启动命令自带身份变量(`Lifecycle._launch`);会话重启后用 `a2a agent recover` |

## 还没做 / 没验证

- `blocked` 状态下 `agent prompt` 的真实行为(代码里按文档映射到 `HerdrAgentBlocked`,没有实测)。
- 另外三个智能体(Spec / 架构 / RTL)的启动脚本是否满足覆盖 `exec` 的前提(`preflight_launcher` 会在启动前检查)。
- 文本以 `-` 开头的 `agent prompt` 是否会被 herdr 当成选项。目标(pane ID / 名字)已在客户端拦截,但 prompt 文本没有处理。
- Broker、lifecycle、`a2a send` / `a2a status` 命令行还没写。目标 `blocked` 时的立即失败属于 broker(要向 herdr 查实时状态)。
- 默认会话里 pane 的 `HERDR_SESSION` 取值未测;命名会话里已实测会被注入。
- 拓扑里"声明额外模板字段并用正则白名单"的设想没有实现,第一版模板只允许 `{ip}`。
- `TopologyStore.remove_ip` / `remove_role` 不检查是否还有对应 agent 在运行,lifecycle 应先清除 agent。
