# herdr 安装与启动 Codex / Claude Code(macOS)

> 说明:本文只整理流程,**未实际安装或执行任何命令**。
> 信息来源:
> - https://github.com/herdrdev/herdr/blob/master/README.zh-CN.md
> - https://herdr.dev/zh-cn/docs/(install / quick-start / agents / concepts / keyboard)
>
> 文档抓取日期:2026-10-08。命令以官方文档为准,执行前建议再对照一次官网。

## 1. herdr 是什么

终端里的"智能体复用器"(类似 tmux,但面向 coding agent):

- 一眼看到每个 agent 的状态:`blocked` / `working` / `done` / `idle` / `unknown`
- client/server 架构:关闭终端或 SSH 断线后,后台 server 与 agent 继续运行,再执行 `herdr` 即可重新连接
- 提供 Socket API / CLI,agent 之间可以互相读写 pane(这是后续学习 A2A 的切入点)
- 单个 Rust 二进制,无需 Electron;Apache 2.0 许可

## 2. 安装(macOS)

支持 macOS Intel 与 Apple Silicon。三选一即可。

### 方式 A:官方脚本

```bash
curl -fsSL https://herdr.dev/install.sh | sh
```

### 方式 B:Homebrew(Mac 上最省心,便于升级/卸载)

```bash
brew install herdr
```

### 方式 C:mise

```bash
mise use -g herdr
```

### 方式 D:手动下载

从 https://github.com/herdrdev/herdr/releases 下载对应架构(macOS Intel / macOS Apple Silicon)的二进制,放到 `PATH` 中(如 `/usr/local/bin`)。

### 验证安装

```bash
herdr
```

能进入 herdr 界面即成功。(文档的验证方式即直接启动 `herdr`。)

### 更新

```bash
herdr update                      # 内置更新
herdr channel set preview         # 切到预览版
herdr channel set stable          # 回到稳定版
```

> 官方文档未给出卸载说明。若用 Homebrew 安装,可用 `brew uninstall herdr`。

## 3. 前置条件检查(安装前自查)

herdr 不自带 agent,需要你本机已能直接运行:

```bash
claude --version
codex --version
```

两个命令都能输出版本,说明 Claude Code 与 Codex 已装好、已登录。

## 4. 启动 herdr

在你的项目目录中:

```bash
cd /path/to/your/project
herdr
```

- 首次运行会启动(或连接)默认后台会话 `herdr`。
- 会话里没有 workspace 时,herdr 自动创建一个。
- workspace 是项目级容器,建议"一个仓库/任务 = 一个 workspace"。

## 5. 在 herdr 中启动 Claude Code 和 Codex

herdr 的 pane 就是一个普通终端,**直接在里面敲 agent 的命令即可**,herdr 会自动识别 agent 类型,无需额外配置。

### 推荐布局:左右分屏,左 Claude、右 Codex

1. 进入 herdr 后,在第一个 pane 中运行:
   ```bash
   claude
   ```
2. 按 `ctrl+b`(进入 prefix 模式),再按 `v`,向右分出新 pane。
3. 在新 pane 中运行:
   ```bash
   codex
   ```
4. 用 `ctrl+b` 然后 `h/j/k/l` 在两个 pane 间切换焦点。

也可以用 `ctrl+b` `c` 新开一个 tab,每个 tab 放一个 agent。

### 可选:安装官方集成(增强状态识别 / 重启后恢复会话)

```bash
herdr integration install claude
herdr integration install codex
```

- 不装集成也能工作:herdr 靠前台进程和屏幕内容判断状态。
- 装了之后,server 重启后可恢复会话。
- 状态显示异常时可诊断:
  ```bash
  herdr agent explain
  ```
- Codex 注意:活跃轮次与响应结束后,标题和输入框可能一样,herdr 会退回 `unknown` 状态,属已知现象。

## 6. 常用快捷键

默认 prefix 为 `ctrl+b`:先按 prefix,松开,再按动作键。

| 动作 | 按键 |
| --- | --- |
| 向右分屏 | `prefix` `v` |
| 向下分屏 | `prefix` `-` |
| 新建 tab | `prefix` `c` |
| 切换 pane | `prefix` `h/j/k/l` |
| 切换 workspace | `prefix` `w` |
| 全屏/还原当前 pane | `prefix` `z` |
| 关闭 pane | `prefix` `x` |
| 下一个 / 上一个 tab | `prefix` `n` / `p` |
| 新建 workspace | `prefix` `shift+n` |
| 侧边栏开关 | `prefix` `b` |
| 复制模式 | `prefix` `[` |
| **分离(detach)** | `prefix` `q` |

不想先按 prefix 的直连键(`ctrl+alt` 系):

- 切换 pane:`ctrl+alt+h/j/k/l`
- 新建 tab:`ctrl+alt+c`
- 分屏:`ctrl+alt+d` / `ctrl+alt+shift+d`
- 全屏:`ctrl+alt+z`

> macOS 终端里 `alt` 需要设为 Option-as-Meta(Terminal.app / iTerm2 / Ghostty 各有设置),否则 `ctrl+alt` 组合可能不生效。这一条是我的补充,非官方文档原话。

所有按键均可在配置中自定义,见 https://herdr.dev/zh-cn/docs/configuration/ 。

## 7. 分离、恢复、停止

| 目的 | 操作 |
| --- | --- |
| 分离,agent 继续跑 | `ctrl+b` `q`,或直接关闭终端窗口 |
| 重新连接 | 再次运行 `herdr` |
| 彻底停止会话和所有 agent | `herdr server stop` |

## 8. 核心概念速记

- **Workspace**:项目级容器,包含 tab 和 pane
- **Tab**:workspace 内的一组布局
- **Pane**:真正跑进程的终端,可通过 CLI 读取内容、发送输入、关闭
- **Agent**:herdr 通过前台进程 + 屏幕内容 + 可选集成识别出的 pane 内程序
- **Session**:server 上的持久命名空间,默认名 `herdr`,可有多个独立会话
- **Server/Client**:server 持有 pane 与进程状态;client 是连接它的终端 UI,可同时连多个

## 9. 完整流程清单(照着做)

```bash
# 0. 前置检查
claude --version && codex --version

# 1. 安装(任选其一)
brew install herdr
# 或 curl -fsSL https://herdr.dev/install.sh | sh

# 2. 进入项目并启动
cd /path/to/your/project
herdr

# 3. 在 herdr 的 pane 中:
#    claude                 # 第一个 pane
#    ctrl+b 然后 v          # 向右分屏
#    codex                  # 第二个 pane

# 4. (可选)安装集成
herdr integration install claude
herdr integration install codex

# 5. 离开 / 回来 / 停止
#    ctrl+b 然后 q          # 分离
#    herdr                  # 回来
#    herdr server stop      # 停止
```

## 10. 下一步(面向 A2A 学习)

文档中与 agent 间通信直接相关、值得下一步研究的部分:

- Socket API:https://herdr.dev/zh-cn/docs/socket-api/
- 连接机器(多机管理,`herdr machine add <host>`):https://herdr.dev/zh-cn/docs/connecting-machines/
- 会话状态:https://herdr.dev/zh-cn/docs/session-state/
- 插件:https://herdr.dev/zh-cn/docs/plugins/

## 11. 待确认项

- 官方文档抓取是摘要形式,未看到 `herdr integration install` 的详细副作用(会改哪些配置文件)。装集成前建议先读 agents 页面原文。
- 卸载方式官方未写明。
