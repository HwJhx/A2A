#!/usr/bin/env bash
# 把 Mac 上的源码同步到 VM 本地运行目录(默认 ~/gui_demo_run),避开 OrbStack 共享目录读到旧文件的问题。
# 不传 node_modules、构建产物;目标目录里除 node_modules 外的旧文件先删掉,保证与 Mac 源码一致。
# 用法(在 Mac 上):gui_demo/bin/sync-to-vm.sh [目标目录名]
#   目标目录名:家目录下的一级目录名,只能是 gui_demo_ 开头的字母、数字、下划线、连字符(如 gui_demo_run、gui_demo_dev)。
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:-gui_demo_run}"
# 目标会被清空,所以名字必须足够具体:不允许 .、..、斜杠、空格等,只允许 gui_demo_ 开头
if ! [[ "$DEST" =~ ^gui_demo_[A-Za-z0-9_-]+$ ]]; then
  echo "目标目录名必须匹配 ^gui_demo_[A-Za-z0-9_-]+\$(家目录下的一级目录):$DEST" >&2
  exit 2
fi
ARCHIVE="$(mktemp /private/tmp/gui_demo_sync.XXXXXX)"
trap 'rm -f "$ARCHIVE"' EXIT
COPYFILE_DISABLE=1 tar --no-xattrs -C "$SRC" --exclude node_modules --exclude server/dist --exclude web/dist \
  --exclude '._*' --exclude .DS_Store -cf "$ARCHIVE" .
SIZE="$(stat -f %z "$ARCHIVE")"
# 归档先落到 Mac 本地文件,再让 VM 经 /mnt/mac 读(不用管道,避免 orb 的标准输入在非交互环境里挂起);
# 先等 VM 看到的大小与 Mac 一致(OrbStack 共享目录偶有滞后)
orb -m ubuntu bash -c '
  set -euo pipefail
  dest="$HOME/$1"; archive="/mnt/mac$2"; size="$3"
  for _ in $(seq 50); do [ "$(stat -c %s "$archive" 2>/dev/null)" = "$size" ] && break; sleep 0.2; done
  [ "$(stat -c %s "$archive")" = "$size" ] || { echo "VM 读到的归档大小不对" >&2; exit 1; }
  # 目标不能是软链接,必须是家目录下真实的一级目录(不是家目录本身)
  if [ -L "$dest" ]; then echo "目标 $dest 是软链接,拒绝" >&2; exit 2; fi
  mkdir -p "$dest"
  real="$(realpath "$dest")"
  if [ "$real" = "$(realpath "$HOME")" ] || [ "$(dirname "$real")" != "$(realpath "$HOME")" ]; then
    echo "目标 $dest 的真实路径 $real 不是家目录下的一级目录,拒绝" >&2; exit 2
  fi
  find "$real" -mindepth 1 -maxdepth 1 ! -name node_modules -exec rm -rf {} +
  tar -C "$real" -xf "$archive"
' _ "$DEST" "$ARCHIVE" "$SIZE"
echo "已同步到 VM:~/$DEST"
