"""a2a_codex 的最小命令行入口。"""
from __future__ import annotations

import argparse
import json
import sys

from .herdr_client import HerdrClient


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="a2a-herdr")
    parser.add_argument("--session")
    sub = parser.add_subparsers(dest="command", required=True)
    read = sub.add_parser("read-agent")
    read.add_argument("target")
    read.add_argument("--source", default="recent-unwrapped")
    read.add_argument("--lines", type=int)
    wait = sub.add_parser("wait-agent")
    wait.add_argument("target")
    wait.add_argument("--until", action="append")
    wait.add_argument("--timeout-ms", type=int)
    args = parser.parse_args(argv)
    client = HerdrClient(args.session)
    if args.command == "read-agent":
        print(client.read_agent(args.target, source=args.source, lines=args.lines), end="")
    else:
        until = args.until if args.until is not None else ["idle", "done", "blocked"]
        value = client.wait_agent(args.target, until=until, timeout_ms=args.timeout_ms)
        print(json.dumps(value.raw, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
