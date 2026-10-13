"""gui_demo 的拓扑辅助脚本(a2a 没有建拓扑的命令)。只用 a2a 自己的 TopologyStore;路径取自 A2A_TOPOLOGY。

用法:
  topology_tool.py init <json>   按给定内容新建拓扑(文件已存在则失败)
  topology_tool.py show          输出 {"path", "project_id", "workspace_label", "ips"}
"""
import json
import sys

from a2a.paths import default_topology_path
from a2a.topology import TopologyError, TopologyStore


def main() -> int:
    path = default_topology_path()
    try:
        if sys.argv[1:2] == ["init"] and len(sys.argv) == 3:
            TopologyStore.create(path, json.loads(sys.argv[2]))
        elif sys.argv[1:] != ["show"]:
            print(json.dumps({"error": "用法:init <json> | show"}, ensure_ascii=False), file=sys.stderr)
            return 2
        topology = TopologyStore(path).current()
    except (TopologyError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"path": str(path), "project_id": topology.project_id,
                      "workspace_label": topology.workspace_label, "ips": list(topology.ips)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
