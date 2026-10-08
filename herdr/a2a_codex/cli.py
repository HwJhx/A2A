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
    prompt = sub.add_parser("send-prompt")
    prompt.add_argument("target")
    prompt.add_argument("text")
    prompt.add_argument("--wait", action="store_true")
    prompt.add_argument("--timeout-ms", type=int)
    read = sub.add_parser("read-agent")
    read.add_argument("target")
    read.add_argument("--source", default="recent-unwrapped")
    read.add_argument("--lines", type=int)
    wait = sub.add_parser("wait-agent")
    wait.add_argument("target")
    wait.add_argument("--until", action="append", default=["idle", "done", "blocked"])
    wait.add_argument("--timeout-ms", type=int)
    args = parser.parse_args(argv)
    client = HerdrClient(args.session)
    if args.command == "send-prompt":
        value = client.send_prompt(args.target, args.text, wait=args.wait, timeout_ms=args.timeout_ms)
        print(json.dumps(value.raw, ensure_ascii=False))
    elif args.command == "read-agent":
        print(client.read_agent(args.target, source=args.source, lines=args.lines), end="")
    else:
        value = client.wait_agent(args.target, until=args.until, timeout_ms=args.timeout_ms)
        print(json.dumps(value.raw, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
