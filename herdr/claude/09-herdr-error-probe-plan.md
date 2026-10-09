# herdr 错误码"未提交"语义实测方案

> 状态:**已执行**(2026-10-09,用户同意后在 VM 运行,herdr 0.9.3)。结果见 §7,已写回 08 §5。
> 目的:回答 `08-protocol.md` §5 的"待验证"项——herdr 返回下列错误时,prompt 的文字**有没有可能已经写进目标**。
> 结论写回 08 §5,把"待验证"改成"是"或"否"。验证前 Broker 一律按 `DELIVERY_UNCERTAIN` 处理。

## 1. 要回答的问题

| 错误码 | 要确认的事 |
| --- | --- |
| `agent_not_ready` | 目标 pane 的前台进程不是 agent 时,herdr 是否**一个字节都没写**进 pane |
| `agent_blocked` | 目标处于 `blocked` 时,herdr 是否一个字节都没写 |
| `server_not_running` | 连不上 herdr 服务时,是否可能已写入 |
| `agent_not_found`(调用后才收到) | 调用过程中目标 pane 被关闭时,是否可能已写入一部分 |

## 2. 判定依据(不调用任何模型)

不用真实的 fnx,改用一个**假 agent**:一个记录器程序,以 `pi` 的名字运行(和 fnx 的识别方法一样,用 `exec -a pi`)。它把收到的每个字节连同时间戳追加写进一个日志文件。

- herdr 认为它是 pi agent,可以对它执行 `agent prompt`。
- **日志文件就是真相**:错误返回后,日志里有没有这条 prompt 的文字,就能确定"写没写进去"。比读屏幕可靠,不受全屏界面和历史滚动影响。
- 每次 prompt 用唯一的文字(带序号),避免和上一次混淆。

整个过程**不会启动 fnx,也不会产生任何模型调用**。

## 3. 用例

| # | 错误码 | 怎么造出来 | 看什么 |
| --- | --- | --- | --- |
| 1 | — | **对照组**:假 agent 正常空闲时 prompt | 日志里应出现文字;确认判定方法有效,并记录假 agent 的状态是 `idle` 还是 `unknown` |
| 2 | `agent_not_ready` | 假 agent 注册后退出,让 pane 前台变成一个同样记录输入的 `cat`(argv[0] 不是 pi) | 错误返回后,`cat` 的记录里是否有文字 |
| 3 | `agent_blocked` | 让假 agent 在屏幕上打印 pi 的审批界面文字,看 herdr 是否判为 `blocked`;若是,再 prompt | 日志里是否有文字。**若假 agent 造不出 `blocked`,本项停下,另行请示**(真实造 blocked 需要 fnx 发起工具调用,会调用模型) |
| 4 | `server_not_running` | 停掉本次探测会话的 herdr 服务后 prompt | 错误码与退出码;服务都不在,理论上不可能写入,只记录现象 |
| 5 | `agent_not_found`(静态) | prompt 一个不存在的 pane id | 错误码;不可能写入,作为对照 |
| 6 | `agent_not_found`(竞态) | 一边连续 prompt,一边关闭目标 pane,重复 20 次 | 收到 `agent_not_found` 的那几次,日志里有没有对应文字。**竞态不一定能复现**,复现不了就如实记"未复现",结论保持"待验证" |
| 7 | 附带 | 每个用例前后记录 `agent get` 里的 `state_change_seq` | 为 08 §6 的候选依据收集数据,**不据此下任何结论** |

## 4. 执行环境与安全

- 在 VM 里新建独立命名会话 `a2a_errprobe`,用后台 tmux 承载(和阶段 1 的做法相同)。
- **不碰**:你的默认会话、`test1`、`a2a_verify`,以及 Codex 的 `a2a_codex_verify`。
- 用例 4 只停 `a2a_errprobe` 自己的服务。
- 假 agent 和记录文件放在 `~/A2A_Test/errprobe/`。
- 结束后:`herdr session stop/delete a2a_errprobe`,杀掉对应 tmux,删除 `~/A2A_Test/errprobe/`,并检查没有残留进程和会话。
- 预计耗时:10~15 分钟。

## 5. 产出

- 本文追加第 6 节"实测结果":每个用例的原始现象(错误码、退出码、日志是否有文字、`state_change_seq` 变化)。
- 根据结果修订 08 §5 的分类表(只改"待验证"那几行,并在 08 §11 记一条修订)。
- 若结论让某个错误码从"不确定"变为"可证明未提交",它就能走 `RETRYING` / `TARGET_BLOCKED` / `TARGET_MISSING`,Broker 的自动处理范围随之变大。

## 6. 已知局限

- 假 agent 不是真实的 fnx:herdr 写入 pane 的方式与目标是谁无关,所以"写没写"的结论可以推广;但**状态判定**(例如 `blocked` 怎么识别)可能和真实 fnx 不同。
- 竞态用例只能说明"在 20 次里看到/没看到",不能证明一定不会发生。

## 7. 实测结果(2026-10-09,VM,herdr 0.9.3)

脚本:`review-probes/probe_herdr_errors.py`(假 agent:`review-probes/fake_agent_logger.py`)。共跑两轮:

- 第一轮发现计划外的错误码 `agent_prompt_failed`,且有返回失败但已写入的情况;但该轮把"并发发送"和"关闭 pane"两个因素混在一起,`agent get` 的输出解析也有缺陷(截断后再解析)。
- 第二轮修正解析,并把两个因素拆成 C6a(只并发)和 C6b(只关闭)。**下表是第二轮的结果。**

两轮结束后都确认:会话 `a2a_errprobe` 已删除,无假 agent 进程残留,`a2a_codex_verify` 保持运行未受影响。

| 用例 | 构造 | herdr 返回 | 耗时 | 文字是否写入目标 |
| --- | --- | --- | --- | --- |
| C1 对照 | 识别为 pi 的假 agent,状态 `idle` | 成功 | 312 ms | **是**(判定方法有效) |
| C2a | 前台 argv0=`notpi`,用 `report-agent` 声明为 pi | `agent_not_ready`:"agent w1:p3 is no longer the pane foreground process" | 6 ms | 否 |
| C2b | pi 被识别后退出,前台换成 `notpi` | `agent_not_found`(agent 退出后立即被注销,不是 `agent_not_ready`) | 6 ms | 否 |
| C3a | 识别为 pi,`report-agent --state blocked` | `agent_blocked`:"… is blocked and requires interactive input" | 2 ms | 否 |
| C3b | 前台 `notpi`,`report-agent` 声明 pi 且 blocked | `agent_blocked` | 6 ms | 否 |
| C5 | 不存在的 pane `w99:p99` | `agent_not_found` | 5 ms | (无目标) |
| C6a | 8 个 prompt **并发**发给同一 agent,不关闭,5 轮共 40 次 | 全部成功 | — | 40/40 是 |
| C6b | 单线程连续发送,中途**关闭 pane**,20 轮,每轮统计关闭前后的最后几次调用,共 46 次 | 成功 9 次;`agent_prompt_failed` 17 次:"PTY actor closed during input submission";`agent_not_found` 20 次 | — | 成功 9/9 是;**`agent_prompt_failed` 17/17 是**;`agent_not_found` 0/20 |
| C4 | 停掉本次会话的服务后发送 | `server_not_running` | 2 ms | 否 |


**结论:**

1. `agent_blocked`、`agent_not_ready`、`agent_not_found`、`server_not_running` 四个错误码**都没有写入**,而且都在几毫秒内返回,符合"写入前检查"的行为。
2. **`agent_prompt_failed` 返回时文字已经写入**(17/17)。它出现在写入过程中 pane 被关闭的时候。这是"返回错误但消息已经送达"的直接证据,必须走 `DELIVERY_UNCERTAIN`。
3. 并发向同一 agent 发送不会报错,文字都会写入(C6a)。这与 08 规则 10 的前提一致:顺序要由 Broker 保证。
4. agent 进程退出后,herdr 立即注销它,随后返回的是 `agent_not_found`,不是 `agent_not_ready`。`agent_not_ready` 只在"herdr 仍认为有 agent,但前台进程不是它"时出现(本次用 `report-agent` 构造)。
5. `state_change_seq`:C1 发送前后都是 1(假 agent 不会进入 `working`,所以没有变化);用 `report-agent` 改状态时会递增。**只记录,不据此下结论**(08 §6)。

**局限:**

- 结论只对 herdr 0.9.3 成立,升级后要重跑脚本。
- 样本有限(每种 2~22 次),说明观察到的行为,不是源码层面的证明。
- 未测"服务在处理请求中途退出"。
- `blocked` 是用 `report-agent` 人为报告的,真实 fnx 的 `blocked` 由 herdr 识别;两者都走同一个"是否 blocked"检查,所以"写入前拒绝"的结论应能推广,但识别本身没有测。
