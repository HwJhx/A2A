# a2a:基于 herdr 的多智能体编排框架

当前阶段:**herdr 调用层**(`HerdrClient`)和**启动命令构造**(`launcher`)。
拓扑、身份、注册表、路由、broker 等后续按 `../claude/04-design-python-framework.md` 逐层添加。

没有第三方依赖,只用标准库。代码兼容 Python 3.9(Mac)和 3.10(虚拟机)。

## 目录

```
a2a/
├── pyproject.toml
├── src/a2a/
│   ├── errors.py         # 异常类型(按 herdr 的 error.code 映射)
│   ├── herdr_client.py   # HerdrClient:herdr CLI 的薄封装
│   └── launcher.py       # 覆盖 exec 的启动命令 + 启动脚本预检
└── tests/
    ├── test_herdr_client.py   # 单元测试(假 runner,不需要 herdr)
    ├── test_launcher.py       # 单元测试
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
- 拓扑、注册表、Router、Broker 都还没写。
