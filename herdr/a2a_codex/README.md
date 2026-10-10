# a2a_codex：HerdrClient、动态拓扑与 Router

这是 A2A 项目的独立主线：Herdr CLI Python 封装、动态拓扑、稳定业务身份、持久 Registry，以及 Router / 固定模板 / 持久队列 / 审计。当前包含单消息投递引擎、常驻 Broker Runtime、队列告警与基础业务 CLI；同时包含 fnx 所需的 `exec -a pi` 启动器和 agent 重命名流程。

## 本地测试

```bash
cd /Users/jhx/Documents/code/personal/pi_agent/A2A/herdr/a2a_codex
PYTHONPATH=.. python3 -m unittest discover -s tests -v
```

## 使用

```python
from pathlib import Path
from a2a_codex import HerdrClient, build_launch_command

client = HerdrClient("walk1")
created = client.create_tab(label="dv", cwd="/home/jhx/A2A_Test",
                            env={"A2A_ROLE": "dv", "A2A_IP": "uart"})
client.start_agent(created.pane_id,
    build_launch_command("/home/jhx/.forenyx/fnx_dv/bin/fnx_dv"))
client.wait_for_agent_detected(created.pane_id, timeout_s=60)
client.rename_agent(created.pane_id, "dv_uart")
client.wait_agent("dv_uart", until=("idle", "done"), timeout_ms=30000)
print(client.read_agent("dv_uart", lines=60))
```

`HerdrClient.send_prompt()` 是 Broker 使用的底层控制接口，不是 agent 的通信入口；agent 的业务发送必须经 `Router.send(edge_id)` 鉴权和模板渲染。`a2a-herdr` 命令行也不提供任意文本 prompt 子命令。

安装本项目后可使用 `a2a`（`a2a-herdr` 仍作为兼容入口）：

```bash
a2a --topology /absolute/path/topology.yaml send dv_done
a2a --session soc_a broker run
a2a status <msg_id>
a2a queue [<dst_agent_id>]
a2a resolve <msg_id> retry --reason "核实后重试"
a2a ruling void <ruling_id> --reason "核实效果后作废" --verified-effects '{"state":"unchanged"}'
a2a dispatch resume --reason "数据已修复并核验" --quarantine-location none
a2a spool quarantine list
a2a spool repair <incident_id> --reason "从备份恢复" --verification "消息状态和 queue_seq 已核对"
```

`send` 只接受拓扑中已配置的 `edge_id`，发送文本由该边的固定模板生成；没有自由文本 prompt 命令。拓扑路径依次可由 `--topology` 或 `A2A_TOPOLOGY` 指定，未指定时使用包内示例配置。`broker run` 以持久 Spool / AuditLog 启动单实例调度器；`status` 与 `queue` 以 JSON 输出当前消息或目标队列头。`DELIVERY_UNCERTAIN` 队列头会在暂停时写 warning 和审计，1 小时提醒、24 小时升级只产生告警，不改状态、不放行。`resolve`、`ruling void`、`dispatch resume` 都需要 `--reason` 并写审计；`--actor` 默认取当前系统用户名，只作审计署名、不作为认证。Spool 损坏文件从备份恢复到原路径后，先用 `spool repair` 核验并审计，再显式 `dispatch resume`。

`send_prompt(wait=True)` 必须指定 `timeout_ms`。如果收到 `HerdrPromptOutcomeUnknown`（包括 Herdr 的 `agent_prompt_stalled` 和调用进程超时），prompt 可能已经送达；先检查目标 agent，再决定后续动作，不能直接重试。

Herdr 的删除语义是 `close`，所以 `delete_workspace/tab/pane` 分别封装为对应的 `close` 命令；创建 pane 使用 `pane split`。`start_agent` 使用 `pane run`，这是为了支持 fnx 的 `exec -a pi` 启动方式，而不是使用 `herdr agent start --kind pi`。

## 拓扑、身份与 Registry

默认示例配置位于 `config/topology.yaml`。当前 schema 版本为 1，配置 project、role、IP 和有向 edge。role ID（例如 `dv`、`sw`）用于 `A2A_ROLE`，稳定业务 ID 按 `{role}_{ip}` 生成，例如 `dv_uart`。Herdr 的 workspace/tab/pane ID 与显示名只作为运行时地址保存。

```python
from a2a_codex import AgentIdentity, Registry, TopologyStore

topology_store = TopologyStore(Path("config/topology.yaml").resolve())
topology = topology_store.current
identity = AgentIdentity.create(topology, role="dv", ip_id="uart")
registry = Registry()  # A2A_STATE_DIR > XDG_STATE_HOME/a2a > ~/.local/state/a2a
record = registry.register(
    identity,
    session="soc_a",
    workspace_id="w1",
    tab_id="w1:t1",
    pane_id="w1:p2",
    status="idle",
)

# pane 重建时只更新运行时位置，业务身份仍为 dv_uart
registry.update_runtime("dv_uart", tab_id="w1:t3", pane_id="w1:p8")

# Python API 修改拓扑：每次变更都会校验、备份旧 YAML 并原子写回
topology_store.add_ip("i2c")
topology_store.add_edge("sw_to_dv", "sw", "dv", "请验证 {ip}")
topology_store.set_template("sw_to_dv", "请开始验证 {ip}")

# 其他进程或人工修改文件后，current 会按需检查并加载新配置
topology = topology_store.current
```

Registry 使用 JSON 文件、文件锁和原子替换持久化，适用于 macOS/Linux。状态目录可通过绝对路径 `A2A_STATE_DIR` 指定，默认采用绝对路径 `XDG_STATE_HOME/a2a`，未设置 XDG 时回退到 `~/.local/state/a2a`。显式 Registry、TopologyStore、Spool 和 AuditLog 路径也必须是绝对路径。`resolve_sender()` 必须指定当前 Herdr session，再根据 `HERDR_PANE_ID` 查注册记录，并交叉校验 `A2A_PROJECT_ID`、`A2A_ROLE`、`A2A_IP`。这一步只做身份解析，不负责 edge 授权或发送消息。

`TopologyStore` 提供 `add_ip` / `remove_ip`、`add_role` / `remove_role`、`add_edge` / `remove_edge`、`set_template` 和 `set_role_launcher`。每次修改都在跨进程文件锁内基于磁盘最新版本执行，完整校验后保存 `.bak` 备份并原子替换 YAML；失败修改不会写入。读取 `current` 时会检查文件变化，合法编辑立即生效，非法编辑保留最后有效快照并记录 `last_error`；也可以显式调用 `reload()` 强制加载，非法文件会抛出 `TopologyError`。程序写回 YAML 会重排内容且不保留注释。

动态拓扑只改变可用角色/IP/通信边和 launcher 配置，不会自动创建或关闭 Herdr pane，也不会自动清理 Registry 中对应的 agent。删除 IP 前应由调用方确认并处理仍在运行的 agent。

## Router、持久队列与审计

`Router.send(edge_id)` 只接收拓扑中的边 ID。发送方由 `A2A_PROJECT_ID`、`A2A_ROLE`、`A2A_IP`、`HERDR_PANE_ID` 和 Registry 交叉确认；目标角色来自边，目标 IP 强制继承发送方 IP，消息文本只能由拓扑模板中的 `{ip}` 渲染。拒绝的请求会记录原因并抛出 `SendRejected`，不会进入队列；成功则返回 `SendReceipt`，表示“已入队”，不表示对方已收到。

```python
from pathlib import Path
from a2a_codex import AuditLog, Registry, Router, Spool, TopologyStore

router = Router(
    TopologyStore(Path("config/topology.yaml").resolve()),
    Registry(),
    Spool(),
    AuditLog(),
    session="soc_a",
)
receipt = router.send("dv_done")  # 读取当前进程的 A2A_* / HERDR_* 环境变量
print(receipt.msg_id, receipt.dst, receipt.text)
```

队列和审计默认位于 Registry 的状态目录下（`spool/`、`audit.jsonl`），也可以分别传入绝对路径。Router 不调用 Herdr、不等待目标空闲、不发送 prompt；`DeliveryBroker` 负责单条消息投递，`BrokerRuntime` 提供单实例调度和每目标串行 worker。Runtime 启动时先恢复 Spool、将遗留 `DISPATCHING` 转为 `DELIVERY_UNCERTAIN` 并补做未完成裁定；队列头暂停时不会忙轮询，调度故障会持久 fail-closed 停止全部派发。Python API 和 CLI 均支持操作员裁定、作废未完成裁定、Spool 损坏隔离与恢复、告警及显式恢复派发。基础 CLI 已提供业务发送、状态/队列查询、常驻运行及操作员动作；端到端入口仍在后续阶段。

Spool 在重新打开时会清理原子写入留下的临时文件，并在终态归档已写入但 pending 删除前进程退出时，以有效 done 记录为准消除重复 pending。无法读取的 Spool 文件会保留到 `spool/corrupt/` 并持久停止派发；操作员从备份恢复到原路径后，经 `BrokerRuntime.resolve_spool_corruption()` 核验和审计，再显式恢复派发。AuditLog 会把崩溃留下的不完整尾行保留到 `audit.jsonl.corrupt` 并写入 `AUDIT_REPAIRED`；中间坏行通过 `CORRUPT_LINE` 暴露，安全敏感恢复读取会 fail-closed。故障注入测试覆盖进程中断窗口，不等同于断电或底层存储设备故障测试。
