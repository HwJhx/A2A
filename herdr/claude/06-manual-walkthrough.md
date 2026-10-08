# 手动走一遍:覆盖 `exec` 启动 `fnx_dv` / `fnx_sw`

> 目的:在虚拟机里自己动手验证 `04-design-python-framework.md` §2.3 的启动方式。
> 会话名用 `walk1`,避免和已有的 `test1` 混在一起。
> 所有命令都在虚拟机(`ubuntu`,192.168.139.41)里执行。
>
> 背景:herdr 按前台进程的 `argv[0]` 识别 agent。`fnx_*` 的进程名是 `forenyx-cli`,herdr 认不出。
> 启动时把启动脚本里最后那句 `exec` 换成 `exec -a pi`,让 `argv[0]` 变成 `pi`,herdr 就能自动识别。

---

## 1. 进虚拟机,启动一个命名会话

```bash
orb -m ubuntu
mkdir -p ~/A2A_Test && cd ~/A2A_Test
herdr session attach walk1
```

这会进入 `walk1` 的 herdr 界面,它和别的会话互不可见。这个终端先别动,之后的命令都在**另一个**虚拟机终端里执行。
要离开界面但保持运行:按 `ctrl+b`,再按 `q`。

## 2. 另开一个虚拟机终端,建两个 tab 并注入身份

在 Mac 上新开一个终端窗口:

```bash
orb -m ubuntu
herdr --session walk1 tab create --label dv --cwd "$HOME/A2A_Test" --env A2A_ROLE=dv --env A2A_IP=uart --no-focus
herdr --session walk1 tab create --label sw --cwd "$HOME/A2A_Test" --env A2A_ROLE=sw --env A2A_IP=uart --no-focus
herdr --session walk1 pane list
```

新会话里应该是:`w1:p1` 是自带的第一个 tab,`w1:p2` 是 `dv`,`w1:p3` 是 `sw`。以 `pane list` 的实际输出为准。

- [X] 完成

## 3.(可选)先用一个无关程序验证 herdr 看 `argv[0]`

```bash
herdr --session walk1 pane run w1:p1 'bash -c "exec -a pi sleep 600"'
sleep 3
herdr --session walk1 agent list
```

能看到 `w1:p1` 被识别为 `pi / idle`,就说明 herdr 认的是 `argv[0]`。然后停掉它:

```bash
herdr --session walk1 pane send-keys w1:p1 ctrl+c
```

- [X] 完成

## 4. 在 `dv` 的 pane 里用覆盖 `exec` 的方式启动 `fnx_dv`

**写法一(推荐,直接在 pane 里敲)**:在 herdr 界面里切到 `dv` tab,在那个 pane 里粘贴:

```bash
bash -c 'exec(){ builtin exec -a pi "$@"; }; source "$HOME/.forenyx/fnx_dv/bin/fnx_dv"'
```

**写法二(用命令发,注意引号要转义)**:

```bash
herdr --session walk1 pane run w1:p2 "bash -c 'exec(){ builtin exec -a pi \"\$@\"; }; source \"\$HOME/.forenyx/fnx_dv/bin/fnx_dv\"'"
```

屏幕上应出现 `ForeNyx CLI · fnx_dv v0.4.7` 的横幅。

- [X] 完成

## 5. 看 herdr 认不认它

```bash
herdr --session walk1 pane process-info --pane w1:p2
herdr --session walk1 agent list
```

预期:

- `process-info` 里 `argv` 是 `["pi"]`。
- `agent list` 里有 `w1:p2`,显示 `"agent":"pi"`、`"agent_status":"idle"`。**不用任何上报。**

- [X] 完成

## 6. 发一句话验证 `agent prompt`

```bash
herdr --session walk1 agent prompt w1:p2 "只回复你的名字:你是哪个智能体?"
sleep 8
herdr --session walk1 pane read w1:p2 --source visible --lines 30
```

预期:`agent prompt` 无报错,屏幕里出现它的回复。这一步会产生一次很小的模型调用。
如果这里报 `agent_not_ready`,说明启动方式没生效。

- [X] 完成

## 7. 验证身份变量能传进去

```bash
herdr --session walk1 pane send-text w1:p2 '! echo ENVCHK $A2A_ROLE $A2A_IP $HERDR_PANE_ID'
herdr --session walk1 pane send-keys w1:p2 enter
sleep 3
herdr --session walk1 pane read w1:p2 --source visible --lines 30 | grep ENVCHK
```

预期有一行 `ENVCHK dv uart w1:p2`。注意 `!` 前缀是 pi 的用户侧 bash,不经过模型。

- [X] 完成

## 8. 对 `sw` 重复第 4 到 7 步

把 `w1:p2` 换成 `w1:p3`,把 `fnx_dv` 换成 `fnx_sw`。预期横幅是 `fnx_sw v0.0.8`,环境变量是 `ENVCHK sw uart w1:p3`。

- [X] 完成

## 9.(可选)验证 dv 能不能给 sw 发消息

```bash
herdr --session walk1 agent prompt w1:p3 "uart已完成uvm验证,请执行驱动程序的开发" --wait --timeout 120000
```

这一步会让 `fnx_sw` 真的开始做事,可能耗时、耗费用,想省的话跳过。

- [ ] 完成(或已跳过)

## 10. 清理

```bash
herdr session stop walk1
herdr session delete walk1
```

如果不再需要旧的符号链接:

```bash
rm -f ~/A2A_Test/bin/pi
```

- [X] 完成

---

## 出问题时,把这几项贴出来

| 现象                       | 需要的输出                                                            |
| -------------------------- | --------------------------------------------------------------------- |
| 第 4 步没有出横幅          | `herdr --session walk1 pane read w1:p2 --source visible --lines 30` |
| 第 5 步`agent list` 为空 | `pane process-info` 的完整输出                                      |
| 第 6 步`agent_not_ready` | `pane process-info` 和 `agent get w1:p2` 的输出                   |

## 说明

- 这套启动方式的依据与局限见 `04-design-python-framework.md` §2.3。
- 它依赖启动脚本里**只有一处 `exec`** 且不使用 `$0` / `BASH_SOURCE`。`fnx_dv`、`fnx_sw` 满足;另外三个智能体(Spec / 架构 / RTL)不在这台虚拟机上,没有验证。
- 不要用 `herdr agent start --kind pi` 启动 `fnx_*`,它会去执行名为 `pi` 的命令。
