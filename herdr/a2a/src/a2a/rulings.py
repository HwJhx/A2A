"""操作员裁定(阶段 5d;协议见 herdr/claude/08-protocol.md §7.3、§8 第 3 步、§9 ruling 取值表)。

三步,对应协议的"先落盘,再生效,可幂等补做":
  1. plan():检查消息是不是队列头、能不能这样裁定,生成 OPERATOR_RULING 事件(含稳定的 ruling_id;
     终态重试时连新消息的 msg_id 也在这里确定)。
  2. 调用方把事件写进审计日志(AuditLog.record 会 fsync)。**写不进去就什么也不做。**
  3. apply():按事件执行效果——状态迁移 / 重试入队 / 放行,每个效果都带 ruling_id、都可重复执行;
     全部效果落盘后才写 RULING_APPLIED。

broker 启动时用 unapplied() 找出"已落盘但没有 RULING_APPLIED、也没被作废"的裁定并补做;
审计里读不出来、但看起来是裁定的内容,一律 fail-closed(全局停止投递),等操作员作废(void)。
"""
from __future__ import annotations

import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ._fsutil import exclusive_lock
from .audit import AuditLog, fingerprint
from .messages import DELIVERED, DELIVERY_UNCERTAIN, FAILED, QUEUED, RETRYING, new_msg_id
from .spool import Spool

OPERATOR_RULING = "OPERATOR_RULING"
RULING_APPLIED = "RULING_APPLIED"
RULING_VOIDED = "RULING_VOIDED"
QUEUE_RELEASED = "QUEUE_RELEASED"

# ruling 取值(08 §9 表)
DELIVERED_RULING = "delivered"
NOT_DELIVERED_RETRY = "not_delivered_retry"
ABANDON = "abandon"
RETRY_TERMINAL = "retry_terminal"
ABANDON_AND_CONTINUE = "abandon_and_continue"

ACTIONS = ("delivered", "retry", "abandon")  # 命令行上的三个动作
_RELEASES = {DELIVERED_RULING, ABANDON, ABANDON_AND_CONTINUE}


class RulingError(RuntimeError):
    """裁定不合法(不是队列头、状态不对),或补做时发现与裁定矛盾。"""


def new_ruling_id() -> str:
    return "r%015x-%s" % (time.time_ns() >> 4, secrets.token_hex(3))


def plan(spool: Spool, msg_id: str, action: str, *, actor: str, reason: str) -> Dict[str, Any]:
    """生成 OPERATOR_RULING 事件(尚未落盘)。不合法时抛 RulingError,不改变任何东西。"""
    if action not in ACTIONS:
        raise RulingError(f"动作必须是 {ACTIONS} 之一")
    if not reason.strip():
        raise RulingError("必须给出裁定理由(--reason)")
    message = spool.get(msg_id)
    head = spool.head(message.dst)
    if head is None or head.queue_seq != message.queue_seq:
        raise RulingError(f"{msg_id} 不是 {message.dst} 的队列头"
                          f"(队列头槽位 {head.queue_seq if head else '无'});只能裁定队列头")
    event: Dict[str, Any] = {"state": OPERATOR_RULING, "ruling_id": new_ruling_id(), "actor": actor,
                             "reason": reason, "msg_id": msg_id, "dst": message.dst,
                             "queue_seq": message.queue_seq, "session": message.session,
                             "previous_state": message.state}
    if head.active is not None:
        if head.active.msg_id != msg_id:
            raise RulingError(f"{message.dst} 的槽位 {head.queue_seq} 正在投递 {head.active.msg_id};"
                              f"只能裁定它,而不是更早的 {msg_id}")
        if message.state != DELIVERY_UNCERTAIN:
            raise RulingError(f"{msg_id} 处于 {message.state},还在投递中,不需要裁定")
        ruling, new_state = {"delivered": (DELIVERED_RULING, DELIVERED), "retry": (NOT_DELIVERED_RETRY, RETRYING),
                             "abandon": (ABANDON, FAILED)}[action]
        event.update(ruling=ruling, new_state=new_state)
        if ruling == DELIVERED_RULING:
            event["evidence"] = "operator_confirmed"
        return event
    last = head.last_terminal
    if last is None or last.msg_id != msg_id:
        raise RulingError(f"槽位 {head.queue_seq} 的最后一次结果是 {last.msg_id if last else '无'},只能裁定它")
    if last.state == DELIVERED:
        raise RulingError(f"{msg_id} 已送达,broker 会自动放行,不需要裁定")
    if action == "delivered":
        raise RulingError(f"{msg_id} 已是终态 {last.state},终态不可变。若确认其实已送达,"
                          f"请改用 abandon 放行(在 --reason 里写明已送达的依据)")
    if action == "retry":
        event.update(ruling=RETRY_TERMINAL, new_state=last.state, retry_msg_id=new_msg_id(),
                     retry_initial_state="QUEUED")
    else:
        event.update(ruling=ABANDON_AND_CONTINUE, new_state=last.state)
    return event


def lock(state_dir: "str | Path"):
    """裁定的跨进程锁:resolve 的 检查 → 落盘 → 生效、ruling void、broker 启动补做都在这把锁里串行,
    避免两个并发裁定都通过队列头检查,或补做与新裁定交错。裁定很少,用一把全局锁即可。"""
    return exclusive_lock(Path(state_dir) / "rulings.lock")


def apply(spool: Spool, audit: AuditLog, event: Dict[str, Any], *, recovering: bool = False) -> List[str]:
    """执行一条已落盘的裁定。每个效果都可重复执行;全部完成后写 RULING_APPLIED。返回实际执行的步骤。"""
    rid, ruling, msg_id, dst = event["ruling_id"], event["ruling"], event["msg_id"], event["dst"]
    base = {"ruling_id": rid, "dst": dst, "session": event.get("session"), "queue_seq": event.get("queue_seq")}
    detail = f"操作员裁定 {ruling}({event.get('actor')}):{event.get('reason')}"
    steps: List[str] = []
    entries = audit.read()
    seen = {(e.get("state"), e.get("msg_id")) for e in entries if e.get("ruling_id") == rid}

    def record_once(entry: Dict[str, Any]) -> None:
        # 效果落盘与它的审计不是一次原子写入;补做时按 (ruling_id, 事件, msg_id) 补记缺的、不重复记已有的
        if (entry["state"], entry["msg_id"]) not in seen:
            audit.record(entry)
            seen.add((entry["state"], entry["msg_id"]))

    if ruling in (DELIVERED_RULING, NOT_DELIVERED_RETRY, ABANDON):
        current = spool.get(msg_id)
        entry = dict(base, msg_id=msg_id, state=event["new_state"], detail=detail)
        if event.get("evidence"):
            entry["evidence"] = event["evidence"]
        if current.ruling_id == rid:
            pass  # 迁移已经做过(ruling_id 与状态同一次落盘);之后 broker 可能已继续推进,不能再做一次
        elif current.state == event["previous_state"]:
            spool.update(msg_id, state=event["new_state"], detail=detail, ruling_id=rid)
            steps.append(f"{event['previous_state']}->{event['new_state']}")
        else:
            raise RulingError(f"消息 {msg_id} 现在是 {current.state}(最近裁定 {current.ruling_id}),"
                              f"与裁定 {rid} 的前提 {event['previous_state']} 矛盾;不能猜测,停止补做")
        record_once(entry)
    elif ruling == RETRY_TERMINAL:
        retry = spool.enqueue_retry(msg_id, event["retry_msg_id"], detail=detail)
        record_once(dict(base, msg_id=retry.msg_id, state=QUEUED, retry_of=msg_id,
                         detail=f"{detail}:重试入队,继承槽位 {retry.queue_seq}"))
        steps.append(f"retry_enqueued:{retry.msg_id}")
    elif ruling != ABANDON_AND_CONTINUE:
        raise RulingError(f"未知的 ruling: {ruling!r}")
    if ruling in _RELEASES:
        record = spool.release(dst, event["queue_seq"], reason=ruling, ruling_id=rid)
        released = dict(base, state=QUEUE_RELEASED, msg_id=msg_id, detail=f"{detail}:放行")
        if not record.get("already_released"):
            steps.append("released")
            record_once(released)
        elif recovering and not any(e.get("state") == QUEUE_RELEASED and e.get("dst") == dst
                                    and e.get("queue_seq") == event["queue_seq"] for e in entries):
            # 放行已落盘、审计没写就崩溃了:补记。只在启动恢复时做(此时 broker 的 worker 还没开始);
            # 运行中遇到"已放行",是 broker 先自动放行了(裁定为已送达后),由它写带 ruling_id 的审计
            record_once(released)
    audit.record(dict(base, state=RULING_APPLIED, msg_id=msg_id, steps=steps,
                      detail="恢复时补做" if recovering else "裁定已生效"))
    return steps


def unapplied(audit: AuditLog) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """返回 (已落盘但未生效、未作废的裁定, 看起来是裁定但读不出来、未作废的损坏内容)。"""
    entries = audit.read()
    done = {e.get("ruling_id") for e in entries if e.get("state") == RULING_APPLIED}
    voided = {e.get("voided_ruling_id") for e in entries if e.get("state") == RULING_VOIDED}
    voided_corrupt = {e.get("voided_corrupt") for e in entries if e.get("state") == RULING_VOIDED}
    pending = [e for e in entries if e.get("state") == OPERATOR_RULING
               and e.get("ruling_id") not in done and e.get("ruling_id") not in voided]
    corrupt: List[Dict[str, str]] = []
    for e in entries:
        if e.get("state") == "CORRUPT_LINE" and e.get("mentions_ruling") and e["fingerprint"] not in voided_corrupt:
            corrupt.append({"fingerprint": e["fingerprint"], "where": "audit.jsonl 中间的损坏行",
                            "preview": e.get("detail", "")[:120]})
    for fragment in audit.quarantined():
        fp = fingerprint(fragment)
        if "OPERATOR_RULING" in fragment and fp not in voided_corrupt:
            corrupt.append({"fingerprint": fp, "where": audit.corrupt_path.name, "preview": fragment[:120]})
    return pending, corrupt


def void(audit: AuditLog, target: str, *, actor: str, reason: str, verified: str) -> Dict[str, Any]:
    """作废一条无法补做的裁定(08 §7.3 规则 8):只停止补做,不撤销已持久化的效果。

    target 是 ruling_id,或 "corrupt:<指纹>"(读不出的裁定)。verified 写作废前核实到的已生效效果。
    """
    if not reason.strip() or not verified.strip():
        raise RulingError("作废必须给出理由(--reason)和核实结果(--verified)")
    pending, corrupt = unapplied(audit)
    event: Dict[str, Any] = {"state": RULING_VOIDED, "actor": actor, "reason": reason, "verified": verified}
    if target.startswith("corrupt:"):
        fp = target.split(":", 1)[1]
        if fp not in {c["fingerprint"] for c in corrupt}:
            raise RulingError(f"没有待处理的损坏裁定 {fp}")
        event["voided_corrupt"] = fp
    else:
        if target not in {p["ruling_id"] for p in pending}:
            raise RulingError(f"没有未生效的裁定 {target}(已生效或已作废的不能再作废)")
        event["voided_ruling_id"] = target
    audit.record(event)
    return event


def reconcile(spool: Spool, audit: AuditLog) -> Tuple[int, Optional[str]]:
    """broker 启动时补做未生效的裁定。返回 (补做数量, 需要全局停止的原因或 None)。"""
    pending, corrupt = unapplied(audit)
    if corrupt:
        refs = ", ".join(f"corrupt:{c['fingerprint']}({c['where']})" for c in corrupt)
        return 0, f"审计日志里有读不出来的操作员裁定,不能猜测其结果:{refs};核实后用 a2a ruling void 作废"
    count = 0
    for event in pending:
        try:
            apply(spool, audit, event, recovering=True)
        except Exception as exc:
            return count, (f"补做裁定 {event.get('ruling_id')} 失败:{exc};"
                           f"核实实际状态后用 a2a ruling void {event.get('ruling_id')} 作废,再重新裁定")
        count += 1
    return count, None
