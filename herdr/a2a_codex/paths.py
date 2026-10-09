"""规范化 A2A 持久化路径。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional


def absolute_path(value: str | Path, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label} 必须是绝对路径: {path}")
    return path.resolve()


def default_state_dir(environ: Optional[Mapping[str, str]] = None) -> Path:
    values = os.environ if environ is None else environ
    override = values.get("A2A_STATE_DIR")
    if override:
        return absolute_path(override, "A2A_STATE_DIR")
    xdg = values.get("XDG_STATE_HOME")
    if xdg:
        return absolute_path(Path(xdg).expanduser() / "a2a", "XDG_STATE_HOME")
    return (Path.home() / ".local" / "state" / "a2a").resolve()
