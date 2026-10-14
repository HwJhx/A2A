"""gui_demo 的拓扑辅助脚本(a2a 没有建拓扑、删拓扑的命令)。只用 a2a 自己的 TopologyStore 与文件锁;路径取自 A2A_TOPOLOGY。

用法:
  topology_tool.py init <json>          按给定内容新建拓扑(文件已存在则失败)
  topology_tool.py show                 输出 {"path", "project_id", "workspace_label", "ips"}
  topology_tool.py check <project_id>   只读:topology.yaml 与(若存在)topology.yaml.bak 都是普通文件、
                                        能解析、project_id 等于给定值;不满足退出码 3
  topology_tool.py remove <project_id> [--missing-ok]
                                        在 a2a 的同一把锁(topology.yaml.lock)下再核对一次,然后删 topology.yaml;
                                        .bak 归属对得上才删,否则(或删不掉)保留并给警告;.lock 永远不删(21 号文档 §11.2)。
                                        --missing-ok:topology.yaml 不存在也算成功(新建回滚用:init 可能在写文件之前就失败)

check / remove 输出 {"removed": [...], "warnings": [...]};主拓扑核对不通过时什么都不删,退出码 3。
"""
import json
import os
import stat
import sys
from pathlib import Path

import yaml

from a2a._fsutil import exclusive_lock
from a2a.paths import default_topology_path
from a2a.topology import TopologyError, TopologyStore


class NotOurs(Exception):
    pass


def _lexists(path: Path) -> bool:
    """只有"不存在"才返回 False;权限、I/O 等其他错误照常抛出(fail-closed)。"""
    try:
        os.lstat(path)
        return True
    except FileNotFoundError:
        return False


def _fail_at(step: str) -> None:
    """仅测试用:GUI_DEMO_FAIL_AT 等于给定步骤时模拟失败。"""
    if os.environ.get("GUI_DEMO_FAIL_AT") == step:
        raise OSError(f"测试注入:{step}")


def _owner(path: Path) -> str:
    """普通文件、能解析、返回 project_id;否则抛 NotOurs。"""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise NotOurs(f"{path} 不存在")
    if not stat.S_ISREG(st.st_mode):
        raise NotOurs(f"{path} 不是普通文件")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:  # 不是合法 UTF-8 / YAML 也算"不是本工程的"
        raise NotOurs(f"{path} 读不了:{exc}")
    pid = data.get("project_id") if isinstance(data, dict) else None
    if not isinstance(pid, str):
        raise NotOurs(f"{path} 里没有 project_id")
    return pid


def _check_main(path: Path, project_id: str) -> None:
    pid = _owner(path)
    if pid != project_id:
        raise NotOurs(f"{path} 属于工程 {pid},不是 {project_id}")


def _lock_path_ok(lock: Path) -> None:
    """加锁会跟随软链接:锁文件存在时必须是普通文件。"""
    try:
        st = os.lstat(lock)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(st.st_mode):
        raise NotOurs(f"{lock} 不是普通文件,不加锁")


def check(path: Path, project_id: str) -> dict:
    _check_main(path, project_id)
    bak = path.with_name(path.name + ".bak")
    if _lexists(bak):
        pid = _owner(bak)
        if pid != project_id:
            raise NotOurs(f"{bak} 属于工程 {pid},不是 {project_id}")
    return {"removed": [], "warnings": []}


def remove(path: Path, project_id: str, missing_ok: bool = False) -> dict:
    lock = path.with_name(path.name + ".lock")
    bak = path.with_name(path.name + ".bak")
    _lock_path_ok(lock)
    removed, warnings = [], []
    with exclusive_lock(lock):  # 与 TopologyStore.create / update 同一把锁
        if missing_ok and not _lexists(path):
            return {"removed": removed, "warnings": warnings}
        _check_main(path, project_id)
        path.unlink()
        removed.append(str(path))
        # 主拓扑已删:.bak 只是辅助文件。之后对它的任何检查或删除出错(归属不符、权限、I/O)都只记警告、
        # 照常完成,避免"主拓扑已删、命令却失败"的半删除
        try:
            _fail_at("bak_lstat")
            if _lexists(bak):
                pid = _owner(bak)
                if pid != project_id:
                    raise NotOurs(f"{bak} 属于工程 {pid},不是 {project_id}")
                _fail_at("bak_unlink")
                bak.unlink()
                removed.append(str(bak))
        except Exception as exc:  # noqa: BLE001 —— 主拓扑已删,收尾阶段的任何意外都只记警告,不能让命令失败造成半删除
            warnings.append(f"{exc};已保留 {bak.name}")
    return {"removed": removed, "warnings": warnings}


def main() -> int:
    path = default_topology_path()
    args = sys.argv[1:]
    try:
        if args[:1] == ["init"] and len(args) == 2:
            TopologyStore.create(path, json.loads(args[1]))
            _fail_at("topology_after_write")  # 测试:文件已写入、随后失败(如目录 fsync 出错)
        elif args[:1] == ["check"] and len(args) == 2:
            print(json.dumps(check(Path(path), args[1]), ensure_ascii=False))
            return 0
        elif args[:1] == ["remove"] and len(args) in (2, 3) and args[2:] in ([], ["--missing-ok"]):
            print(json.dumps(remove(Path(path), args[1], missing_ok=args[2:] == ["--missing-ok"]), ensure_ascii=False))
            return 0
        elif args != ["show"]:
            print(json.dumps({"error": "用法:init <json> | show | check <project_id> | remove <project_id> [--missing-ok]"},
                             ensure_ascii=False),
                  file=sys.stderr)
            return 2
        topology = TopologyStore(path).current()
    except NotOurs as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 3
    except (TopologyError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"path": str(path), "project_id": topology.project_id,
                      "workspace_label": topology.workspace_label, "ips": list(topology.ips)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
