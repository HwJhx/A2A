# 设计草案:基于 herdr 的"角色 × IP"多智能体 Python 框架

> 承接 `03-requirements-restated.md`(含 §11、§12 你的答复)。
>
> **v2 更新(已按你的第二轮答复修订)**:
> - 拓扑改为**有向边**,并成为"谁能给谁发"的唯一入口(§3)
> - 新增**消息模板**,与拓扑同属配置;发送方不能写自由文本(§3)
> - 投递条件收紧为**必须目标 idle**,发完即走(§4)
> - agent **只通过框架 API/CLI 发送**,先鉴权再执行(§5)
> - 布局暂不设计、框架先放虚拟机内、角色名字已更正(§7、§8、§10)
> - 其余未提及的部分保持 v1 原样
>
> **v3 更新(合入 codex 方案中值得借鉴的部分,并按你的三项决定修订)**:
> - 目标卡在审批(`blocked`)时:**立即返回失败**,不排队等待(§4.3、§4.4)
> - `done` 与 `idle` 统一视为 `READY`(可投递),`done` 的行为已在虚拟机实测(§4.4、§10.3)
> - 业务 ID **不带项目前缀**,`agent_id` = `{角色码}_{ip}`;herdr 名字在 agent 被识别后用 `agent rename` 设置,重启后需重设(§2.1,已实测)
> - 新增:业务身份与注册表(§2.1)、Python 包结构(§2.2)、消息状态机与日志(§4.6)、删除三档(§5)、反向验证用例(§9)、socket 安全(§8)
> - 投递改由**单一 broker 进程**执行(因为"发完即走"需要后台投递者)(§4.3)
> **v4 更新(虚拟机实测,启动方式已定)**:
> - 新增 §2.3:herdr 不能自动识别 `fnx_*`(进程名是 `forenyx-cli`);`report-agent` 上报后 `agent prompt` 仍失败;
>   采用**覆盖 `exec` 让 `argv[0]` 变成 `pi`** 的通用启动方式,已在 `fnx_dv`、`fnx_sw` 上验证
> - 拓扑配置里每个智能体只记录**启动脚本路径**(§3);Provisioner 的启动与预检按 §2.3、§5 修订
> - `done` 不会自己变回 `idle`、`agent wait --until idle` 在 `done` 时会超时等实测结论写入 §4.4、§10.3
>
> **v7 更新(复核后修复 P1~P7,状态迁移表写入协议文档)**:
> - 消息状态、合法迁移表、`DELIVERY_UNCERTAIN`、崩溃恢复规则、拒绝代码的**权威定义在 `08-protocol.md`**;`tests/test_protocol.py` 逐行对照其中的迁移表与代码
> - 队列:状态只能沿迁移表前进,终态不可变;所有 ID 严格校验;`recover()` + `pending()` 过滤,归档两步之间崩溃不会重复投递
> - 审计:崩溃留下的半行在下次追加前被隔离到 `.corrupt`,不再吞掉下一条记录
> - Router:目标登记的会话必须与发送方会话一致
>
> **v6 更新(Router 已实现,代码在 `herdr/a2a/`)**:
> - `router.py`:发送方鉴权 + 拓扑校验 + 固定模板渲染 + 入队;不碰 herdr(§3.1)
> - 新增 `messages.py`(消息模型与状态常量)、`spool.py`(持久队列)、`audit.py`(审计日志)
> - 接口里没有"目标 IP / 目标名字 / 自由文本"参数:目标 = (边的 to 角色, 发送方自己的 IP),消息 = 边上的模板
> - 发送方的会话名取自 pane 环境变量 `HERDR_SESSION`(命名会话里已实测会被 herdr 注入,D16)
> - 拒绝原因有固定代码,每次拒绝在审计日志里恰好留下一条记录
>
> **v5 更新(拓扑、身份、注册表已实现,代码在 `herdr/a2a/`)**:
> - 保留 `project_id`(D13):随环境变量 `A2A_PROJECT_ID` 注入,不进入 `agent_id`(仍是 `{角色码}_{ip}`)
> - 动态拓扑现在就实现(D14):`TopologyStore` 支持热加载和增删 IP / 角色 / 边 / 模板,跨进程文件锁 + 原子写回
> - 状态目录解析(D15):`A2A_STATE_DIR` > `$XDG_STATE_HOME/a2a` > `~/.local/state/a2a`
> - 发送方判定收成一个入口 `identity.resolve_sender`(§3.1)
> - 拓扑 schema 以实现为准(§3):`version: 1`、`project_id`、`workspace_label`、角色用 `label`
> - 拓扑与注册表代码移植自 `herdr/a2a_codex`(该目录未被改动)
>
> 本文是**设计草案**,实现部分见代码与 `herdr/a2a/README.md`。部分结论已在虚拟机实测。每个结论标注依据等级:
> - **[实测]**:我在虚拟机(herdr 0.9.3)里实际运行得到的结果
> - **[本机验证]**:我在本机运行 `herdr <子命令>` 看到的用法输出(只读,未创建任何对象)
> - **[文档]**:官方文档或视频转写所述,尚未实测
> - **[推断]**:我的推理,必须实测

## 1. 先回答你的 4 个担心点

### 担心 1:能否提前拉起所有 agent?有没有命令/API?

**能,有命令,也有 socket API。** [本机验证]

| 目的 | 命令 |
| --- | --- |
| 建 workspace | `herdr workspace create [--cwd PATH] [--label TEXT] [--env K=V] [--focus\|--no-focus]` |
| 建 tab | `herdr tab create [--workspace ID] [--cwd PATH] [--label TEXT] [--env K=V] [--no-focus]` |
| 拆 pane | `herdr pane split [PANE] --direction right\|down [--cwd PATH] [--env K=V] [--no-focus]` |
| 在 pane 里启动 agent | 对 `fnx_*` **不能用** `agent start`(见 §2.3);用 `pane run` 写入覆盖 `exec` 的启动命令。其他本来就叫 `pi` 的 agent 仍可用 `herdr agent start <名字> --kind pi --pane <pane-id>` |
| 关闭 | `herdr pane close`、`herdr tab close`、`herdr workspace close` |

`--kind` 列表里有 `pi`,即 herdr 原生识别 pi 系 agent。[本机验证]
你的二开版 `fnx_dv`、`fnx_sw` **不会自动识别**,需按 §2.3 的方式启动,已实测。

**一个会咬人的现实问题**:你要在**一个 tab 里放 30 个 pane**。终端窗口大小有限,30 次拆分后每个 pane 极小,拆分可能被拒绝,界面也基本不可用。[推断]
这不是 API 问题,是布局问题。备选见 §7。

> **v4 补充**:`--kind` 列表里有 `pi`,但那是让 herdr 去执行名为 `pi` 的命令。`fnx_dv`、`fnx_sw` 的进程名是 `forenyx-cli`,
> herdr 不会自动识别,需要按 §2.3 的方式启动。已在虚拟机实测。

### 担心 2:tab / pane 能否命名为 IP 名,以便寻址?

**能命名,而且命名后可修改。** [本机验证]

| 对象 | 创建时命名 | 事后改名 |
| --- | --- | --- |
| workspace | `workspace create --label` | `workspace rename <id> <label>` |
| tab | `tab create --label` | `tab rename <id> <label>` |
| pane | (拆分时无 label 参数) | `pane rename <pane_id> <label>`,`--clear` 清除 |
| agent | 对 `fnx_*` 不能用 `agent start` 起名(见 §2.3);识别成功后用 `agent rename` 设置 | `agent rename <target> <名字>`,`--clear` 清除 |

**寻址用的是 agent 名字,不是 tab/pane 的 label。**
`agent prompt/read/wait/get` 的 target 接受"唯一 agent 名字"或"承载 agent 的 pane id"。[本机验证]
所以 tab/pane 的 label 主要给人看;给机器寻址的是 **agent 名字**。

agent 名字规则:小写字母开头,正则 `[a-z][a-z0-9_-]{0,31}`,存活 agent 中唯一。[文档]
所以**不能**多个角色都叫 `uart`。建议命名:

```
{角色码}_{ip}     例:dv_uart   sw_uart   rtl_uart   spec_uart   arch_uart
```

注意总长度 ≤ 32 字符,IP 名超长时需要缩写表。

### 担心 3:agent 能否自己调用 herdr 命令向同 IP 另一个 agent 发消息?

**可以。** 机制就是 `herdr agent prompt <名字> "<文本>"`。[本机验证命令存在]
agent 在自己的 pane 里执行 shell 命令即可,pane 里会有 `HERDR_ENV=1` 等环境变量。[文档]
前提是 pi 能执行 shell 命令、并且被允许执行(权限审批)。

**跨 tab 没有限制**:命令只认名字/pane id,不关心 tab。[文档,待实测]
当前你的会话只有一个 tab,所以还没实测过跨 tab。

### 担心 4:agent 怎么知道"我是验证 tab 的 UART"?

**不要靠 tab/pane 的显示名。** 它们会被改名,而且 pane 的位置 ID(`w1:p3`)是不透明的。[文档]

可靠做法:**创建 pane 时用 `--env` 注入业务身份**。[本机验证 `--env` 参数存在]

```bash
herdr pane split ... --env A2A_ROLE=dv --env A2A_IP=uart ...
```

之后 pane 里启动的 agent 继承环境变量,框架从 `A2A_ROLE` / `A2A_IP` 读自己的身份。
同时 herdr 自动注入 `HERDR_WORKSPACE_ID`、`HERDR_TAB_ID`、`HERDR_PANE_ID`。[文档]

两层身份:

| 层 | 内容 | 稳定性 |
| --- | --- | --- |
| 业务身份 | `A2A_ROLE`、`A2A_IP`(我们注入) | 稳定,不随改名变化 |
| herdr 位置 | `HERDR_*_ID` | 稳定、不复用,但没有业务含义 |

- 环境变量继承 [实测]:`tab create --env A2A_ROLE=.. --env A2A_IP=..` 注入后,pane 里的 shell、以及按 §2.3 启动的 `fnx_dv` / `fnx_sw`(用其 `!` 命令读取)都能读到,`HERDR_PANE_ID` / `HERDR_TAB_ID` / `HERDR_WORKSPACE_ID` / `HERDR_ENV=1` 也在。
- `pane report-metadata --token NAME=VALUE` 看起来也能给 pane 挂键值对。[本机验证命令存在,语义未验证]

## 2. 设计总览

```
        ┌───────────────────────────────────────────────┐
        │  a2a_topology.yaml  (节点 + 允许的跳转, 单一事实源)  │
        └───────────────┬───────────────────────────────┘
                        │ 加载 / 校验
        ┌───────────────▼───────────────┐
        │        Python 框架 a2a         │
        │  Registry  Router  Provisioner │
        │  Delivery(队列+等待+投递)       │
        └───────┬──────────────┬────────┘
                │ herdr CLI    │ (可选) herdr socket
        ┌───────▼──────────────▼────────┐
        │  herdr server (虚拟机内)        │
        │  workspace → 5 tab → N pane    │
        └────────────────────────────────┘
```

四个模块:

| 模块 | 职责 |
| --- | --- |
| Registry | 记录有哪些节点(角色 × IP),以及稳定业务 ID → 当前 herdr 位置的映射 |
| Provisioner | 创建/停止/关闭/清除 workspace、tab、pane,启动 agent,注入身份 |
| Router | 读拓扑,鉴权、渲染模板,校验"谁能给谁发"(拦截无效跳转) |
| Broker(投递) | 单一后台进程:消息入队、等目标 READY、投递、审计、去重 |

### 2.1 业务身份与注册表(借鉴 codex)

herdr 的位置 ID(`w1:p27`)只表示"我现在在哪",会因移动、重建、恢复而变化;
业务身份表示"我是谁、负责什么",必须稳定。两者分开保存。

业务身份(你已决定**不带项目前缀**):

```text
project_id = soc_a      # 保留(D13),随环境变量 A2A_PROJECT_ID 注入;不进入 agent_id
role     = dv
ip       = uart
agent_id = dv_uart        # 业务 ID,框架里的唯一身份。herdr 名字默认与它相同,但需启动后 agent rename 设置(见下)
```

注册表记录(运行时状态,存盘持久化):

```json
{
  "format_version": 1,
  "updated_at": "2026-10-09T10:00:00Z",
  "agents": {
    "dv_uart": {
      "project_id": "soc_a",
      "ip_id": "uart",
      "role": "dv",
      "agent_id": "dv_uart",
      "session": "walk1",
      "workspace_id": "w1",
      "tab_id": "w1:t4",
      "pane_id": "w1:p27",
      "agent_name": "dv_uart",
      "status": "idle",
      "lifecycle": "running",
      "registered_at": "2026-10-09T09:00:00Z",
      "updated_at": "2026-10-09T10:00:00Z"
    }
  }
}
```

规则:

1. 通信永远按 `agent_id` 查注册表,取出当前 `pane_id`/`agent_name` 再调 herdr。agent 自己不接触 pane id。
   `session` 必须是非空字符串(默认会话写 `"default"`):pane 编号在不同会话里会重复。
   `status` 只是上次观察到的快照,会过期;投递前必须向 herdr 查实时状态。
2. pane 被移动、重建或 herdr 恢复后,**只更新注册表**,`agent_id` 不变。
3. 显示名(workspace/tab/pane 的 label)可随意改名,不影响 `agent_id`。
4. 以后若出现多项目共用一个 herdr session,再引入项目前缀;当前不需要。
5. 名字长度上限 32 字符,`{角色码}_{ip}` 过长时要用缩写表,且缩写必须唯一。

#### 2.1.1 herdr agent 名字(实测,虚拟机 herdr 0.9.3)

三个"ID"的分工:

| 名称 | 例子 | 角色 |
| --- | --- | --- |
| 业务 ID | `dv_uart` | **框架里的唯一身份**:注册表的键,拓扑、鉴权、审计日志都用它 |
| herdr agent 名字 | `dv_uart` | 业务 ID 在 herdr 里的别名,可选,用 `agent rename` 设置,便于日志和排查 |
| herdr pane ID | `w1:p2` | herdr 的寻址句柄,稳定到 pane 被移动或关闭 |

`agent rename` 的实测结果:

| 测试 | 结果 |
| --- | --- |
| 改名 | 成功,`agent list` 里出现名字 |
| 按名字 `agent get` / `prompt` / `wait` / `read` | 都成功 |
| 重名 | 被拒绝:`agent_name_taken` |
| 非法名(大写、带点) | 被拒绝:`invalid_agent_name`,须小写字母开头,只含小写字母、数字、`-`、`_`,1 到 32 字符 |
| agent 退出后 | 名字失效,`agent get` 返回 `agent_not_found` |
| agent 重启后 | **名字不会自动恢复**,需要重新 `agent rename` |

由此得到的规则:

- **改名是启动流程的最后一步**:建 pane → 覆盖 `exec` 启动 → 等 herdr 识别 → `agent rename`。识别之前不能改名。
- **每次重启都要重新改名**。Provisioner 的 `restore` 也要包含这一步。
- **注册表同时存 pane ID 和名字**,二者互为备份;**实际调用以 pane ID 为准**(已大量验证),名字作为方便排查的别名。
- 名字重名会被拒绝,所以每个 `{角色码}_{ip}` 在一个 herdr session 内只能存活一个。

### 2.2 Python 包结构(借鉴 codex)

```text
a2a/                              # 实际位置:herdr/a2a/
├── pyproject.toml                # 依赖:PyYAML>=5.4
├── config/topology.example.yaml
├── src/a2a/
│   ├── errors.py         # herdr 调用错误(按 error.code 映射)             [已实现]
│   ├── herdr_client.py   # 薄封装 herdr CLI(subprocess + 解析 JSON)        [已实现]
│   ├── launcher.py       # 覆盖 exec 的启动命令 + 启动脚本预检(§2.3)       [已实现]
│   ├── paths.py          # 状态目录与默认文件路径                            [已实现]
│   ├── _fsutil.py        # 跨进程文件锁 + 原子写入                           [已实现]
│   ├── topology.py       # Topology(不可变快照)+ TopologyStore(热加载/动态修改) [已实现]
│   ├── identity.py       # AgentIdentity + resolve_sender(发送方判定唯一入口)  [已实现]
│   ├── registry.py       # 业务 ID <-> 当前 herdr 位置                       [已实现]
│   ├── messages.py       # 消息模型、状态常量、按时间排序的 msg_id           [已实现]
│   ├── spool.py          # 持久队列:pending/<目标>/ 与 done/,原子写入        [已实现]
│   ├── audit.py          # 审计日志:追加写 JSON Lines,跨进程文件锁          [已实现]
│   ├── router.py         # 鉴权 + 拓扑校验 + 模板渲染 + 入队(§3.1)           [已实现]
│   ├── broker.py         # 投递:队列、等待 READY、投递、重试、审计(§4)       [阶段 5]
│   ├── lifecycle.py      # 创建/停止/关闭/清除/恢复(§5)                      [待做]
│   └── cli.py            # 给 agent 用的 `a2a send <edge_id>`                [待做]
└── tests/                # 161 个测试(含虚拟机集成测试)
```

底层先用 `subprocess` 调 herdr CLI;长时间等待与事件订阅留到后期再用 socket(已决定,见 D12)。
规模化时的动机:broker 每个目标一个 worker、各自用 `agent wait` 阻塞,每次 wait 是一个子进程,150 个目标就是 150 个进程;socket 的 `events.subscribe` 可以用一条长连接收所有状态变化。socket API 目前只读过文档,没有实测。

### 2.3 智能体的识别与启动(v4 新增:实测结论,替代此前"用 `agent start` 启动"的假设)

#### 2.3.1 问题

herdr 靠**前台进程的 `argv[0]`** 判断一个 pane 里跑的是哪种 agent。
`fnx_dv`、`fnx_sw` 的启动脚本最后一行是 `exec "$LIBEXEC_DIR/forenyx-cli" "$@"`,
前台进程的 `argv[0]` 是 `.../libexec/forenyx-cli`,不是 `pi`,所以 herdr **不会自动识别**。
识别不了,`agent list` 为空,`agent prompt` / `agent wait` 等 agent 层命令也都不可用。

另外,`agent start --kind pi` 会去执行名为 `pi` 的命令。虚拟机里没有这个命令,所以也不能用它启动 fnx。

#### 2.3.2 三种办法的实测结论 [实测,虚拟机 herdr 0.9.3]

| 办法 | 结果 |
| --- | --- |
| A. `pane report-agent` 手动上报 | `agent list` / `agent get` 会显示 `pi / idle`,**但 `agent prompt` 失败**:`agent_not_ready: agent w1:p2 is no longer the pane foreground process`,且消息没有写入。(先上报后启动、启动后上报、释放后换来源重报,三种顺序结果一致。用户亦独立复现了该报错。)**不可用** |
| B. 符号链接 | 建名为 `pi` 的符号链接指向 `forenyx-cli` 再启动,识别与 `agent prompt` 都成功。**缺点**:每个智能体的程序、数据目录、环境变量都不同(`fnx_dv` 与 `fnx_sw` 的程序校验值不同),要为每个智能体各建一个链接,并重复启动脚本里的环境变量逻辑 |
| C. **覆盖 `exec`(采用)** | 不建链接、不改 fnx 安装包、不重复环境变量逻辑,见下 |

herdr 按 `argv[0]` 识别的依据:`bash -c 'exec -a pi sleep 600'` 之后,进程文件名仍是 `sleep`,
`argv` 是 `['pi','600']`,herdr 立即识别为 `pi / idle`。[实测]

#### 2.3.3 采用的启动方式(办法 C)

拓扑配置里,每个智能体只记录**它自己的启动脚本路径**(5 个智能体安装目录各不相同,也没有关系):

```yaml
roles:
  dv: {tab_label: "验证智能体", kind: pi, launcher: /home/jhx/.forenyx/fnx_dv/bin/fnx_dv}
  sw: {tab_label: "软件智能体", kind: pi, launcher: /home/jhx/.forenyx/fnx_sw/bin/fnx_sw}
  # spec / arch / rtl 同理,各填自己的启动脚本绝对路径
```

框架向目标 pane 写入的命令:

```bash
bash -c 'exec(){ builtin exec -a pi "$@"; }; source "<launcher 绝对路径>"'
```

原理:`bash -c` 起一个子 shell,先定义一个叫 `exec` 的函数覆盖内置 `exec`,
再 `source` fnx 自己的启动脚本。脚本照常运行:设置它自己的 `FORENYX_*` / `PI_*` 环境变量、授权检查、离线逻辑等。
走到最后一行 `exec forenyx-cli "$@"` 时,调用的是我们的函数,它实际执行 `builtin exec -a pi forenyx-cli ...`,
于是最终进程的 `argv[0]` 变成了 `pi`。

向 fnx 传命令行参数:

```bash
bash -c 'exec(){ builtin exec -a pi "$@"; }; source "<launcher>" "$@"' _ <参数1> <参数2>
```

通过 herdr 写入 pane:`herdr pane run <pane-id> '<上面的整条命令>'`(框架用 `shlex.quote` 处理路径,并使用绝对路径)。
创建 pane 时注入的 `A2A_ROLE` / `A2A_IP` 由 pane 里的 shell 继承,fnx 子进程可读到。

#### 2.3.4 实测结果 [实测]

| 项 | `fnx_dv` | `fnx_sw` |
| --- | --- | --- |
| 启动横幅 | `fnx_dv v0.4.7` | `fnx_sw v0.0.8` |
| 前台进程 `argv` | `['pi']` | `['pi']` |
| herdr 识别 | 自动识别为 `pi / idle`,无需上报 | 同左 |
| `agent prompt` | 成功,状态 `working → done` | 成功,状态 `working → done` |
| 身份变量 | `dv uart w1:p2` | `sw uart w1:p3` |

横幅分别是各自身份,说明两者各用各的数据目录与环境变量,没有串。

#### 2.3.5 依赖与预检(框架必须做)

此办法依赖两个前提:
1. 启动脚本里**只有一处 `exec`**(因为 `exec` 函数会覆盖脚本内所有 `exec` 调用),且不使用 `$0` / `BASH_SOURCE`。
   `fnx_dv`、`fnx_sw` 的脚本都只有第 227 行一处 `exec "$LIBEXEC_DIR/forenyx-cli" "$@"`。
2. herdr 继续按 `argv[0]` 识别。

**另外三个智能体(Spec / 架构 / RTL)不在这台虚拟机上,第 1 条是否成立未验证。**
所以 Provisioner 启动任何智能体前后都要检查:

| 时机 | 检查 | 失败处理 |
| --- | --- | --- |
| 启动前 | 脚本存在且可读;`exec` 语句只有一处;不含 `$0` / `BASH_SOURCE` | 拒绝启动,报告原因 |
| 启动后(等待数秒) | `pane process-info` 的 `argv[0]` 是 `pi`;`agent list` 里该 pane 被识别为 `pi` | 标记节点 `LAUNCH_FAILED`,不进入注册表的 `running` 状态 |
| 启动后 | 向 pane 内 `!` 命令读取 `A2A_ROLE` / `A2A_IP` 与注册表一致(可选) | 标记异常 |

升级 fnx 后启动脚本可能变化,预检能提前发现。

#### 2.3.6 已知局限

- 子 shell 里用函数覆盖内置 `exec` 是一个绕过手法,依赖 bash 非 POSIX 模式。fnx 启动脚本需继续使用 `#!/bin/bash`。
- 本办法只解决"让 herdr 认出来"。授权检查、skills 加载等深层功能,我只验证了启动和一句对话。
- 此办法下 `agent start` 不再用于启动 fnx;框架自己通过 `pane run` 启动,并自行完成预检和启动后核对。

## 3. 拓扑与模板数据结构(全局唯一入口)

> v2:边是**有向**的;消息内容由**模板**生成;二者同属一份配置,是"谁能给谁发什么"的**唯一事实源**。

```yaml
version: 1
project_id: soc_a            # 保留(D13);随 A2A_PROJECT_ID 注入;不进入 agent_id
workspace_label: SoC-A

roles:                       # 角色 = tab;role ID 同时用于 A2A_ROLE 和 agent_id 前缀
  spec: {label: "Spec智能体", kind: pi, launcher: null}
  arch: {label: "架构智能体", kind: pi, launcher: null}
  rtl:  {label: "RTL智能体",  kind: pi, launcher: null}
  dv:   {label: "验证智能体", kind: pi, launcher: /home/jhx/.forenyx/fnx_dv/bin/fnx_dv}   # 本地 pi-custom / VerifAgent
  sw:   {label: "软件智能体", kind: pi, launcher: /home/jhx/.forenyx/fnx_sw/bin/fnx_sw}   # 本地 fnx-sw

ips: [uart, gpio, spi, i2c]  # 可以为空,运行中用 TopologyStore.add_ip 动态添加

edges:                       # 有向边:from -> to;反方向必须另写一条
  - id: dv_done
    from: dv
    to: sw
    template: "{ip}已完成 UVM 验证，请开始驱动程序和 HAL 框架开发。"
  - id: dv_to_rtl_fix        # 回退示例(句式为示例,请替换为你的真实文案)
    from: dv
    to: rtl
    template: "{ip}验证发现 RTL 问题，请检查并修复。"
```

规则:

1. **有向**:`dv→sw` 不蕴含 `sw→dv`。要双向就写两条。
2. **唯一入口**:没有写进 `edges` 的跳转,一律不存在。框架里没有任何旁路。
3. **同 IP 是硬编码铁律**:发送方与接收方 IP 必须相同。`edges` 只决定"同一 IP 内哪些角色之间能通",不能被配置放开为跨 IP。
4. **模板是字符串 + 占位符**。默认只有 `{ip}` 一个占位符,由框架从发送方身份填入,**发送方不能传入**。
5. **第一版模板只允许 `{ip}`**,已实现的校验会拒绝其他字段名、`{ip.__class__}` 这类属性访问、`{ip!r}` / `{ip:>10}` 这类格式说明、空占位符。
   以下"声明额外字段并用正则白名单"的设想**尚未实现**(E6 默认只有 `{ip}`),留待需要时再做:

```yaml
  - id: dv_done
    from: dv
    to: sw
    template: "{ip}已完成uvm验证(覆盖率{cov}),请执行驱动程序的开发"
    fields:
      cov: {regex: "^[0-9]{1,3}(\\.[0-9]+)?%$"}    # 示例
```

6. **动态配置(已实现,D14)**:拓扑与模板都在配置文件里,用 `TopologyStore` 读写:
   - **热加载**:`current()` 每次比较文件签名,文件被改动(人工编辑或其他进程修改)就重新加载;新内容不合法时保留上一份合法拓扑继续服务,错误记在 `last_error`,不会让 broker 崩溃。
   - **动态修改**:`add_ip` / `remove_ip` / `add_role` / `remove_role` / `set_role_launcher` / `add_edge` / `remove_edge` / `set_template`。每次修改都在跨进程文件锁里:重新读磁盘 → 修改 → 整体校验 → 备份上一版为 `.bak` → 原子写回。多个进程同时修改不同条目不会互相覆盖;修改会让拓扑非法时整体拒绝,文件不变。
   - **修订号**:`revision` 是文件内容的 SHA-256 前 12 位,审计日志应记录它。
   - **局限**:程序写回会整体重写 YAML,**不保留注释**(上一版在 `.bak`)。`remove_ip` / `remove_role` 不检查是否还有对应 agent 在运行,调用方(lifecycle)应先清除 agent。
7. **一条边一个模板**(v2 默认)。若同一对角色之间要有多种句式,给它们不同的 `id`,发送时按 `id` 选择(见 §10 的待确认项 E1)。

### 3.1 鉴权顺序(发送一条消息时,任一步失败即报错,**不执行任何 herdr 命令**)

已实现于 `router.py` 的 `Router.send(edge_id, env)`。接口里**没有**目标 IP、目标名字、自由文本这些参数,
发送方身份只能从环境变量推出:

```
send(edge_id, env):
 1. 发送方身份 —— identity.resolve_sender(env, registry, topology, session)(唯一入口)   拒绝代码: identity
      · 环境变量 A2A_PROJECT_ID / A2A_ROLE / A2A_IP / HERDR_PANE_ID 必须齐全,且 A2A_PROJECT_ID 与拓扑一致
      · 用 HERDR_PANE_ID + 会话在注册表里找到该 pane 登记的 agent;找不到 → SenderNotRegisteredError
      · 环境变量声明的 (project, ip, role) 必须与登记记录完全一致 → 否则 IdentityMismatchError(防冒充)
      · 该 agent 必须是 running → 否则 SenderNotRunningError
      · (role, ip) 必须仍在当前拓扑里 → 否则 NodeNotInTopologyError
      · 会话来自 Router 构造参数,否则取环境变量 HERDR_SESSION,再没有则按 "default"
 2. 边必须存在                                                                       拒绝代码: unknown_edge
 3. 边的 from 角色 == 发送方角色(方向正确)                                           拒绝代码: wrong_direction
 4. 目标 = (边的 to 角色, 发送方自己的 IP) —— 同 IP 铁律由构造保证,没有参数能改变目标 IP
    目标节点必须在当前拓扑里(纵深防御)                                                 拒绝代码: target_not_in_topology
 5. 目标必须已登记                                                                   拒绝代码: target_missing
    目标登记的 project / IP 必须与发送方一致(注册表被破坏时的防御)                      拒绝代码: target_missing
    目标必须是 running                                                               拒绝代码: target_not_running
 6. 渲染模板(只填入 {ip});检查非空、长度(默认 ≤ 1000 字符)、控制字符(允许 \n \t)   拒绝代码: bad_message
 7. 写入持久队列(状态 QUEUED),写审计日志,返回回执(msg_id、src、dst、text、拓扑修订号)
```

要点:

- **每次拒绝在审计日志里恰好留下一条记录**,含 `msg_id`、`edge_id`、`reject_code`、原因、拓扑修订号。身份未核实时,日志里的 `src` 为空,环境变量声称的身份记在 `claimed_*` 字段(不可信,仅供排查)。
- 每次发送都取 `TopologyStore.current()`,所以模板、边的改动无需重启即生效;审计里记录当时的拓扑修订号。
- **Router 不检查目标是否 `blocked`**:那要问 herdr,属于 broker 的投递阶段(`TARGET_BLOCKED`,§4.3)。
- 被拒绝的消息不入队,查它请看审计日志;已入队的消息可用 `Router.status(msg_id)` 查询。
- 去重(同一 `(edge_id, ip, 关联键)` 在幂等窗口内只投递一次)放在 broker(§4.7),Router 不做。

## 4. 等待与投递机制(你 Q4 让我设计的部分)

### 4.1 好消息:不必自己"轮询"

herdr 自带阻塞等待:`herdr agent wait <target> [--until STATUS]... [--timeout MS]`。[本机验证]
它在 herdr 服务端等待状态变化,**不需要我们写 sleep 循环**。

如果后期想要同时监控大量 agent,socket API 有 `events.subscribe`(事件推送)。[文档]
所以真正需要设计的不是"怎么轮询",而是**怎么保证并发下不丢、不乱、不重**。

### 4.2 最大的坑:等到 idle 和发出去之间有空隙

```
A:  agent wait dv_uart → idle ✓
B:  (同时) 另一个发送方也等到了 idle ✓
A:  agent prompt dv_uart "..."   → dv_uart 开始 working
B:  agent prompt dv_uart "..."   → 打断/拼接到 A 的内容后面
```

两个发送方同时看到 idle,各发一条,会造成消息交织。这是**竞态**,`wait` 解决不了。

### 4.3 方案:单一 broker + 每目标串行投递 + 持久队列

你的决定:**发完即走;目标必须 READY(idle,`done` 视同可投递)才发;目标卡在审批则立即返回。**

"发完即走"意味着发送方调用后立刻返回,真正的等待与投递必须由别人在后台完成。
所以引入**单一 broker 进程**(部署在虚拟机内,见 §8):

```text
agent 里:  a2a send dv_done
             → 鉴权 + 渲染模板 + 写入持久队列 → 立即返回 msg_id          (毫秒级)
broker:    每个目标一个串行 worker,按 FIFO 逐条投递
```

每目标一个串行 worker,**就等价于目标锁**:同一目标同一时刻只有一个 worker 在投递,所以不会出现"两个发送方同时看到 idle 各发一条"。
用一把 `flock` 保证 broker 实例只有一个;broker 重启后从磁盘队列恢复。

worker 对单条消息的流程:

```
deliver(msg):
    s = agent get dst
    if s.status == blocked:                       # 目标卡在审批/提问
        标记 TARGET_BLOCKED,写审计,立即返回失败     # 你的决定:不排队等待
    agent wait dst --until idle --until done --until blocked --timeout T
    s = agent get dst                              # 再确认一次
    if s.status == blocked:   → TARGET_BLOCKED,立即失败
    if s.status not in (idle, done):  → 视为未就绪,回到 wait(有限次),耗尽 → TIMEOUT
    agent prompt dst "<渲染后的文本>"                 # 不加 --wait:发完即走
    agent wait dst --until working --timeout 5s    # 仅确认对方确实开始处理
    标记 DELIVERED,写审计
```

要点:

1. **等待不自己轮询**:`agent wait` 在 herdr 服务端阻塞,等到 READY 或 `blocked` 就返回。
2. **等待与发送之间的空隙**由"每目标串行 worker"消除。herdr 的 `agent wait`/事件订阅只解决等待,不提供"等待 + 发送"的原子性。
3. **持久化**:消息先落盘(`spool/{dst}/{msg_id}.json`)再返回;崩溃后可恢复,不丢。
4. **去重**:每条消息带 `msg_id`。官方提示超时或 `agent_prompt_stalled` 不代表没发出去,重试前先检查对方状态和最近输出,避免重复提交。[文档]
5. **FIFO**:同一目标的多条消息按入队顺序投递。
6. **"发完即走"的边界**:框架不等待对方完成任务,也不读回结果;只用"对方进入 working"确认投递成功。
7. **blocked 不自动处理**:不替它点批准;发送方得到失败结果(通过 `a2a status <msg_id>` 查询),由人去处理。

### 4.4 状态转换表(v3)

框架内部把 herdr 状态归一化:

| herdr 状态 | 框架分类 | 行为 |
| --- | --- | --- |
| `idle` | **READY** | 投递 |
| `done` | **READY** | 投递(见下) |
| `working` | BUSY | 等待 |
| `blocked` | BLOCKED | **立即返回失败**(`TARGET_BLOCKED`),不排队 |
| `unknown` | UNRELIABLE | 不能当作 READY;等待至超时后失败(`TIMEOUT`) |
| agent 不存在 | MISSING | 失败(`TARGET_MISSING`),不重试 |

**关于 `done`(已实测)**:

- `done` 表示"后台完成、你还没看过"。在虚拟机实测中,无人查看时它可以停 20 秒以上不变;有人查看(聚焦)后变成 `idle`。
- `agent wait --until idle` 遇到 `done` 会**超时失败**(退出码 1);`--until done`、或 `--until idle --until done` 立即返回。
- 对 `done` 状态的 agent 发 `agent prompt` 成功。
- 所以框架**必须**把 `idle` 与 `done` 同时视为 READY,等待时必须写 `--until idle --until done`;只等 `idle` 会让后台完成的 agent 永远卡住。

### 4.5 发送语义(v3:发完即走)

- 发送方调用后**立即返回** `msg_id`,不阻塞等待目标空闲。
- 不回传任务结果。以后需要回执时,加一条反向边,让下游 agent 同样通过框架给上游发消息;通信语义保持对称,也避免读屏幕(全屏 TUI 的备用屏幕问题)。[文档]
- 发送方可查询投递状态:`a2a status <msg_id>`(见 §4.6)。

### 4.6 消息状态机与审计日志(借鉴 codex)

> **权威定义见 `08-protocol.md`**(状态、合法迁移表、每个迁移的触发条件、崩溃恢复规则、审计事件、拒绝代码)。本节是概览,与 08 不一致时以 08 为准。
> 当前为**协议版本 2(已定稿,2026-10-09)**。要点:`DELIVERY_UNCERTAIN` 默认不自动重发,只能由操作员裁定或经真实 herdr 验证的机制离开,且没有由时间触发的出口;只有能证明 prompt 未提交的错误才能重试;每个目标只处理队列头,不确定态和确定失败都会暂停该目标的队列,等操作员处理(08 §7)。
> 下面的 `CREATED` / `ROUTED` 只是示意,协议里没有这两个状态:Router 鉴权通过后直接以 `QUEUED` 入队。

```text
CREATED ──鉴权通过──▶ ROUTED ──入队──▶ QUEUED
QUEUED ──目标 READY──▶ DISPATCHING ──herdr 接受 prompt──▶ DELIVERED
```

异常终态/中间态:

| 状态 | 触发 | 是否自动重试 |
| --- | --- | --- |
| `REJECTED` | 鉴权失败(无效跳转、跨 IP、身份不符、字段不合法) | 否 |
| `TARGET_BLOCKED` | 目标卡在审批/提问 | **否**(你的决定:立即返回;Broker 不自动重试,操作员可显式裁定重试) |
| `TARGET_MISSING` | 目标节点不存在或已删除 | 否 |
| `WAITING_TARGET` | 目标 BUSY,正在等 | (等待中) |
| `TIMEOUT` | 等待 READY 超时,或目标状态查询持续失败 | 否(终态;暂停该目标队列,等操作员"重试"或"放弃并继续") |
| `RETRYING` | **已证明 prompt 未提交**的错误(08 §5;herdr 0.9.3 实测:`agent_not_ready`、`server_not_running`,以及请求发出前的客户端失败) | 是,退避 |
| `FAILED` | 操作员放弃,或确定不可恢复的错误;从 `DELIVERY_UNCERTAIN` 只能由操作员放弃进入 | 否 |
| `DELIVERY_UNCERTAIN` | 调用 herdr 后结果不明(超时、`agent_prompt_stalled`、无法证明未提交的错误、Broker 崩溃窗口、观察窗口内没看到目标开始处理) | **否**:默认不自动重发;只有操作员明确裁定,或经真实 herdr 验证的机制确认后,才转 `DELIVERED` / `RETRYING`;超时只告警 |

审计日志(追加写,每条记录一行 JSON):

```json
{"ts":"...","msg_id":"...","edge_id":"dv_done","src":"dv_uart","dst":"sw_uart",
 "state":"DELIVERED","detail":"agent prompt accepted","attempt":1}
```

被拒绝的发送也必须写入(含原因),这是排查"谁试图越权"的依据。

### 4.7 接收方来源校验与去重(借鉴 codex)

codex 建议接收方也校验来源并去重消费。在你"固定句式模板"的约束下,这件事要这样做:

- **去重放在 broker**:同一 `(edge_id, ip, 关联键)` 在幂等窗口内重复发送,只投递一次。
- **接收 agent 无法验证来源**:收到的只是一句固定中文,没有结构化字段。要做接收方校验,需要在模板之外由框架附加一段机器可读标记(例如尾部 `[a2a:dv_done:msg_id]`)。
  这会改变"消息只能是固定句式"的字面含义,所以**默认不加**,你已决定先不做(D11)。

## 5. Python API 草案

> v2:agent **不直接调用 herdr**,只通过框架。框架先鉴权(§3.1),失败则报错、不执行。

```python
from a2a import Topology, Provisioner, Router

topo = Topology.load("a2a_topology.yaml")      # 拓扑 + 模板,单一事实源
prov = Provisioner(topo)                       # 封装 herdr CLI
router = Router(topo, prov)

# ---- 供应(Q1,可动态增删)----
prov.ensure_workspace()                          # 建/找 workspace
prov.ensure_role_tab("dv")                       # 建 tab,label=验证智能体
prov.spawn("dv", "uart")                         # 拆 pane + 注入身份 + 预检 + 覆盖 exec 启动(§2.3)+ 启动后核对 + agent rename(§2.1.1)
prov.spawn_all(roles=["dv", "sw"], ips=topo.ips) # 批量
prov.stop("dv", "uart")                          # 只停 agent 进程,保留 pane 与注册信息(默认,可恢复)
prov.close("dv", "uart")                         # 关闭 pane,但保留注册信息与消息历史,可重建
prov.purge("dv", "uart")                         # 彻底删除:pane、注册表、拓扑中的该节点;历史仍留审计日志
prov.restore("dv", "uart")                       # 按注册表重建 pane 并重启 agent
prov.rename_tab("dv", "验证智能体")               # 改显示名,不影响寻址

# ---- 拓扑与模板的动态配置 ----
topo.add_edge(id="dv_done", frm="dv", to="sw", template="{ip}已完成uvm验证,请执行驱动程序的开发")
topo.remove_edge("dv_done")
topo.set_template("dv_done", "...")
topo.save()                                      # 写回配置文件

# ---- 通信 ----
router.send("dv_done")      # 发送方身份由环境变量+注册表确定,目标 = (边的 to 角色, 同 IP)
#   → 鉴权 → 渲染模板 → 入队 → 等 idle → 投递

# ---- 观测 ----
router.status("sw", "uart")     # agent get
router.read("sw", "uart", lines=120)
router.pending()                # 队列中待投递/待人工的消息
router.audit(tail=100)          # 审计日志(含被拒绝的发送)
```

agent 在自己 pane 里使用的 CLI:

```bash
a2a send dv_done                  # 立即返回 msg_id,投递在后台完成
#   发送方 = 从 A2A_ROLE / A2A_IP / HERDR_PANE_ID 解析出的 (dv, uart)
#   目标   = (sw, uart)  —— 由边 dv_done 的 to 角色 + 发送方的 IP 推出
#   消息   = 模板渲染结果:"uart已完成uvm验证,请执行驱动程序的开发"
#   agent 无法指定目标 IP,也无法写自由文本
```

好处:

- 接口里**没有"目标 IP"和"自由文本"参数**,越权在接口形态上就不可表达。
- 鉴权、拓扑校验、模板渲染都在一个入口完成。
- agent 只需要知道事件 id(`dv_done`),不需要知道对方的名字或 pane id。

### 内部封装:只薄封装 herdr CLI

| 框架方法 | herdr 命令 |
| --- | --- |
| `ensure_role_tab` | `tab create --label` / `tab rename` |
| `spawn` | `pane split --env A2A_ROLE=.. --env A2A_IP=.. --no-focus --cwd <明确路径>` + `pane rename` + 预检 + `pane run`(覆盖 `exec` 的启动命令,§2.3.3)+ `pane process-info` / `agent list` 核对 + `agent rename <pane> <agent_id>` |
| `remove` | `pane close` |
| `status` | `agent get` |
| `read` | `agent read --source recent-unwrapped` |
| deliver(broker) | `agent get` + `agent wait <名字> --until idle --until done --until blocked` + `agent prompt <名字> "<文本>"` |

CLI 的 stdout 是 JSON,服务端错误以 JSON 输出到 stderr 并退出码为 1,语法错误退出码为 2。[文档]
Python 用 `subprocess.run` 解析 JSON 即可,不依赖 socket 协议细节。

### 5.1 "agent 不得直接调用 herdr"如何落地

你的决定是:agent 发消息必须走 Python API。这条规则本身是**对 agent 行为的要求**,有三种落地强度(请选):

| 强度 | 做法 | 能防住什么 |
| --- | --- | --- |
| L1 提示词约定 | 在 pi 二开项目的 AGENTS.md/系统提示里写明"只能用 `a2a send`,禁止 `herdr agent prompt`" | 防不住 agent 违反约定 |
| L2 工具层限制 | 在 pi 的工具/命令白名单里,禁止执行含 `herdr agent prompt`、`herdr pane send-*`、`herdr pane run` 的命令 | 防住按字面调用的情况 |
| L3 PATH 包装 | 虚拟机里让 agent 进程 PATH 上的 `herdr` 指向一个包装脚本,仅放行只读子命令 | 最强,仍可能被绕过(绝对路径) |

我的建议是 **L1 + L2 起步**。L1/L2 是改动你自己的 pi 二开仓库,我没有读过它们的权限/工具代码,
所以具体怎么做,需要看了代码才能给方案。

L1/L2/L3 都属于"约定或半强制",agent 在同一 OS 用户下仍可能直接调用 herdr(例如用绝对路径)。
codex 建议把"不能绕过 broker"提升为**运行时权限边界**:

| 强度 | 做法 | 现实约束 |
| --- | --- | --- |
| L4 broker 独占 | agent 只能与 broker 通信(一个受限入口),由 broker 独占对 herdr 的调用权 | herdr 把 agent 作为自己的子进程运行,agent 与 herdr server 同属一个 OS 用户,默认能访问同一个 socket。要做到真隔离,需要沙箱或不同用户,**可行性我没有验证过,需实测** |

建议路径:**先 L1 + L2,上线后观察审计日志;若出现绕过再评估 L4。**

**L2 在 pi 里的具体做法(新发现)**:pi 的扩展接口有 `pi.on("tool_call", ...)` 钩子,在工具执行前触发,
返回 `{ block: true, reason: "..." }` 即可**阻止这次调用**。(`docs/extensions.md`)
所以可以写一个扩展:当 agent 想用 `bash` 执行包含 `herdr agent prompt`、`herdr pane send-*`、`herdr pane run` 等写入类命令时,直接拦下并告诉它"请用 `a2a send`"。
局限:它拦的是"字面上的命令",agent 若把命令写进脚本再执行、或拼接字符串,可能绕过。它能挡住绝大多数无意的误用,挡不住刻意绕过。

### 5.2 TUI 里的 agent(fnx_dv / fnx_sw)怎么调用 `a2a send`

> 依据:我读了 `pi-custom/packages/coding-agent` 的源码和文档(`src/core/tools/bash.ts`、`docs/extensions.md`、`docs/security.md`)。
> `fnx-sw` 与 `pi-custom` 同为 pi-monorepo,我只读了 `pi-custom`;`fnx-sw` 是否一致需另行确认。

**结论:pi 本来就能执行 shell 命令,所以 agent 直接在对话里"决定调用"即可,不需要任何额外通道。**

pi 内置工具里有 `bash`(`bash.ts`):模型想执行命令时,pi 就在后台 shell 里运行并把输出还给模型。
所以只要 `a2a` 命令装在虚拟机里、在 `PATH` 上,agent 就能像执行 `ls` 一样执行:

```bash
a2a send dv_done
```

有三种让 agent"知道什么时候该调用"的方式,由弱到强:

| 方式 | 做法 | 特点 |
| --- | --- | --- |
| A. 写进提示词/技能 | 在 `AGENTS.md`(pi 会自动读取)或 pi 的 skill 里写:"UVM 验证全部通过后,执行 `a2a send dv_done`,不要执行其他 herdr 命令" | 最简单;靠模型自觉 |
| B. 自定义工具 | 写一个 pi 扩展,用 `pi.registerTool()` 注册一个叫 `a2a_send` 的工具,只接受一个参数(边 id) | 模型调用的是"函数",不是拼 shell 字符串;参数受限,不能夹带自由文本 |
| C. A + B | 提示词告诉它什么时候用,工具限制它怎么用 | **推荐** |

要让这条链路成立,有三个前提需要**实测**:

1. `A2A_ROLE`、`A2A_IP`、`HERDR_PANE_ID` 这些环境变量:**已验证**用户侧 `!` 命令能读到(`fnx_dv`、`fnx_sw` 均可)。**模型侧 bash 工具**读到与否仍需再确认。
2. `a2a` 命令在虚拟机里对 pi 进程可见。
3. pi 二开版(`fnx_dv`、`fnx_sw`)的 `bash` 工具没有被你们改成需要人工批准;若需要批准,会卡在这一步,无人值守流程就跑不通。

注意:pi 官方文档(`docs/security.md`)明确写了 **pi 没有内置沙箱**,工具以启动它的用户权限运行。这决定了 §5.1 里 L4 很难做到。

## 6. 风险与我的坦率提醒

1. **"拦截无效跳转"依赖 agent 只走框架。**
   框架 API 能保证"走框架的调用一定被鉴权";但 agent 如果绕开框架直接执行 `herdr agent prompt <别人>`,框架拦不住。
   你已决定 agent 必须走 Python API,这是对 agent 行为的要求,落地强度见 §5.1(L1/L2/L3)。
2. **提示注入链。** 一个 agent 读到的外部内容(例如 UVM 日志里的字符串)可能被它转写成对下游的指令。
   150 个 agent 串联后风险被放大。建议消息体只允许**框架模板生成的固定句式 + 少量结构化字段**,
   不允许把 agent 自由生成的长文本原样转发给下游。
3. **权限审批会卡住流水线。** 下游 agent 执行命令/写文件会弹审批,状态变 `blocked`。
   如果希望无人值守跑通,需要你**单独**决定权限策略。我不建议为了流畅默认全开。
4. **150 个 agent 的资源。** 每个 pi 实例是一个 Node 进程加模型调用。虚拟机的内存/CPU 是否承受,需要先压测。
5. **一个 tab 30 个 pane 的布局。** 见 §1 担心 1 和 §7。
6. **状态检测依赖屏幕识别。** 你的二开 pi 如果改了界面标题或提示符,herdr 可能识别为 `unknown`。
   herdr 对 `pi` 有官方 integration,会往 `~/.pi/agent/extensions/` 写文件。[文档]
   二开版本是否沿用同一目录,需检查。装 integration 会改动用户配置,装之前请先看原文。
7. **名字长度。** `{角色码}_{ip}` 受 32 字符限制;IP 名较长时要缩写表,且缩写必须唯一。

## 7. 布局备选(针对"一个 tab 30 个 pane")

> v2:你已决定**先不管布局**。本节仅作备忘,不影响当前设计。通信只依赖 agent 名字,与布局无关。

herdr 文档里 pane 是真实终端,有最小尺寸。可选思路(都需实测):

| 方案 | 思路 | 代价 |
| --- | --- | --- |
| A. 不管布局 | 一个 tab 里堆 30 个 pane,平时 `pane zoom` 看单个 | 拆分可能失败;视觉上不可用 |
| B. 控制台 + 后台 | 只有"当前关注的 IP"摊开,其他 pane 用 `pane move --new-tab` 放到临时 tab | 需要动态搬运,tab 语义被稀释 |
| C. 一角色多 tab | 按 IP 分页(如每 8 个 IP 一个 tab),角色内逻辑分组由注册表保证 | 与你"5 个 tab=5 个角色"的映射不再一一对应 |
| D. 多 workspace | 每个 IP 一个 workspace,里面 5 个 tab(角色) | **与你 drawio 的结构相反**,但天然规避密集布局;寻址仍靠 agent 名字,不受影响 |

关键事实:**通信不依赖布局**,只依赖 agent 名字。所以布局可以后期调整而不改通信层。
这也是我建议"寻址靠 agent 名字 + 注入身份,而不是靠 tab/pane 位置"的原因。

## 8. 虚拟机(192.168.139.41)相关

> v2:你已决定**框架先放虚拟机内运行**,即下表第一种方式。

你的 herdr 和两个 pi 系 agent 都在 OrbStack 虚拟机里。选择:

| 方式 | 说明 |
| --- | --- |
| 框架运行在虚拟机内 | 直接调本地 herdr,最简单,锁也是本机 flock。**推荐起步** |
| 框架运行在 Mac,控制虚拟机 | 用 `herdr --machine <label>` 在已保存的 SSH 机器上执行 API 命令。[本机验证选项存在] 需要先 `herdr machine add` 配置;本地 flock 对远端目标不再适用 |

起步先做第一种。

安全(借鉴 codex):

- herdr 的 socket 是本地 Unix 域 socket(默认 `~/.config/herdr/herdr.sock`),**不要**通过 SSH 隧道、socat 等方式暴露到局域网。
- 框架运行在虚拟机内,只通过本机 socket/CLI 调 herdr;Mac 侧如需观察,走 SSH。
- broker 的入口(接收 `a2a send` 的本地通道)同样只监听本机,并限制访问权限。
- socket 和 session 数据里可能含提示词、命令输出、密钥,按终端历史同等对待保护。

## 9. 建议的验证顺序(小规模原型,不动主会话)

使用**命名测试会话**,2 个 IP × 2 个角色:

1. 起一个命名 session(`herdr --session a2a_test`),不动你的主会话。
2. `workspace create --label soc_test`、`tab create --label dv`、`tab create --label sw`。
3. `pane split ... --env A2A_ROLE=dv --env A2A_IP=uart --no-focus --cwd <路径>`,在里面按 §2.3.3 启动 `fnx_dv`。
   验证:被识别为 `pi`(`agent list`)、环境变量能被读到。(`fnx_dv`、`fnx_sw` 上已验证通过;其余三个智能体待验证。)
4. 同理建 `sw_uart`、`dv_gpio`、`sw_gpio`。
5. **跨 tab 通信**:手工 `herdr agent prompt sw_uart "..." --wait`,确认跨 tab 可用。
6. **竞态复现**:并发发两条到同一个目标,观察是否交织,验证锁的必要性。
7. **blocked 行为**:让目标停在审批界面,确认 `agent_blocked`。
8. **反向用例(必须全部被拒绝,借鉴 codex)**:
   - 同角色跨 IP:`dv_uart` → `sw_gpio`
   - 未配置的边:`sw_uart` → `dv_uart`(只配了 `dv→sw` 时)
   - 方向反了:`sw → dv`
   - 目标节点不存在或已 `purge`
   - 发送方身份不符:伪造 `A2A_IP`,而 `HERDR_PANE_ID` 对应的是别的节点
   - 模板字段不合法
   - 目标处于 `blocked`:应立即得到 `TARGET_BLOCKED`,且不向其写入任何输入
   每一项都要在审计日志里留下一条带原因的记录。
9. 再开始写 Provisioner / Router / Broker。

每一步只做**可逆、可清理**的操作;清理用 `pane close` / `tab close` / 停掉该命名会话。

## 10. 决策记录与待确认项

### 10.1 已决定

| # | 议题 | 决定 | 落点 |
| --- | --- | --- | --- |
| D1 | 拦截无效跳转 | agent 只能通过框架 API 发送;API 先鉴权,不在拓扑中则报错、不执行 | §3.1、§5、§5.1 |
| D2 | 消息内容 | 模板 + 少量字段,发送方不能写自由文本;模板可动态配置 | §3 |
| D3 | 发送语义 | 发完即走;必须目标 READY 才发 | §4.3~4.5 |
| D4 | 布局 | 暂不处理 | §7 |
| D5 | 框架运行位置 | 先放虚拟机内 | §8 |
| D6 | 名字对应 | 验证 = `fnx_dv`,软件 = `fnx_sw` | §3 |
| D7 | 回退通信 | 需要;拓扑是唯一入口,边有向,A→B 与 B→A 分别定义 | §3 |
| D8 | 目标卡在审批 | **立即返回失败**,不排队 | §4.3、§4.4 |
| D9 | `done` | **当作可投递**(归入 READY),已实测证实必要 | §4.4 |
| D10 | 业务 ID | **不带项目前缀**:`agent_id` = `{角色码}_{ip}` | §2.1 |
| D11 | 接收方校验 | **先不验证消息来源**;去重只放在 broker,不在固定句式后附加标记 | §4.7 |
| D13 | `project_id` | **保留**。拓扑必填;随 `A2A_PROJECT_ID` 注入;进入注册表和身份校验;**不进入 `agent_id`**(仍是 `{角色码}_{ip}`,与 D10 不冲突) | §2.1、§3 |
| D14 | 动态拓扑 | **现在实现**:热加载 + 动态增删 IP / 角色 / 边 / 模板 + 跨进程文件锁 + 原子写回 + 写前备份 | §3 |
| D17 | 消息状态迁移 | 写入协议文档 `08-protocol.md`:状态只能沿合法迁移表前进;终态之后不可再修改;新增 `DELIVERY_UNCERTAIN`;Broker 启动时先 `recover()`,再把停在 `DISPATCHING` 的消息迁到 `DELIVERY_UNCERTAIN` | `08-protocol.md`、`messages.py` |
| D16 | 发送方的会话名 | 取自 pane 环境变量 `HERDR_SESSION`。**命名会话里已实测 herdr 会注入它**(以及 `HERDR_SOCKET_PATH`、`HERDR_BIN_PATH`);默认会话里的取值未测,没有该变量时按 `"default"` 处理 | `router.session_from_env` |
| D15 | 状态目录 | `A2A_STATE_DIR` > `$XDG_STATE_HOME/a2a` > `~/.local/state/a2a`;路径必须是绝对路径(agent 工作目录各不相同,相对路径会悄悄产生多份状态);拓扑文件另可用 `A2A_TOPOLOGY` 指定 | `paths.py` |
| D12 | herdr 调用方式 | **CLI 先行**(subprocess 调 herdr CLI);**socket API 留到规模化阶段**(事件订阅、大量并发等待),压测证明需要时再做。`HerdrClient` 公开方法不绑定传输方式,日后只需重写内部 `_call` / `_call_text` | §2.2、§4.1 |

### 10.2 仍待确认

| # | 问题 | 我的默认处理 |
| --- | --- | --- |
| E1 | 同一对角色之间,是否需要**多种句式**? | 允许多条边,用不同 `id` 区分 |
| E4 | "agent 不得直接调用 herdr"的落地强度(§5.1 的 L1~L4)? | 先 L1 + L2,看审计日志再决定是否上 L4 |
| E5 | 发送方身份防冒充:接受"环境变量 + `HERDR_PANE_ID` 与注册表交叉核对"? | 接受则按 §3.1 第 1 步实现 |
| E6 | 模板里除 `{ip}` 外是否还要其他字段(如 artifacts 路径、覆盖率)? | 默认只有 `{ip}`;如需要,按 §3 第 5 条声明并校验 |
| E8 | `blocked` 导致失败后,发送方 agent 如何得知并处理? | 提供 `a2a status <msg_id>`;是否要自动通知人,待你决定 |

### 10.3 实测状态

| # | 事实 | 状态 |
| --- | --- | --- |
| 1 | `fnx_dv` / `fnx_sw` 能否被 herdr 识别为 `pi` | **已验证**:不能自动识别;`report-agent` 不够(`agent prompt` 失败);按 §2.3 覆盖 `exec` 启动后可识别。另外三个智能体未验证 |
| 2 | `--env` 注入的变量能否被 agent 继承 | **已验证**:pane 里的 shell 和 `fnx_dv` / `fnx_sw` 的 `!` 命令都能读到 |
| 3 | 跨 tab 的 `agent prompt` | **基本验证**:从 pane 外的 shell 向非聚焦 tab 里的 agent 发送成功;严格的"从 dv 的 pane 内发往 sw 的 pane"未单独测 |
| 4 | `agent wait` 与 `done` | **已验证**:`done` 不一定自己变回 `idle`(无人看时可停 20 秒以上;有人查看时约十几秒后变 `idle`);`agent wait --until idle` 在 `done` 时会超时失败,`--until done` 或 `--until idle --until done` 立即返回;对 `done` 的 agent 发 prompt 成功。所以必须同时等 `idle` 和 `done` |
| 5 | 同一目标并发投递 | **已验证**:两条同时发给 `working` 的 agent,都被接受,按两轮依次处理,无文字交织,但**顺序不确定**。broker 串行投递的目的是保证顺序、避免在对方忙时发送 |
| 6 | 同一 OS 用户下能否阻止 agent 绕过 broker 直接调用 herdr(L4) | 未验证 |
| 7 | `blocked` 状态下 `agent prompt` 的行为 | 未验证(pi 默认没有审批界面,未找到触发办法) |
| 8 | pi 模型侧 bash 工具是否弹批准 | 未验证(只验证了用户侧 `!` 命令) |
| 9 | 办法 C 的前提(只有一处 `exec`、不用 `$0`)对另外三个智能体是否成立 | 未验证;框架启动前预检(§2.3.5) |
| 12 | pane 里注入的 herdr 环境变量 | **已验证(命名会话)**:`HERDR_ENV=1`、`HERDR_PANE_ID`、`HERDR_TAB_ID`、`HERDR_WORKSPACE_ID`、`HERDR_SESSION`(会话名)、`HERDR_SOCKET_PATH`、`HERDR_BIN_PATH`。默认会话未测 |
| 14 | 队列与审计的崩溃恢复(复核后修复 P1~P7) | **已验证**(Mac 与虚拟机):归档两步之间强杀 → `pending()` 不再返回已送达消息,`recover()` 清理残留;审计写到一半强杀 → 下次追加前残片被隔离,新记录完整;ID 路径穿越、未知状态、终态回退、目标在另一会话均被拒绝 |
| 13 | Router 的反向用例、多进程并发发送 | **已验证**(Mac 与虚拟机):见 `tests/test_router.py`;6 个进程并发发送 30 条,全部入队、ID 唯一、审计无交织 |
| 11 | 拓扑与注册表的跨进程并发、写入中途崩溃 | **已验证**(Mac 与虚拟机):6 个进程并发修改拓扑、8 个进程并发登记注册表均不丢更新;在 `os.replace` 之前强杀进程,原文件完好、可继续写入 |
| 10 | `agent rename` 与按名字通信 | **已验证**:改名成功;按名字 `get` / `prompt` / `wait` / `read` 都成功;重名与非法名被拒;退出后名字失效、重启后不自动恢复(§2.1.1) |

> 另:`herdr agent list` 本身输出 JSON,本机版本**不接受** `--json` 参数(用法输出为 `usage: herdr agent list`)。所有示例都不要写 `--json`。
