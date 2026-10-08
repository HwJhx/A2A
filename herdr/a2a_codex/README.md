# a2a_codex：阶段 3 HerdrClient

这是 A2A 项目的第一版 Herdr CLI Python 封装，当前不包含业务路由、拓扑、身份注册表和消息队列；同时包含 fnx 所需的 `exec -a pi` 启动器和 agent 重命名流程。

## 本地测试

```bash
cd /Users/jhx/Documents/code/personal/pi_agent/A2A/herdr/a2a_codex
PYTHONPATH=.. python3 -m unittest discover -s tests -v
```

## 使用

```python
from a2a_codex import HerdrClient, build_launch_command

client = HerdrClient("walk1")
created = client.create_tab(label="dv", cwd="/home/jhx/A2A_Test",
                            env={"A2A_ROLE": "dv", "A2A_IP": "uart"})
client.start_agent(created.pane_id,
    build_launch_command("/home/jhx/.forenyx/fnx_dv/bin/fnx_dv"))
client.wait_for_agent_detected(created.pane_id, timeout_s=60)
client.rename_agent(created.pane_id, "dv_uart")
client.wait_agent("dv_uart", until=("idle", "done"), timeout_ms=30000)
client.send_prompt("dv_uart", "只回复两个字:收到", wait=True, timeout_ms=120000)
print(client.read_agent("dv_uart", lines=60))
```

Herdr 的删除语义是 `close`，所以 `delete_workspace/tab/pane` 分别封装为对应的 `close` 命令；创建 pane 使用 `pane split`。`start_agent` 使用 `pane run`，这是为了支持 fnx 的 `exec -a pi` 启动方式，而不是使用 `herdr agent start --kind pi`。
