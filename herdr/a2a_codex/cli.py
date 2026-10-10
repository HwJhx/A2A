"""受业务拓扑约束的 A2A Broker 命令行。"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .audit import AuditLog
from .broker import DeliveryBroker
from .herdr_client import HerdrClient
from .messages import DELIVERY_UNCERTAIN, DELIVERED, TERMINAL_STATES
from .registry import Registry
from .router import Router, session_from_env
from .runtime import BrokerRuntime
from .spool import Spool
from .topology import TopologyStore


def _topology_path(value: str | None) -> Path:
    configured = value or os.environ.get("A2A_TOPOLOGY")
    path = Path(configured) if configured else Path(__file__).resolve().parent / "config" / "topology.yaml"
    return path.expanduser().resolve()


def _state_components():
    return Spool(), AuditLog()


def _router(args: argparse.Namespace, spool: Spool, audit: AuditLog) -> Router:
    session = args.session or session_from_env(os.environ)
    topology = TopologyStore(_topology_path(args.topology))
    registry = Registry()
    return Router(topology, registry, spool, audit, session=session)


def _runtime(args: argparse.Namespace, spool: Spool, audit: AuditLog) -> BrokerRuntime:
    session = args.session or session_from_env(os.environ)
    broker = DeliveryBroker(HerdrClient(session), Registry(), spool, audit)
    return BrokerRuntime(broker)


def _actor(value: str | None) -> str:
    return value or getpass.getuser()


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def _message_view(message) -> dict:
    return asdict(message)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="a2a")
    parser.add_argument("--session", help="Herdr session（默认 HERDR_SESSION 或 default）")
    parser.add_argument("--topology", help="拓扑 YAML（默认 A2A_TOPOLOGY 或安装包示例拓扑）")
    sub = parser.add_subparsers(dest="command", required=True)

    read = sub.add_parser("read-agent", help="读取 agent 终端输出（Herdr 诊断）")
    read.add_argument("target")
    read.add_argument("--source", default="recent-unwrapped")
    read.add_argument("--lines", type=int)

    wait = sub.add_parser("wait-agent", help="等待 agent 状态（Herdr 诊断）")
    wait.add_argument("target")
    wait.add_argument("--until", action="append")
    wait.add_argument("--timeout-ms", type=int)

    run = sub.add_parser("broker", help="Broker 管理")
    run_sub = run.add_subparsers(dest="broker_command", required=True)
    run_sub.add_parser("run", help="启动常驻 Broker")

    send = sub.add_parser("send", help="按拓扑边鉴权并提交固定模板消息")
    send.add_argument("edge_id")

    status = sub.add_parser("status", help="查看消息与投递状态")
    status.add_argument("msg_id")

    queue = sub.add_parser("queue", help="查看目标队列头和暂停状态")
    queue.add_argument("dst", nargs="?", help="可选的目标 agent_id")

    resolve = sub.add_parser("resolve", help="裁定当前目标队列头")
    resolve.add_argument("msg_id")
    resolve.add_argument("action", choices=("delivered", "retry", "abandon", "abandon-and-continue"))
    resolve.add_argument("--actor", help="审计署名（默认当前系统用户名；不是认证）")
    resolve.add_argument("--reason", required=True, help="裁定原因，写入审计")

    ruling = sub.add_parser("ruling", help="管理尚未完整应用的裁定")
    ruling_sub = ruling.add_subparsers(dest="ruling_command", required=True)
    void = ruling_sub.add_parser("void", help="作废未完成的裁定，不撤销已持久化效果")
    void.add_argument("ruling_id")
    void.add_argument("--actor", help="审计署名（默认当前系统用户名；不是认证）")
    void.add_argument("--reason", required=True)
    void.add_argument("--verified-effects", required=True,
                      help="作废前核实到的效果，JSON object")

    dispatch = sub.add_parser("dispatch", help="控制全局派发")
    dispatch_sub = dispatch.add_subparsers(dest="dispatch_command", required=True)
    resume = dispatch_sub.add_parser("resume", help="核验后恢复全局派发")
    resume.add_argument("--actor", help="审计署名（默认当前系统用户名；不是认证）")
    resume.add_argument("--reason", required=True)
    resume.add_argument("--quarantine-location", required=True,
                        help="已处理的隔离数据路径；没有隔离数据时填 none")

    spool_cmd = sub.add_parser("spool", help="查看并修复 Spool 隔离项")
    spool_sub = spool_cmd.add_subparsers(dest="spool_command", required=True)
    quarantine = spool_sub.add_parser("quarantine", help="列出隔离记录")
    quarantine_sub = quarantine.add_subparsers(dest="quarantine_command", required=True)
    quarantine_sub.add_parser("list", help="列出所有隔离记录")
    repair = spool_sub.add_parser("repair", help="核验已从备份恢复到原路径的 Spool 文件")
    repair.add_argument("incident_id")
    repair.add_argument("--actor", help="审计署名（默认当前系统用户名；不是认证）")
    repair.add_argument("--reason", required=True)
    repair.add_argument("--verification", required=True,
                        help="恢复数据的核验依据，写入审计")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "read-agent":
            client = HerdrClient(args.session)
            print(client.read_agent(args.target, source=args.source, lines=args.lines), end="")
            return 0
        if args.command == "wait-agent":
            client = HerdrClient(args.session)
            until = args.until if args.until is not None else ["idle", "done", "blocked"]
            value = client.wait_agent(args.target, until=until, timeout_ms=args.timeout_ms)
            _json(value.raw)
            return 0

        spool, audit = _state_components()
        if args.command == "send":
            router = _router(args, spool, audit)
            receipt = router.send(args.edge_id)
            _json(asdict(receipt))
            return 0
        if args.command == "status":
            _json(_message_view(spool.get(args.msg_id)))
            return 0
        if args.command == "queue":
            targets = [args.dst] if args.dst else spool.queue_targets()
            rows = []
            for dst in targets:
                head = spool.queue_head(dst)
                if head is None:
                    continue
                queue_messages = spool.pending(dst) + [item for item in spool.done()
                                                        if item.dst == dst]
                later = sum(1 for item in queue_messages
                            if item.queue_seq is not None and head.queue_seq is not None
                            and item.queue_seq > head.queue_seq)
                rows.append({"dst": dst, "head": _message_view(head),
                             "paused": head.state == DELIVERY_UNCERTAIN or
                                      (head.state in TERMINAL_STATES and head.state != DELIVERED),
                             "paused_message_count": later,
                             "slot_released": spool.slot_released(dst, head.queue_seq)})
            _json(rows)
            return 0
        if args.command == "broker" and args.broker_command == "run":
            _runtime(args, spool, audit).run()
            return 0
        if args.command == "resolve":
            runtime = _runtime(args, spool, audit)
            action = "abandon_and_continue" if args.action == "abandon-and-continue" else args.action
            result = runtime.broker.resolve(args.msg_id, action, actor=_actor(args.actor),
                                            reason=args.reason)
            _json(result)
            return 0
        if args.command == "ruling" and args.ruling_command == "void":
            try:
                effects = json.loads(args.verified_effects)
            except ValueError as exc:
                raise ValueError("--verified-effects 必须是有效 JSON") from exc
            if not isinstance(effects, dict):
                raise ValueError("--verified-effects 必须是 JSON object")
            result = _runtime(args, spool, audit).broker.void_ruling(
                args.ruling_id, actor=_actor(args.actor), reason=args.reason,
                verified_effects=effects)
            _json(result)
            return 0
        if args.command == "dispatch" and args.dispatch_command == "resume":
            result = _runtime(args, spool, audit).resume_dispatch(
                actor=_actor(args.actor), reason=args.reason,
                quarantine_location=args.quarantine_location)
            _json(result)
            return 0
        if args.command == "spool" and args.spool_command == "quarantine":
            _json(spool.quarantine_incidents())
            return 0
        if args.command == "spool" and args.spool_command == "repair":
            result = _runtime(args, spool, audit).resolve_spool_corruption(
                args.incident_id, actor=_actor(args.actor), reason=args.reason,
                verification=args.verification)
            _json(result)
            return 0
        parser.error("不支持的命令")
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"a2a: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
