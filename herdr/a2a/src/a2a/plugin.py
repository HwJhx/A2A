"""把 pi 插件安装进 fnx 的 agent 目录(路线图 313;19 号方案"插件改为软链接安装")。

安装 = 在 <agent 目录>/agent/extensions/ 下建软链接 a2a.ts -> 仓库里的 pi-extension/a2a.ts,
所有智能体共用仓库里的同一个文件,改插件后不会漏更新。实测 fnx(pi 0.79.10)能加载软链接形式的扩展。

只往 fnx 的 agent 目录加外部文件,不改 fnx 的程序、启动脚本、libexec。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List

PLUGIN_SOURCE = Path(__file__).resolve().parents[2] / "pi-extension" / "a2a.ts"
LINK_NAME = "a2a.ts"
# 记录 extensions/ 是本命令创建的(内容是该目录的 设备号:inode);放在 extensions/ 之外,免得被当成扩展加载
CREATED_MARKER = ".a2a-created-extensions"
TOOL_NAME = "a2a_send"
# 注册同名工具的写法(pi.registerTool({ name: "a2a_send", ... }));只读取该工具的扩展不算冲突
_REGISTERS = re.compile(r"""name\s*:\s*["'`]a2a_send["'`]""")


class PluginError(RuntimeError):
    pass


def _agent_root(agent_dir: str) -> Path:
    root = Path(os.path.expanduser(agent_dir)) / "agent"
    if not root.is_dir():
        raise PluginError(f"{root} 不存在:--agent-dir 应是 fnx 的目录(例如 ~/.forenyx/fnx_dv),"
                          f"且该 fnx 至少运行过一次")
    return root


def _ours(link: Path) -> bool:
    return link.is_symlink() and Path(os.readlink(link)) == PLUGIN_SOURCE


def _identity(path: Path) -> str:
    st = path.stat()
    return f"{st.st_dev}:{st.st_ino}"


def _created_by_us(root: Path, ext: Path) -> bool:
    """标记记录的就是现在这个 extensions/ 目录:目录被外部删除后重建(inode 变了)就不算本命令创建的。"""
    try:
        return (root / CREATED_MARKER).read_text().strip() == _identity(ext)
    except FileNotFoundError:
        return False


def _conflicts(ext: Path) -> List[str]:
    """extensions/ 里其他也注册 a2a_send 的扩展(同名工具冲突)。按 `name: "a2a_send"` 文本匹配,
    是尽力而为的检查:能发现别处复制来的旧版插件,识别不了动态拼出的名字。"""
    found = []
    if not ext.is_dir():
        return found
    for f in sorted(ext.iterdir()):
        if f.name == LINK_NAME or f.suffix not in (".ts", ".js"):
            continue
        try:
            if _REGISTERS.search(f.read_text(errors="replace")):
                found.append(str(f))
        except OSError as exc:
            found.append(f"{f}(读取失败:{exc})")
    return found


def status(agent_dir: str) -> Dict[str, Any]:
    root = _agent_root(agent_dir)
    ext = root / "extensions"
    link = ext / LINK_NAME
    if _ours(link):
        state = "linked"                       # 已链接到本仓库
    elif link.is_symlink():
        state = "linked_elsewhere"
    elif link.exists():
        state = "regular_file"
    else:
        state = "not_installed"
    result: Dict[str, Any] = {"agent_dir": str(root.parent), "path": str(link), "state": state,
                              "source": str(PLUGIN_SOURCE), "conflicts": _conflicts(ext)}
    if link.is_symlink():
        result["link_target"] = os.readlink(link)
        result["target_exists"] = link.exists()
    return result


def install(agent_dir: str) -> Dict[str, Any]:
    root = _agent_root(agent_dir)
    ext = root / "extensions"
    link = ext / LINK_NAME
    if not PLUGIN_SOURCE.is_file():
        raise PluginError(f"仓库里的插件 {PLUGIN_SOURCE} 不存在")
    if _ours(link):
        return dict(status(agent_dir), changed=False)
    if os.path.lexists(link):
        raise PluginError(f"{link} 已存在且不是指向本仓库插件的软链接,不覆盖(可能是别人的同名插件);"
                          f"请人工确认后处理")
    conflicts = _conflicts(ext)
    if conflicts:
        raise PluginError(f"{ext} 里已有扩展也注册 {TOOL_NAME}:{', '.join(conflicts)};不安装")
    if not ext.exists():
        ext.mkdir()
        (root / CREATED_MARKER).write_text(_identity(ext) + "\n")
    link.symlink_to(PLUGIN_SOURCE)
    return dict(status(agent_dir), changed=True)


def uninstall(agent_dir: str) -> Dict[str, Any]:
    root = _agent_root(agent_dir)
    ext = root / "extensions"
    link = ext / LINK_NAME
    changed = False
    if _ours(link):
        link.unlink()
        changed = True
    elif os.path.lexists(link):
        raise PluginError(f"{link} 不是指向本仓库插件的软链接,不删除;请人工确认后处理")
    marker = root / CREATED_MARKER
    if ext.is_dir() and _created_by_us(root, ext) and not any(ext.iterdir()):
        ext.rmdir()                            # 只删本命令创建、且已空的目录
    if marker.exists() and not (ext.is_dir() and _created_by_us(root, ext)):
        marker.unlink()                        # 目录已删,或已不是当初创建的那个:标记作废
    return dict(status(agent_dir), changed=changed)
