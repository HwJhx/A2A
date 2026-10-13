#!/usr/bin/env bash
# 在 Mac 上运行:把源码同步到 VM 的 ~/gui_demo_run,按 lockfile 安装依赖并构建。不启动后端。
# 用法:gui_demo/bin/deploy-vm.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
"$HERE/sync-to-vm.sh" gui_demo_run
orb -m ubuntu bash -lc '
  set -euo pipefail
  export PATH="$HOME/.local/lib/node-v22.20.0-linux-arm64/bin:$PATH"
  cd ~/gui_demo_run
  npm ci --no-audit --no-fund
  npm run build
'
echo "已部署到 VM:~/gui_demo_run。在 VM 上运行 ~/gui_demo_run/bin/start 启动。"
