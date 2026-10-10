"""a2a 命令行。路径都取自 paths.py(A2A_STATE_DIR / A2A_TOPOLOGY 等环境变量)。

agent 用:
  a2a send <edge_id>                 发送固定句式(身份取自 pane 环境变量;立即返回 msg_id)
  a2a status <msg_id>                查询一条消息
操作员用:
  a2a queue [<dst>]                  各目标的队列头、暂停原因、积压数量;全局停止状态
  a2a resolve <msg_id> delivered|retry|abandon --reason TEXT
  a2a ruling void <ruling_id|corrupt:指纹> --reason TEXT --verified TEXT
  a2a dispatch resume --reason TEXT
  a2a topology show | add-ip | remove-ip | add-edge | remove-edge | set-template
  a2a agent spawn | stop | close | purge | restore <role> <ip> [--session NAME]   生命周期
  a2a agent list [--session NAME]
  a2a broker run --session NAME [--once]

输出都是 JSON(方便 agent 和脚本解析)。退出码:0 成功;2 用法错误;3 broker 已在运行;
4 发送被拒绝;5 找不到;6 操作不合法(例如裁定的不是队列头)。
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import signal
import sys
from typing import Any, List, Optional

from . import rulings
from .audit import AuditLog
from .broker import Broker, BrokerAlreadyRunning
from .errors import HerdrError
from .herdr_client import HerdrClient, herdr_version
from .messages import DELIVERY_UNCERTAIN
from .paths import default_audit_path, default_registry_path, default_spool_dir, state_dir
from .policy import BrokerConfig, semantics_verified
from .registry import AgentNotRegisteredError, Registry
from .router import Router, SendRejected
from .spool import MessageNotFoundError, SlotError, Spool, SpoolError
from .topology import TopologyError, TopologyStore

EXIT_USAGE, EXIT_RUNNING, EXIT_REJECTED, EXIT_NOT_FOUND, EXIT_INVALID = 2, 3, 4, 5, 6


def _out(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _err(message: str, code: int) -> int:
    print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
    return code


def _actor() -> str:
    """裁定的署名:执行命令的系统用户名。只作审计署名,不作身份认证(08 §10 第 8 项)。"""
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def _spool() -> Spool:
    return Spool(default_spool_dir())


def _audit() -> AuditLog:
    return AuditLog(default_audit_path())


def _halt_path():
    return state_dir() / "dispatch.halted"


# ---- agent 命令 ---------------------------------------------------------
def cmd_send(args: argparse.Namespace) -> int:
    router = Router(TopologyStore(), Registry(default_registry_path()), _spool(), _audit())
    try:
        receipt = router.send(args.edge_id)
    except SendRejected as exc:
        print(json.dumps({"rejected": exc.code, "reason": exc.reason, "msg_id": exc.msg_id}, ensure_ascii=False),
              file=sys.stderr)
        return EXIT_REJECTED
    _out({"msg_id": receipt.msg_id, "dst": receipt.dst, "state": receipt.state, "queue_seq": receipt.queue_seq,
          "text": receipt.text})
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    try:
        _out(_spool().get(args.msg_id).to_dict())
        return 0
    except MessageNotFoundError:
        rejected = [e for e in _audit().read() if e.get("msg_id") == args.msg_id and e.get("state") == "REJECTED"]
        if rejected:
            _out(rejected[-1])
            return 0
        return _err(f"找不到消息 {args.msg_id}", EXIT_NOT_FOUND)
    except SpoolError as exc:
        return _err(str(exc), EXIT_INVALID)


# ---- 操作员命令 ----------------------------------------------------------
def _describe_head(spool: Spool, dst: str) -> dict:
    try:
        head = spool.head(dst)
    except SpoolError as exc:
        return {"dst": dst, "paused": True, "reason": f"队列状态无法读取(fail-closed):{exc}"}
    if head is None:
        return {"dst": dst, "paused": False, "head": None}
    backlog = len([m for m in spool.pending(dst) if m.queue_seq != head.queue_seq])
    info: dict = {"dst": dst, "queue_seq": head.queue_seq, "backlog": backlog}
    if head.active is not None:
        m = head.active
        info.update(msg_id=m.msg_id, state=m.state, detail=m.detail, updated_at=m.updated_at)
        info["paused"] = m.state == DELIVERY_UNCERTAIN
        if info["paused"]:
            info["reason"] = "投递结果不确定,等待裁定"
            info["resolve"] = f"a2a resolve {m.msg_id} delivered|retry|abandon --reason ..."
    else:
        m = head.last_terminal
        info.update(msg_id=m.msg_id, state=m.state, detail=m.detail, updated_at=m.updated_at, paused=True)
        if m.state == "DELIVERED":
            info.update(paused=False, reason="已送达,等待 broker 放行")
        else:
            info["reason"] = f"确定失败({m.state}),等待操作员重试或放弃并继续"
            info["resolve"] = f"a2a resolve {m.msg_id} retry|abandon --reason ..."
    return info


def cmd_queue(args: argparse.Namespace) -> int:
    spool = _spool()
    try:
        targets = [args.dst] if args.dst else spool.queue_targets()
    except SpoolError as exc:
        return _err(str(exc), EXIT_INVALID)
    pending, corrupt = rulings.unapplied(_audit())
    halted = None
    if _halt_path().exists():
        try:
            halted = json.loads(_halt_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            halted = {"reason": "dispatch.halted 存在(内容无法解析)"}
    _out({"dispatch_halted": halted, "unapplied_rulings": [p["ruling_id"] for p in pending],
          "unreadable_rulings": corrupt, "targets": [_describe_head(spool, d) for d in targets]})
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    spool, audit = _spool(), _audit()
    try:
        event = rulings.plan(spool, args.msg_id, args.action, actor=_actor(), reason=args.reason)
    except MessageNotFoundError:
        return _err(f"找不到消息 {args.msg_id}", EXIT_NOT_FOUND)
    except (rulings.RulingError, SlotError, SpoolError) as exc:
        return _err(str(exc), EXIT_INVALID)
    audit.record(event)  # 先落盘;写不进去会抛异常,什么都不会生效
    try:
        steps = rulings.apply(spool, audit, event)
    except Exception as exc:
        return _err(f"裁定 {event['ruling_id']} 已记录但生效失败:{exc};broker 下次启动会补做,"
                    f"或核实后用 a2a ruling void 作废", EXIT_INVALID)
    _out({"ruling_id": event["ruling_id"], "ruling": event["ruling"], "msg_id": event["msg_id"],
          "retry_msg_id": event.get("retry_msg_id"), "steps": steps})
    return 0


def cmd_ruling_void(args: argparse.Namespace) -> int:
    try:
        _out(rulings.void(_audit(), args.target, actor=_actor(), reason=args.reason, verified=args.verified))
        return 0
    except rulings.RulingError as exc:
        return _err(str(exc), EXIT_INVALID)


def cmd_dispatch_resume(args: argparse.Namespace) -> int:
    path = _halt_path()
    if not path.exists():
        return _err("投递没有处于全局停止状态", EXIT_INVALID)
    audit = _audit()
    _, corrupt = rulings.unapplied(audit)
    if corrupt:
        return _err("仍有读不出来的操作员裁定未作废:" + ", ".join("corrupt:" + c["fingerprint"] for c in corrupt),
                    EXIT_INVALID)
    try:
        halted = json.loads(path.read_text(encoding="utf-8")).get("reason")
    except (OSError, ValueError):
        halted = None
    audit.record({"state": "DISPATCH_RESUMED", "actor": _actor(), "reason": args.reason, "halted_reason": halted})
    path.unlink()
    _out({"resumed": True, "halted_reason": halted})
    return 0


# ---- 拓扑 --------------------------------------------------------------
def cmd_topology(args: argparse.Namespace) -> int:
    store = TopologyStore()
    try:
        if args.op == "show":
            _out({"path": str(store.path), "revision": store.revision, "topology": store.current().to_dict()})
            return 0
        ops = {
            "add-ip": lambda: store.add_ip(args.ip),
            "remove-ip": lambda: store.remove_ip(args.ip),
            "add-edge": lambda: store.add_edge(args.edge_id, args.from_role, args.to_role, args.template),
            "remove-edge": lambda: store.remove_edge(args.edge_id),
            "set-template": lambda: store.set_template(args.edge_id, args.template),
        }
        before = store.revision
        ops[args.op]()
        store.reload_if_changed()
        change = {k: v for k, v in vars(args).items() if k not in ("func", "group", "verbose")}
        _audit().record({"state": "TOPOLOGY_CHANGED", "actor": _actor(), "change": change,
                         "previous_revision": before, "topology_revision": store.revision,
                         "detail": "拓扑变更不改写已入队消息(08 §7.2)"})
        _out({"ok": True, "op": args.op, "revision": store.revision})
        return 0
    except TopologyError as exc:
        return _err(str(exc), EXIT_INVALID)


# ---- 生命周期 ----------------------------------------------------------
def cmd_agent(args: argparse.Namespace) -> int:
    from .lifecycle import Lifecycle, LifecycleError
    from .launcher import LauncherError

    session = args.session or os.environ.get("HERDR_SESSION")
    if not session:
        return _err("需要 --session(或环境变量 HERDR_SESSION)", EXIT_USAGE)
    life = Lifecycle(client=HerdrClient(session), topology=TopologyStore(),
                     registry=Registry(default_registry_path()), audit=_audit(), state_dir=state_dir())
    try:
        if args.op == "list":
            _out(life.list())
            return 0
        if args.op in ("spawn", "restore"):
            record = getattr(life, args.op)(args.role, args.ip, cwd=args.cwd)
        else:
            record = getattr(life, args.op)(args.role, args.ip)
        _out({"op": args.op, "agent_id": record.agent_id, "lifecycle": record.lifecycle, "pane_id": record.pane_id,
              "tab_id": record.tab_id})
        return 0
    except AgentNotRegisteredError as exc:
        return _err(str(exc), EXIT_NOT_FOUND)
    except (LifecycleError, LauncherError, TopologyError, HerdrError, ValueError) as exc:
        return _err(f"{type(exc).__name__}: {exc}", EXIT_INVALID)


# ---- broker ------------------------------------------------------------
def build_broker(session: str, *, herdr_bin: str = "herdr") -> Broker:
    try:
        version: Optional[str] = herdr_version(herdr_bin=herdr_bin)
    except HerdrError as exc:
        logging.getLogger("a2a.broker").warning("无法读取 herdr 版本(%s),按未实测版本处理", exc)
        version = None
    return Broker(
        spool=_spool(), registry=Registry(default_registry_path()), audit=_audit(),
        client=HerdrClient(session, herdr_bin=herdr_bin), session=session, state_dir=state_dir(),
        semantics_verified=semantics_verified(version), herdr_version=version, config=BrokerConfig(),
    )


def cmd_broker_run(args: argparse.Namespace) -> int:
    session = args.session or os.environ.get("HERDR_SESSION")
    if not session:
        return _err("需要 --session(或环境变量 HERDR_SESSION)", EXIT_USAGE)
    broker = build_broker(session, herdr_bin=args.herdr_bin)
    try:
        broker.acquire()
    except BrokerAlreadyRunning as exc:
        return _err(str(exc), EXIT_RUNNING)
    try:
        summary = broker.recover()
        logging.getLogger("a2a.broker").info("启动恢复完成:%s", summary)
        if args.once:
            broker.scan(threaded=False)
            return 0

        def stop(signum, frame):  # noqa: ARG001
            broker.stop_event.set()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        broker.run_forever()
        return 0
    finally:
        broker.release_lock()


# ---- 参数 --------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="a2a", description="基于 herdr 的多智能体编排框架")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="group", required=True)

    p = sub.add_parser("send", help="发送一条固定句式消息(在 agent 的 pane 里运行)")
    p.add_argument("edge_id")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("status", help="查询一条消息")
    p.add_argument("msg_id")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("queue", help="各目标的队列头与暂停原因")
    p.add_argument("dst", nargs="?")
    p.set_defaults(func=cmd_queue)

    p = sub.add_parser("resolve", help="裁定队列头:delivered / retry / abandon")
    p.add_argument("msg_id")
    p.add_argument("action", choices=rulings.ACTIONS)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_resolve)

    ruling = sub.add_parser("ruling", help="裁定管理").add_subparsers(dest="command", required=True)
    p = ruling.add_parser("void", help="作废一条无法补做的裁定(只停止补做,不撤销已生效的效果)")
    p.add_argument("target", help="ruling_id,或 corrupt:<指纹>")
    p.add_argument("--reason", required=True)
    p.add_argument("--verified", required=True, help="作废前核实到的已生效效果")
    p.set_defaults(func=cmd_ruling_void)

    dispatch = sub.add_parser("dispatch", help="全局投递").add_subparsers(dest="command", required=True)
    p = dispatch.add_parser("resume", help="解除全局停止")
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_dispatch_resume)

    topo = sub.add_parser("topology", help="查看或修改拓扑").add_subparsers(dest="op", required=True)
    topo.add_parser("show").set_defaults(func=cmd_topology)
    for name in ("add-ip", "remove-ip"):
        p = topo.add_parser(name)
        p.add_argument("ip")
        p.set_defaults(func=cmd_topology)
    p = topo.add_parser("add-edge")
    p.add_argument("edge_id")
    p.add_argument("from_role")
    p.add_argument("to_role")
    p.add_argument("template")
    p.set_defaults(func=cmd_topology)
    p = topo.add_parser("remove-edge")
    p.add_argument("edge_id")
    p.set_defaults(func=cmd_topology)
    p = topo.add_parser("set-template")
    p.add_argument("edge_id")
    p.add_argument("template")
    p.set_defaults(func=cmd_topology)

    agent = sub.add_parser("agent", help="agent 生命周期").add_subparsers(dest="op", required=True)
    for name, help_text in (("spawn", "新建 pane 并启动 agent"), ("stop", "只停 agent 进程,保留 pane 与注册"),
                            ("close", "关闭 pane,保留注册"), ("purge", "关闭 pane 并注销"),
                            ("restore", "按注册信息重新启动")):
        p = agent.add_parser(name, help=help_text)
        p.add_argument("role")
        p.add_argument("ip")
        p.add_argument("--session")
        if name in ("spawn", "restore"):
            p.add_argument("--cwd", help="agent 的工作目录(默认家目录)")
        p.set_defaults(func=cmd_agent)
    p = agent.add_parser("list", help="注册表 + herdr 实时状态")
    p.add_argument("--session")
    p.set_defaults(func=cmd_agent)

    broker = sub.add_parser("broker", help="消息投递进程").add_subparsers(dest="command", required=True)
    p = broker.add_parser("run", help="前台运行 broker(Ctrl-C / SIGTERM 退出)")
    p.add_argument("--session", help="服务的 herdr 会话名(默认取 HERDR_SESSION)")
    p.add_argument("--once", action="store_true", help="启动恢复后只同步处理一遍就退出(调试用)")
    p.add_argument("--herdr-bin", default="herdr", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_broker_run)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
