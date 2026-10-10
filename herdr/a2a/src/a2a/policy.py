"""Broker 的判定策略:prompt 返回分类、配置与退避。纯函数,不调用 herdr。

依据 herdr/claude/08-protocol.md:
  * §5 herdr 返回值的分类:一次 `agent prompt` 之后,先回答"这条 prompt 有没有可能已经提交"。
    能证明没提交的才可以重试;不能证明的一律进入 DELIVERY_UNCERTAIN。
  * §5 的"是(实测)"结论只对实测过的 herdr 版本成立(VERIFIED_HERDR_VERSIONS)。
    版本不在其中时,这些错误码退回"不确定"(semantics_verified=False)。
  * §3 末段:目标状态查询的退避与上限。
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Iterator, Mapping, Optional, Tuple

from .errors import HerdrBinaryNotFound, HerdrError, HerdrUsageError
from .messages import DELIVERY_UNCERTAIN, FAILED, RETRYING, TARGET_BLOCKED, TARGET_MISSING

# 08 §5 的"是(实测)"结论来自这些 herdr 版本(09 号文档 §7)。升级 herdr 后重跑
# herdr/claude/review-probes/probe_herdr_errors.py,结果一致再把新版本加进来。
VERIFIED_HERDR_VERSIONS: Tuple[str, ...] = ("0.9.3",)

# ---- prompt 返回的分类 ---------------------------------------------------
ACCEPTED = "accepted"            # herdr 接受了;还要经过观察窗口才能判 DELIVERED(08 §3)
NOT_SUBMITTED = "not_submitted"  # 能证明没有提交 -> RETRYING
BLOCKED = "blocked"              # 写入前拒绝,目标 blocked -> TARGET_BLOCKED
MISSING = "missing"              # 写入前查找目标失败 -> TARGET_MISSING
UNCERTAIN = "uncertain"          # 不能证明没提交 -> DELIVERY_UNCERTAIN
CONFIG_ERROR = "config_error"    # 配置或实现缺陷(命令行用法错误等),确定不可恢复 -> FAILED

# 分类 -> 调用之后消息要迁往的状态(ACCEPTED 不直接迁移,要先观察)
OUTCOME_TO_STATE: Mapping[str, str] = {
    NOT_SUBMITTED: RETRYING,
    BLOCKED: TARGET_BLOCKED,
    MISSING: TARGET_MISSING,
    UNCERTAIN: DELIVERY_UNCERTAIN,
    CONFIG_ERROR: FAILED,
}

# 只有在实测版本上才成立的分类(08 §5 "是(实测)" 那几行)
_VERIFIED_ONLY = {
    "agent_blocked": BLOCKED,
    "agent_not_found": MISSING,
    "agent_not_ready": NOT_SUBMITTED,
    "server_not_running": NOT_SUBMITTED,
}
# 与版本无关的分类
_ALWAYS = {
    "agent_prompt_failed": UNCERTAIN,   # 实测:返回时已经写入
    "agent_prompt_stalled": UNCERTAIN,  # herdr 文档:不代表没有发出
    "timeout": UNCERTAIN,
    "client_timeout": UNCERTAIN,        # 本进程等子进程超时:超时可能发生在提交之后
    "agent_name_taken": CONFIG_ERROR,   # rename 的错误,不属于 prompt 路径;出现即实现缺陷
    "invalid_agent_name": CONFIG_ERROR,
}


def semantics_verified(version: Optional[str]) -> bool:
    """当前 herdr 版本是否在 08 §5 的实测范围内。版本未知时返回 False(保守)。"""
    return version is not None and version in VERIFIED_HERDR_VERSIONS


def classify_prompt_result(error: Optional[BaseException], *, semantics_verified: bool) -> str:
    """把一次 `agent prompt` 的结果分类。error 为 None 表示 herdr 接受了。

    任何无法确认的情况都归为 UNCERTAIN:未知错误码、无法解析的输出、非 herdr 的异常。
    """
    if error is None:
        return ACCEPTED
    if isinstance(error, HerdrBinaryNotFound):
        return NOT_SUBMITTED  # 请求从未发出(08 §5 第一行)
    if isinstance(error, HerdrUsageError):
        return CONFIG_ERROR   # 命令行用法错误属于配置缺陷
    if not isinstance(error, HerdrError):
        return UNCERTAIN
    code = error.code or ""
    if code in _ALWAYS:
        return _ALWAYS[code]
    if code in _VERIFIED_ONLY:
        return _VERIFIED_ONLY[code] if semantics_verified else UNCERTAIN
    return UNCERTAIN


# ---- 配置 --------------------------------------------------------------
@dataclass(frozen=True)
class BrokerConfig:
    """Broker 的可配置参数。默认值都是**暂定**的(08 §3、§6、§9,10 号待确认表),待实测后调整。"""

    wait_ready_timeout_s: float = 300.0      # 等目标 READY 的总时限;也是状态查询持续失败的总上限
    observe_window_s: float = 30.0           # herdr 接受后,等目标开始处理(working / blocked)的窗口
    query_backoff_initial_s: float = 1.0     # 状态查询失败后的首次退避
    query_backoff_max_s: float = 30.0        # 单次退避上限
    query_max_consecutive_failures: int = 5  # 连续失败次数上限,达到即 TIMEOUT
    alert_remind_s: float = 3600.0           # 不确定态满 1 小时提醒
    alert_escalate_s: float = 86400.0        # 满 24 小时升级
    scan_interval_s: float = 1.0             # broker 扫描新消息与暂停队列的间隔
    error_cooldown_s: float = 30.0           # broker 内部错误(例如注册表读不出)后,该目标的冷却时间

    def __post_init__(self) -> None:
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{f.name} 必须是正数,收到 {value!r}")
        if self.query_backoff_initial_s > self.query_backoff_max_s:
            raise ValueError("query_backoff_initial_s 不能大于 query_backoff_max_s")
        if self.alert_remind_s > self.alert_escalate_s:
            raise ValueError("alert_remind_s 不能大于 alert_escalate_s")
        if not isinstance(self.query_max_consecutive_failures, int):
            raise ValueError("query_max_consecutive_failures 必须是整数")

    @classmethod
    def from_mapping(cls, data: Optional[Mapping[str, Any]]) -> "BrokerConfig":
        """从配置字典构造;未知键报错,避免拼错的配置项被悄悄忽略。"""
        data = dict(data or {})
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError(f"未知的 Broker 配置项: {', '.join(unknown)}")
        return cls(**data)


def backoff_delays(initial_s: float, max_s: float) -> Iterator[float]:
    """退避间隔:initial_s 起每次加倍,单次不超过 max_s。无限序列,由调用方按次数或总时限截断。"""
    if initial_s <= 0 or max_s <= 0:
        raise ValueError("退避间隔必须是正数")
    delay = initial_s
    while True:
        yield min(delay, max_s)
        delay = min(delay * 2, max_s)
