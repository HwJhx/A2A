"""把 pi 插件安装进 fnx 的 agent 目录(路线图 313;19 号方案"插件改为软链接安装")。

安装 = 在 <agent 目录>/agent/extensions/ 下建软链接 a2a.ts -> 仓库里的 pi-extension/a2a.ts,
所有智能体共用仓库里的同一个文件,改插件后不会漏更新。实测 fnx(pi 0.79.10)能加载软链接形式的扩展。

只往 fnx 的 agent 目录加外部文件,不改 fnx 的程序、启动脚本、libexec。

正式部署改用 pi 的在线安装(`fnx_dv install git:github.com/HwJhx/A2A@<标签>`,仓库根目录 package.json 是 pi 包清单);
两种方式不能同时用在同一个 fnx 上(会加载两份插件)。所以这里也读 <agent 目录>/agent/settings.json 的 packages:
status 报告指向本仓库的在线包;已有在线包时软链接 install 拒绝。反过来(先软链接、后在线安装)挡不住,
pi 的 install 不经过这里,只能靠 status 报出"两份都装了"。

pi 还会加载**项目级**配置:<fnx 启动目录>/<configDir>/(fnx 的 configDir 是 .forenyx)下的 settings.json 的 packages、
git/、npm/ 里的安装副本、extensions/ 里的扩展。软链接装在 agent 级,对所有项目生效,这里无法知道有哪些项目;
用 --project-dir 指定要一并检查的项目目录(可多个),不指定时只保证 agent 级。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlparse
from typing import Any, Dict, List, Sequence

PLUGIN_SOURCE = Path(__file__).resolve().parents[2] / "pi-extension" / "a2a.ts"
REPO_ROOT = PLUGIN_SOURCE.parents[3]          # 仓库根目录(package.json 即 pi 包清单)
# 本仓库在 GitHub 上的身份(小写的 主机/路径);在线安装来源按它识别
REPO_IDS = ("github.com/hwjhx/a2a",)
PACKAGE_NAME = "a2a-pi-plugin"               # 仓库根目录 package.json 的 name;按它识别 pi 装上的副本
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


def _dir_entries(d: Path) -> List[Path]:
    """pi 0.79.10 resolveExtensionEntries:目录的 package.json 有 pi.extensions 就用其中存在的入口,
    否则 index.ts,再否则 index.js;都没有返回空。"""
    try:
        manifest = json.loads((d / "package.json").read_text(encoding="utf-8"))
        exts = manifest.get("pi", {}).get("extensions") if isinstance(manifest, dict) else None
    except FileNotFoundError:
        exts = None
    except (OSError, ValueError, AttributeError):
        exts = None
    if isinstance(exts, list):
        found = [d / e for e in exts if isinstance(e, str) and (d / e).exists()]
        if found:
            return found
    for name in ("index.ts", "index.js"):
        if (d / name).exists():
            return [d / name]
    return []


def _extension_entries(ext: Path) -> List[Path]:
    """extensions/ 里 pi 可能加载的入口。pi 0.79.10 有两套规则:fnx CLI 实际走包管理器的
    collectAutoExtensionEntries(extensions/ 本身有清单 / index 时只用它们,否则顶层 .ts/.js + 子目录入口);
    SDK 的 discoverExtensionsInDir 则不看根目录清单,总是顶层 .ts/.js + 子目录入口。这里取两者的并集:
    宁多勿少,最坏是多拒绝一次安装,不会漏检。也不跳过点开头的条目。"""
    if not ext.is_dir():
        return []
    found = list(_dir_entries(ext))
    for f in sorted(ext.iterdir()):
        if f.is_dir():
            found += _dir_entries(f)
        elif f.suffix in (".ts", ".js") and f not in found:
            found.append(f)
    return found


def _conflicts(ext: Path) -> List[str]:
    """extensions/ 里其他也注册 a2a_send 的扩展(同名工具冲突),含子目录形式的扩展。入口是本仓库插件
    (真实路径相同)也算;按 `name: "a2a_send"` 文本匹配是尽力而为的检查:能发现别处复制来的旧版插件,
    识别不了动态拼出的名字。"""
    found = []
    link = ext / LINK_NAME
    for f in _extension_entries(ext):
        if f == link:
            continue                               # a2a.ts 本身由调用方按 state 单独判断
        try:
            if os.path.realpath(f) == str(PLUGIN_SOURCE) or _REGISTERS.search(f.read_text(errors="replace")):
                found.append(str(f))
        except OSError as exc:
            found.append(f"{f}(读取失败:{exc})")
    return found


def _git_identity(source: str) -> str:
    """git 来源 → 小写的"主机/用户/仓库"(尽力而为的补充判断;主要靠下面按安装结果识别)。
    去掉 git: 前缀、协议(不分大小写)、用户名、端口、#ref、@ref(含斜杠的分支名)、.git;
    路径只取前两段(用户/仓库)。不是 git 来源返回空串。"""
    src = source.strip()
    if src[:4].lower() == "git:":
        src = src[4:].strip()
    elif not re.match(r"^(https?|ssh|git)://", src, re.I):
        return ""
    src = src.split("#", 1)[0]                              # #标签
    src = re.sub(r"^(https?|ssh|git)://", "", src, flags=re.I)
    src = re.sub(r"^[^@/]+@", "", src)                      # git@github.com:… / user@host/…
    m = re.match(r"^([^/:]+)(?::(\d+)(?=/)|:)?/?(.*)$", src)  # 主机[:端口]/路径 或 主机:路径(scp 写法)
    if not m:
        return ""
    host, path = m.group(1), m.group(3)
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        return ""
    repo = re.sub(r"@.*$", "", parts[1])                    # 仓库名后的 @标签 / @分支(分支里的斜杠已在后面的段里)
    repo = re.sub(r"\.git$", "", repo)
    return f"{host}/{parts[0]}/{repo}".lower()


def _package_source(entry: Any) -> str:
    """settings.json 的 packages 项:字符串,或带 source 字段的对象(过滤资源时的写法)。"""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and isinstance(entry.get("source"), str):
        return entry["source"]
    return ""


def _package_name(directory: str) -> str:
    try:
        data = json.loads(Path(directory, "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return data.get("name", "") if isinstance(data, dict) and isinstance(data.get("name"), str) else ""


def _installed_copies(root: Path) -> List[str]:
    """pi 实际装上的本插件:git 包克隆在 <root>/git/<主机>/<路径>,npm 包在 <root>/npm/node_modules/
    (root 是 agent 目录,或项目的 <configDir> 目录,两者布局相同)。
    按 package.json 的 name 识别,不依赖来源的写法(各种 git URL、#ref、@ref、端口……都不影响)。"""
    found = []
    for base, max_depth in ((root / "git", 4), (root / "npm" / "node_modules", 2)):
        if not base.is_dir():
            continue
        base_depth = len(base.parts)
        for dirpath, dirnames, filenames in os.walk(base):
            depth = len(Path(dirpath).parts) - base_depth
            if "package.json" in filenames and _package_name(dirpath) == PACKAGE_NAME:
                found.append(dirpath)
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if d not in ("node_modules", ".git")] if depth < max_depth else []
    return sorted(found)


def _local_source_path(root: Path, source: str) -> str:
    """本地来源(绝对 / 相对路径、file:// URL)→ 真实路径。相对路径按 settings.json 所在目录**按字面**拼接规整
    (与 pi 用的 Node path.resolve 一致,不先展开软链接再处理 ..)。realpath 不会因路径不存在而抛错。"""
    if source[:7].lower() == "file://":
        source = unquote(urlparse(source).path)
    local = os.path.normpath(os.path.join(str(root), os.path.expanduser(source)))
    return os.path.realpath(local)


def config_dir_name(agent_dir: str) -> str:
    """fnx 的项目级配置目录名:取 <agent 目录>/libexec/package.json 的 piConfig.configDir(与 pi 相同),缺省 .pi。"""
    try:
        data = json.loads((Path(os.path.expanduser(agent_dir)) / "libexec" / "package.json").read_text(encoding="utf-8"))
        name = data["piConfig"]["configDir"]
        if isinstance(name, str) and name and "/" not in name:
            return name
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return ".pi"


def _project_roots(agent_dir: str, project_dirs: Sequence[str]) -> List[Path]:
    name = config_dir_name(agent_dir)
    roots = []
    for d in project_dirs:
        project = Path(os.path.expanduser(d))
        if not project.is_dir():
            raise PluginError(f"项目目录 {project} 不存在")
        roots.append(project.resolve() / name)
    return roots


def _project_findings(agent_dir: str, project_dirs: Sequence[str]) -> List[str]:
    """项目级已有的本插件(在线包或注册 a2a_send 的扩展)。读不了时抛 PluginError(fail-closed)。"""
    found = []
    for base in _project_roots(agent_dir, project_dirs):
        if not base.is_dir():
            continue
        found += online_packages(base)
        ext = base / "extensions"
        if os.path.lexists(ext / LINK_NAME):
            found.append(str(ext / LINK_NAME))
        found += _conflicts(ext)
    return list(dict.fromkeys(found))


def online_packages(root: Path) -> List[str]:
    """已在线安装的本插件:settings.json 的 packages 里指向本仓库的来源,加上 pi 实际装上的副本。
    settings 读不了 / 格式不对时抛 PluginError,调用方不能当作"没有"(fail-closed)。"""
    settings = root / "settings.json"
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as exc:
        raise PluginError(f"读不了 {settings}:{exc};无法确认是否已在线安装插件")
    packages = data.get("packages", []) if isinstance(data, dict) else None
    if not isinstance(packages, list):
        raise PluginError(f"{settings} 的 packages 不是列表;无法确认是否已在线安装插件")
    found = []
    ours = {str(REPO_ROOT), str(PLUGIN_SOURCE), str(PLUGIN_SOURCE.parent)}
    for entry in packages:
        src = _package_source(entry)
        if not src:
            continue
        if _git_identity(src) in REPO_IDS:
            found.append(src)
            continue
        if src.startswith("npm:") or src[:4].lower() == "git:" or re.match(r"^(https?|ssh|git)://", src, re.I):
            continue                                  # 远程来源:是否装的本插件由 _installed_copies 判断
        real = _local_source_path(root, src)
        if real in ours or (os.path.isdir(real) and _package_name(real) == PACKAGE_NAME):
            found.append(src)
    return found + [p for p in _installed_copies(root) if p not in found]


def status(agent_dir: str, project_dirs: Sequence[str] = ()) -> Dict[str, Any]:
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
    try:
        online = online_packages(root)
        result["online_packages"] = online       # 在线安装(pi 包)的来源
    except PluginError as exc:
        online = []
        result["online_packages_error"] = str(exc)
    if online and state == "not_installed":
        result["state"] = "online_package"         # 只有在线安装
    if online and state != "not_installed":
        result["duplicate"] = "软链接与在线安装同时存在,会加载两份插件;请卸载其中一种"
    if project_dirs:
        try:
            project = _project_findings(agent_dir, project_dirs)
            result["project_packages"] = project      # 项目级已有的本插件
        except PluginError as exc:
            project = []
            result["project_packages_error"] = str(exc)
        if project and (online or state != "not_installed"):
            result["duplicate"] = "agent 级与项目级同时装了本插件,在该项目里会加载两份;请卸载其中一份"
    else:
        result["project_scope"] = "未检查项目级安装(<项目>/" + config_dir_name(agent_dir) + "/);用 --project-dir 指定"
    if link.is_symlink():
        result["link_target"] = os.readlink(link)
        result["target_exists"] = link.exists()
    return result


def install(agent_dir: str, project_dirs: Sequence[str] = ()) -> Dict[str, Any]:
    root = _agent_root(agent_dir)
    ext = root / "extensions"
    link = ext / LINK_NAME
    if not PLUGIN_SOURCE.is_file():
        raise PluginError(f"仓库里的插件 {PLUGIN_SOURCE} 不存在")
    if _ours(link):
        return dict(status(agent_dir, project_dirs), changed=False)
    if os.path.lexists(link):
        raise PluginError(f"{link} 已存在且不是指向本仓库插件的软链接,不覆盖(可能是别人的同名插件);"
                          f"请人工确认后处理")
    conflicts = _conflicts(ext)
    if conflicts:
        raise PluginError(f"{ext} 里已有扩展也注册 {TOOL_NAME}:{', '.join(conflicts)};不安装")
    online = online_packages(root)                 # 读不了 settings 时直接报错,不安装
    if online:
        raise PluginError(f"已在线安装本插件({', '.join(online)}),再装软链接会加载两份;不安装。"
                          f"要改用软链接,先用 fnx 的 remove 命令卸载在线包")
    project = _project_findings(agent_dir, project_dirs)
    if project:
        raise PluginError(f"项目级已装了本插件({', '.join(project)}),再装 agent 级软链接会在该项目里加载两份;不安装")
    if not ext.exists():
        ext.mkdir()
        (root / CREATED_MARKER).write_text(_identity(ext) + "\n")
    link.symlink_to(PLUGIN_SOURCE)
    return dict(status(agent_dir, project_dirs), changed=True)


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
