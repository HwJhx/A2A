# 阶段 7 测试方案:回退通信 + 多 IP 隔离(草案,待 Codex 审核、用户确认)

## 1. 目标

阶段 7 只剩一项:"回退通信(如验证 → RTL)按拓扑配置跑通"。同时把阶段 8 里"多 IP 并发"的基础验证提前做掉。要验证:

- 回退边(反方向的边)和正向边一样,按拓扑鉴权、填模板、投递;
- 一个角色有多条出边时,发送方选哪条就发到哪里,不会串;
- 两个 IP 同时跑,消息不会跨 IP,一个 IP 的队列暂停不影响另一个 IP;
- 真实模型在"多条出边"时能选对边(dv 同时有 `dv_done` 和 `dv_bug`)。

用户决定:spec、arch、rtl 三个智能体暂不安装,用假 agent 代替;回退边按下面第 2 节。

## 2. 测试拓扑(仅测试阶段)

角色:spec、arch、rtl(假 agent)、dv、sw(不调用模型的测试用假 agent,真实模型链路用 fnx)。IP:uart、gpio。

| 边 id | 方向 | 类型 | 模板 |
| --- | --- | --- | --- |
| `dv_done` | dv → sw | 正向(沿用阶段 6) | `{ip} ip 我已经完成了uvm验证，你需要对这个{ip} ip进行 驱动程序开发和HAL框架开发。不要真的去做，直接用 a2a_send 回复测试成功或测试失败（二选一）` |
| `sw_test_pass` / `sw_test_fail` | sw → dv | 反馈(沿用阶段 6) | 同阶段 6 |
| `dv_bug` | dv → rtl | **回退** | `{ip} ip 验证发现 RTL 问题，需要你检查并修复。不要真的去做，直接用 a2a_send 回复已修复` |
| `rtl_fixed` | rtl → dv | 回退的回复 | `{ip} ip 的 RTL 问题已修复，请重新验证。你只回复收到即可，不要真的去做` |

spec、arch 只用来让拓扑是完整的 5 角色(验证不相关的角色不受影响),本轮不给它们配边。

## 3. 假 agent 的扩展

现有 `tests/fake_agent.py` 只记录收到的文字、报告 working/idle。新增模式 `script:<边1>,<边2>,...`(Codex 审核后修订,原为 `reply:<edge_id>`):第 n 次收到输入时,报告 working → 如果第 n 项是边 id,就在自己的进程里执行 `python -m a2a.cli send <边>`;是 `-` 或超出列表就只记录 → 报告 idle。

- 所有参与收发的 agent(dv、rtl、sw)都用这种假 agent,由 `a2a agent spawn` 启动(经启动脚本 + 覆盖 exec,和阶段 5f 一样)。它们是 herdr 可寻址的 agent,能被 broker 投递;也带着 spawn 注入的身份环境变量并已登记,Router 对它们的鉴权和对真实 fnx 一样。
- **发起**:测试用 `herdr agent prompt` 给发送方输入一条触发文字(相当于阶段 6 里操作员给 fnx_dv 的开头提示),假 agent 据此执行脚本里的发送。例如 dv_uart = `script:dv_bug,-`:第 1 次输入(触发)发 `dv_bug`,第 2 次输入(收到 `rtl_fixed`)只记录,不会无限循环。rtl_uart = `script:rtl_fixed`。
- **同步屏障**(用于并发用例):启动脚本可设 `FAKE_BARRIER=<文件路径>`。设了的话,假 agent 在发送前先在日志里写"等待屏障",等文件出现再发。测试确认所有发送方都在等待后才创建文件。
- 发送用 spawn 注入的 `A2A_PYTHON`,并把 `A2A_SRC` 加进 `PYTHONPATH`,与 pi 插件的做法一致,保证用的是正在测试的源码(Codex 复核补充)。
- 每次发送的退出码和输出写进假 agent 的日志,测试据此断言。
- 输入按回车计数。同一发送方的两次触发之间,测试先等它回到 idle(发送已完成)再发下一次,保证"第 n 次输入"与"第 n 条边"稳定对应(Codex 复核补充)。
- 局限:验证的是 Router / broker 链路和鉴权,不是 pi 插件调用;插件由阶段 6 的测试与第 5 节的真实模型链路覆盖。

## 4. 不调用模型的集成测试(真实 herdr,假 agent)

新文件 `tests/test_integration_rollback.py`,独立命名会话,结束时清理。5 个角色 × 2 个 IP 中按需 spawn。

通用断言(Codex 审核后补充):用来证明"没收到"的 agent 必须已 spawn、处于可接收状态、日志在发起前为空;收到的一方按**精确内容和次数**核对日志(去掉 ready 行后,只含预期的模板文字各一次),不只看状态。

1. **回退闭环**:spawn dv_uart(`script:dv_bug,-`)、rtl_uart(`script:rtl_fixed`)、sw_uart(只记录)。触发 dv_uart → `dv_bug` 到 rtl_uart → rtl_uart 自动回 `rtl_fixed` → dv_uart 收到。断言:两条都 DELIVERED;rtl_uart 日志恰好一次 `dv_bug` 文字;dv_uart 日志恰好是触发文字 + 一次 `rtl_fixed` 文字;sw_uart 日志为空。
2. **多条出边不串**:dv_uart(`script:dv_done,dv_bug`)触发两次。sw_uart 日志恰好一次 `dv_done` 文字、没有 `dv_bug` 文字;rtl_uart 反之。
3. **多 IP 并发隔离**:dv_uart、dv_gpio 都用 `script:dv_bug` 并设同一个屏障。两边都触发、都在日志里写了"等待屏障"后,创建屏障文件,两边几乎同时发送。断言:两条消息的 src / dst / text / queue_seq 分别是 (dv_uart, rtl_uart, uart 文字, 1) 和 (dv_gpio, rtl_gpio, gpio 文字, 1),都 DELIVERED;rtl_uart、rtl_gpio 日志各恰好一次本 IP 的文字,没有对方 IP 的。
4. **一个 IP 的队列暂停不影响另一个**(Codex 审核后修订):broker 先不启动。两边都发 `dv_bug` 入队;然后 `agent stop` rtl_gpio;再启动 broker。断言:dv_gpio 的消息判 TARGET_MISSING、rtl_gpio 队列暂停(审计 `QUEUE_PAUSED`);dv_uart 的消息照常 DELIVERED 到 rtl_uart。这条测的是"已入队、投递时目标消失"的路径。
5. **目标不可用时 Router 同步拒绝**:rtl_gpio 未 spawn(`target_missing`)或已 stop(`target_not_running`)时,dv_gpio 发 `dv_bug` 被拒,不入队,rtl_uart 日志不变。这条测的是"入队前拒绝"的路径,和第 4 条分开汇报。

已有单元测试覆盖的(方向错误、跨 IP 在构造上不可能等)不重复。

## 5. 真实模型链路(需用户另行同意;只调用 fnx_dv 的模型)

会话独立,uart 一个 IP。dv_uart = 真实 fnx_dv(`--no-builtin-tools`,临时工作目录);rtl_uart = 假 agent(`script:rtl_fixed`);sw_uart = 真实 fnx_sw(用户要求;原方案为只记录的假 agent)。证明 dv 没选错边:审计与队列里没有发给 sw_uart 的消息,fnx_sw 会话文件里没有任何消息、模型未被调用。

1. 操作员给 fnx_dv 输入:`假设你在 uart ip 的 UVM 验证中发现了 RTL 问题，请用 a2a_send 通知 RTL 智能体。`
2. fnx_dv 应在 `dv_done` 和 `dv_bug` 中选 `dv_bug` → rtl_uart。
3. 假 rtl 自动回 `rtl_fixed` → dv_uart。
4. fnx_dv 回复"收到"。

判定通过:审计里 `dv_bug`、`rtl_fixed` 各送达一次;sw_uart 没有收到任何消息;fnx_dv 末行"收到";工作目录为空;fnx 会话文件记录 sophnet / DeepSeek-Flash。fnx_dv 选对边时,fnx_sw 的模型不会被调用。

## 6. 范围外

- spec / arch / rtl 的真实智能体(未安装);它们的插件加载、启动脚本前提留到安装后。
- 大规模并发(150 agent)、改拓扑后自动重启 agent:阶段 8。

## 7. 约束

沿用阶段 6:不动用户与 Codex 的会话;只往 fnx 目录加外部文件;模型调用只在第 5 节,且须用户同意;fnx 会话文件保留作证据;单元测试与真实 herdr 测试分开汇报。
