"""协议测试(协议版本 2,见 herdr/claude/08-protocol.md)。

分三部分:
  1. 迁移表的性质:**直接检查文档里的迁移表**。文档是被冻结的契约,这些测试不依赖代码,
     所以协议先于实现修订时它们照样有意义。
  2. 文档本身的约束:协议里必须明确写出的规则(用户确认的几条)、herdr 错误码必须被分类。
  3. 代码与文档的对照:`test_code_table_equals_the_document`。
     messages.py 已对齐协议版本 2。以后协议再修订时,先改文档,这条测试会失败,
     直到代码对齐;不要为了让它通过而回改文档。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from typing import Dict, FrozenSet, Set

from a2a import ALLOWED_TRANSITIONS

PROTOCOL_DOC = Path(__file__).resolve().parents[2] / "claude" / "08-protocol.md"

QUEUED, WAITING_TARGET, DISPATCHING = "QUEUED", "WAITING_TARGET", "DISPATCHING"
RETRYING, DELIVERY_UNCERTAIN = "RETRYING", "DELIVERY_UNCERTAIN"
DELIVERED, TARGET_BLOCKED, TARGET_MISSING = "DELIVERED", "TARGET_BLOCKED", "TARGET_MISSING"
TIMEOUT, FAILED, REJECTED = "TIMEOUT", "FAILED", "REJECTED"

NON_TERMINAL: FrozenSet[str] = frozenset({QUEUED, WAITING_TARGET, DISPATCHING, RETRYING, DELIVERY_UNCERTAIN})
TERMINAL: FrozenSet[str] = frozenset({DELIVERED, TARGET_BLOCKED, TARGET_MISSING, TIMEOUT, FAILED, REJECTED})
ALL: FrozenSet[str] = NON_TERMINAL | TERMINAL


def parse_doc_table(text: str) -> Dict[str, Set[str]]:
    match = re.search(r"<!-- transitions:begin -->\s*```text\n(.*?)```\s*<!-- transitions:end -->", text, re.S)
    if not match:
        raise AssertionError("08-protocol.md 里找不到机器可读的迁移表(transitions:begin / end 标记)")
    table: Dict[str, Set[str]] = {}
    for line in match.group(1).strip().splitlines():
        left, _, right = line.partition("->")
        table[left.strip()] = set(right.split())
    return table


def reachable(table: Dict[str, Set[str]], start: str) -> Set[str]:
    seen, stack = set(), [start]
    while stack:
        for nxt in table.get(stack.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def who_reaches(table: Dict[str, Set[str]], target: str) -> Set[str]:
    return {old for old, targets in table.items() if target in targets}


@unittest.skipUnless(PROTOCOL_DOC.exists(), "找不到 herdr/claude/08-protocol.md")
class DocumentTableProperties(unittest.TestCase):
    """对文档里的迁移表(协议版本 2)的性质检查。"""

    @classmethod
    def setUpClass(cls):
        cls.text = PROTOCOL_DOC.read_text(encoding="utf-8")
        cls.table = parse_doc_table(cls.text)

    def test_only_non_terminal_states_have_exits_and_all_names_are_known(self):
        self.assertEqual(set(self.table), set(NON_TERMINAL))
        for old, targets in self.table.items():
            self.assertTrue(targets <= ALL, f"{old} 的出口含未知状态 {targets - ALL}")
            self.assertNotIn(old, targets, "同一状态不属于迁移")

    def test_nothing_goes_back_to_queued_and_rejected_is_unreachable(self):
        for old, targets in self.table.items():
            self.assertNotIn(QUEUED, targets, old)
            self.assertNotIn(REJECTED, targets, old)

    def test_failed_is_reachable_from_every_non_terminal_state(self):
        for old in NON_TERMINAL:
            self.assertIn(FAILED, self.table[old], old)

    def test_every_non_terminal_state_can_reach_a_terminal_state(self):
        for old in NON_TERMINAL:
            self.assertTrue(reachable(self.table, old) & TERMINAL, f"{old} 走不到任何终态")

    # ---- 用户确认的规则 ------------------------------------------------
    def test_delivered_only_comes_from_dispatching_or_a_ruled_uncertain_delivery(self):
        self.assertEqual(who_reaches(self.table, DELIVERED), {DISPATCHING, DELIVERY_UNCERTAIN})
        for old in (QUEUED, WAITING_TARGET, RETRYING):
            self.assertNotIn(DELIVERED, self.table[old], old)

    def test_dispatching_is_a_write_ahead_marker_so_it_cannot_go_back_to_waiting(self):
        # 版本 2 删除了 DISPATCHING -> WAITING_TARGET:目标复核放在进入 DISPATCHING 之前
        self.assertNotIn(WAITING_TARGET, self.table[DISPATCHING])
        self.assertNotIn(QUEUED, self.table[DISPATCHING])
        self.assertEqual(self.table[DISPATCHING],
                         {DELIVERED, DELIVERY_UNCERTAIN, RETRYING, TARGET_BLOCKED, TARGET_MISSING, FAILED})
        # 回到等待只能经过 RETRYING("确定没发出")
        self.assertEqual(who_reaches(self.table, WAITING_TARGET), {QUEUED, RETRYING})

    def test_blocked_and_missing_fail_immediately_everywhere_before_or_without_delivery(self):
        # RETRYING 表示"确定没发出",发现 blocked / 目标不存在要立即失败(版本 2 新增两条边)
        for old in (QUEUED, WAITING_TARGET, DISPATCHING, RETRYING):
            self.assertIn(TARGET_BLOCKED, self.table[old], old)
            self.assertIn(TARGET_MISSING, self.table[old], old)
        self.assertEqual(who_reaches(self.table, TARGET_BLOCKED), {QUEUED, WAITING_TARGET, DISPATCHING, RETRYING})
        self.assertEqual(who_reaches(self.table, TARGET_MISSING), {QUEUED, WAITING_TARGET, DISPATCHING, RETRYING})

    def test_blocked_during_uncertain_delivery_never_changes_the_state(self):
        # DELIVERY_UNCERTAIN 期间目标 blocked 可能正是消息已送达的结果,不能标成"确定未送达"
        for forbidden in (TARGET_BLOCKED, TARGET_MISSING, TIMEOUT):
            self.assertNotIn(forbidden, self.table[DELIVERY_UNCERTAIN], forbidden)

    def test_uncertain_delivery_exits_are_exactly_delivered_retrying_failed(self):
        self.assertEqual(self.table[DELIVERY_UNCERTAIN], {DELIVERED, RETRYING, FAILED})

    def test_uncertain_delivery_can_only_resend_through_retrying(self):
        # 不能直接回到 DISPATCHING;重发必须先经过 RETRYING,而这条边在文档里有守卫(见下面的文档测试)
        self.assertNotIn(DISPATCHING, self.table[DELIVERY_UNCERTAIN])
        self.assertIn(DISPATCHING, self.table[RETRYING])
        self.assertEqual(who_reaches(self.table, RETRYING), {DISPATCHING, DELIVERY_UNCERTAIN})

    def test_only_a_dispatching_call_can_produce_an_uncertain_delivery(self):
        self.assertEqual(who_reaches(self.table, DELIVERY_UNCERTAIN), {DISPATCHING})

    def test_timeout_only_happens_while_waiting(self):
        self.assertEqual(who_reaches(self.table, TIMEOUT), {QUEUED, WAITING_TARGET, RETRYING})


@unittest.skipUnless(PROTOCOL_DOC.exists(), "找不到 herdr/claude/08-protocol.md")
class DocumentStatesRules(unittest.TestCase):
    """协议里必须明确写出的规则,以及 herdr 错误码必须被分类。"""

    @classmethod
    def setUpClass(cls):
        cls.text = PROTOCOL_DOC.read_text(encoding="utf-8")

    def lines_with(self, needle: str):
        return [line for line in self.text.splitlines() if needle in line]

    def test_every_state_is_described(self):
        for state in ALL:
            self.assertIn(f"`{state}`", self.text, f"协议文档没有描述状态 {state}")

    def test_protocol_version_and_alignment_status_are_declared(self):
        self.assertIn("协议版本 2", self.text)
        self.assertIn("对齐状态", self.text)

    def test_uncertain_delivery_never_auto_resends_by_default(self):
        self.assertTrue(any("默认不自动重发" in line for line in self.lines_with("DELIVERY_UNCERTAIN")))
        self.assertIn("操作员", self.text)
        self.assertIn("经真实 herdr 验证的机制", self.text)

    def test_manual_rulings_must_be_audited(self):
        self.assertIn("人工裁定必须记录审计", self.text)
        self.assertIn("OPERATOR_RULING", self.text)
        for field in ("actor", "ruling", "reason", "previous_state", "new_state"):
            self.assertIn(f"`{field}`", self.text, field)

    def test_blocked_rules_cover_retrying_and_uncertain(self):
        rule = next(line for line in self.text.splitlines() if line.startswith("5. **`blocked` 的处置"))
        self.assertTrue(rule)
        self.assertTrue(any("RETRYING" in line and "TARGET_BLOCKED" in line for line in self.lines_with("RETRYING")))
        self.assertTrue(any("DELIVERY_UNCERTAIN" in line and "只记入" in line and "不改变状态" in line
                            for line in self.text.splitlines()))

    def test_target_recheck_happens_before_dispatching_and_unknown_outcome_is_uncertain(self):
        self.assertIn("目标状态复核放在进入 `DISPATCHING` 之前", self.text)
        self.assertIn("调用 herdr 之后结果不明,一律进入 `DELIVERY_UNCERTAIN`", self.text)
        self.assertNotIn("`DISPATCHING → WAITING_TARGET` |", self.text.split("## 3.")[1].split("## 4.")[0])

    def test_only_errors_that_prove_non_submission_may_be_retried(self):
        self.assertIn("只有能证明 prompt 未提交的错误才能安全重试", self.text)
        self.assertIn("不能只按错误名分类", self.text)
        # 规则 7 必须与 §5 的实测结果一致,不能再说这些错误码"确认之前按不确定处理"
        rule = next(line for line in self.text.splitlines() if line.startswith("7. **只有能证明 prompt 未提交"))
        self.assertNotIn("确认之前", rule)
        self.assertIn("herdr 0.9.3", rule)
        self.assertIn("agent_prompt_failed", rule)

    def test_state_change_seq_is_only_a_candidate_pending_verification(self):
        mentions = self.lines_with("`state_change_seq`")
        self.assertTrue(mentions)
        self.assertTrue(any("待验证" in line or "尚未验证" in line for line in mentions) or
                        "候选依据,待验证" in self.text)
        self.assertIn("不得作为自动核实机制使用", self.text)
        self.assertIn("不能自动触发任何迁移", self.text)

    def test_accepted_but_not_observed_goes_to_uncertain_not_delivered(self):
        self.assertTrue(any("herdr 接受了" in line and "DELIVERY_UNCERTAIN" in line for line in self.text.splitlines()))
        self.assertIn("accepted_and_observed", self.text)
        self.assertIn("operator_confirmed", self.text)

    def test_every_herdr_error_code_the_client_knows_is_classified(self):
        from a2a.errors import CODE_TO_EXCEPTION

        codes = set(CODE_TO_EXCEPTION) | {"client_timeout", "agent_not_found"}
        self.assertTrue(codes)
        section = self.text.split("## 5.")[1].split("## 6.")[0]
        for code in sorted(codes):
            self.assertIn(f"`{code}`", section, f"§5 的错误分类表没有 herdr 错误码 {code}")

    def test_error_semantics_are_backed_by_real_herdr_measurements(self):
        # 2026-10-09 在 herdr 0.9.3 上实测(09 号文档 §7):四个错误码都没有写入
        section = self.text.split("## 5.")[1].split("## 6.")[0]
        expected = {"agent_blocked": "TARGET_BLOCKED", "agent_not_ready": "RETRYING",
                    "server_not_running": "RETRYING", "agent_not_found": "TARGET_MISSING"}
        for code, disposition in expected.items():
            row = next(line for line in section.splitlines() if line.startswith("| `" + code))
            self.assertIn("**是(实测)**", row, code)
            self.assertEqual(row.rstrip(" |").split("|")[-1].strip().split("(")[0].strip("` "), disposition, code)
        # 实测"返回错误但已写入"的错误码必须走 DELIVERY_UNCERTAIN
        row = next(line for line in section.splitlines() if line.startswith("| `agent_prompt_failed`"))
        self.assertIn("**否(实测:已写入)**", row)
        self.assertTrue(row.rstrip().endswith("`DELIVERY_UNCERTAIN` |"))
        # 结论绑定 herdr 版本,升级后必须重测
        self.assertIn("只对 **herdr 0.9.3** 成立", section)
        self.assertIn("probe_herdr_errors.py", section)

    def test_missing_target_is_provable_only_when_checked_before_the_call(self):
        section = self.text.split("## 5.")[1].split("## 6.")[0]
        row = next(line for line in section.splitlines() if "调用 herdr 之前" in line and "不存在" in line)
        self.assertIn("**是**", row)
        self.assertIn("TARGET_MISSING", row)

    def test_time_never_moves_an_uncertain_delivery(self):
        text = self.text
        self.assertIn("没有由时间触发的出口", text)
        self.assertIn("**不提供**\"超时后自动放弃并放行\"的开关", text)
        self.assertIn("时间只能触发告警,不能触发 `DELIVERY_UNCERTAIN` 的状态迁移", text)
        self.assertIn("只告警,不是状态迁移", text)
        self.assertNotIn("保留期", text)

    def test_queue_head_model_is_per_target_and_not_based_on_state_names(self):
        rule = next(line for line in self.text.splitlines() if line.startswith("12. "))
        for phrase in ("只处理队列头", "只作用于这一个目标", "不是某一时刻的状态名", "仍占住队列头",
                       "只有 `DELIVERED` 自动放行", "没有任何由时间触发的放行"):
            self.assertIn(phrase, rule)

    def section7(self):
        return self.text.split("\n## 7.")[1].split("\n## 8.")[0]

    def test_definite_failures_pause_the_queue_until_an_operator_acts(self):
        row = next(line for line in self.section7().splitlines() if "确定失败终态" in line and line.lstrip().startswith("|"))
        for state in ("FAILED", "TIMEOUT", "TARGET_BLOCKED", "TARGET_MISSING"):
            self.assertIn(f"`{state}`", row)
        self.assertIn("不自动放行", row)

    def test_nothing_but_an_operator_moves_uncertain_to_failed(self):
        self.assertIn("`DELIVERY_UNCERTAIN → FAILED` 只能由操作员放弃触发", self.text)
        section = self.section7().split("### 7.2")[1].split("### 7.3")[0]
        for event in ("崩溃", "spool", "审计写入失败", "拓扑删除", "注销", "通用异常处理"):
            self.assertIn(event, section)
        self.assertIn("不得跳到下一条", section)

    def test_operator_actions_retry_and_abandon(self):
        section = self.section7().split("### 7.3")[1].split("### 7.4")[0]
        self.assertIn("**不新建消息**", section)          # 不确定态的重试直接转 RETRYING
        self.assertIn("继承原 `queue_seq`", section)     # 终态后的重试新建消息
        self.assertIn("`retry_of`", section)
        self.assertIn("重试与放弃并继续互斥", section)
        self.assertIn("重试 `TARGET_BLOCKED` 不违反 D8", section)
        self.assertIn("裁定先落盘,再生效", section)

    def test_queue_seq_semantics(self):
        section = self.section7().split("### 7.4")[1].split("### 7.5")[0]
        for phrase in ("持久、单调递增", "原子分配", "不重排、不复用", "**不作为**严格顺序键",
                       "最多一条未终结的消息", "不投递 `queue_seq` 更大的消息"):
            self.assertIn(phrase, section)

    # ---- 跨章节一致性(补充决议三) --------------------------------------
    def test_d8_wording_is_consistent_everywhere(self):
        # 凡是说"重试"受 D8 约束的地方,都必须限定为"自动",不能出现无条件的"不重试"
        self.assertNotIn("不重试", self.text)
        for line in self.text.splitlines():
            if "D8" in line and "重试" in line:
                self.assertIn("自动", line, line[:80])
        state_row = next(line for line in self.text.splitlines() if line.startswith("| `TARGET_BLOCKED` |"))
        self.assertIn("操作员显式裁定的重试是例外", state_row)

    def ruling_table(self):
        part = self.text.split("`ruling` 的取值(与")[1].split("`reject_code` 取值")[0]
        rows = [line for line in part.splitlines() if line.startswith("| `") and not line.startswith("| `ruling` |")]
        return {line.split("|")[1].strip().strip("`"): [c.strip() for c in line.split("|")[1:-1]] for line in rows}

    def test_ruling_values_cover_every_operator_action_for_every_head_state(self):
        table = self.ruling_table()
        self.assertEqual(set(table), {"delivered", "not_delivered_retry", "abandon", "retry_terminal", "abandon_and_continue"})
        actions = self.section7().split("### 7.3")[1].split("### 7.4")[0]
        for ruling, cells in table.items():
            self.assertIn(cells[1], actions, f"{ruling} 对应的动作 {cells[1]} 不在 §7.3")
        # 动作 × 队列头状态 两两组合都有且只有一个 ruling(裁定已送达只对不确定态)
        pairs = {(cells[1], "终态" in cells[2]) for cells in table.values()}
        self.assertEqual(len(pairs), len(table))
        self.assertEqual(pairs, {("裁定已送达", False), ("重试", False), ("放弃并继续", False),
                                 ("重试", True), ("放弃并继续", True)})

    def test_each_ruling_means_exactly_what_it_should(self):
        # 逐项锁定:两个合法迁移被对调也要能发现
        expected = {
            #  ruling: (动作, 队列头是终态?, 迁移(None=状态不变), 放行?, 必须出现的附加字段)
            "delivered": ("裁定已送达", False, (DELIVERY_UNCERTAIN, DELIVERED), True, ("operator_confirmed",)),
            "not_delivered_retry": ("重试", False, (DELIVERY_UNCERTAIN, RETRYING), False, ()),
            "abandon": ("放弃并继续", False, (DELIVERY_UNCERTAIN, FAILED), True, ()),
            "retry_terminal": ("重试", True, None, False, ("retry_msg_id", "QUEUED", "queue_seq")),
            "abandon_and_continue": ("放弃并继续", True, None, True, ()),
        }
        table = self.ruling_table()
        self.assertEqual(set(table), set(expected))
        for ruling, (action, terminal, move, releases, fields) in expected.items():
            cells = table[ruling]
            self.assertEqual(cells[1], action, ruling)
            self.assertEqual("终态" in cells[2], terminal, ruling)
            if move is None:
                self.assertIn("不变", cells[3], ruling)
            else:
                self.assertEqual(tuple(re.findall(r"`([A-Z_]+)`", cells[3])), move, ruling)
            self.assertEqual(cells[5].startswith("是"), releases, ruling)
            for field in fields:
                self.assertIn(field, cells[4], ruling)

    def test_ruling_side_effects_are_idempotent_and_fail_closed(self):
        rules = self.section7().split("### 7.3")[1].split("### 7.4")[0]
        self.assertIn("都以 `ruling_id` 幂等", rules)
        self.assertIn("**只在全部效果都可靠持久化之后**写入", rules)
        self.assertIn("裁定记录读不出来时 fail-closed", rules)
        self.assertIn("不得猜测裁定结果", rules)
        self.assertIn("受信任的重试入队路径", rules)
        # fail-closed 必须有可审计的人工解除流程,否则目标可能永久停住
        self.assertIn("解除 fail-closed 的人工流程", rules)
        self.assertIn("只停止补做,不撤销已持久化的效果", rules)
        # 作废后不能笼统"回到等待裁定":旧裁定可能已部分生效,必须按实际持久化状态处理
        self.assertNotIn("回到\"等待裁定\"", rules)
        self.assertIn("**不把目标\"回到\"任何状态**", rules)
        self.assertIn("**已持久化的实际状态**", rules)
        rule8 = rules.split("8. **解除 fail-closed 的人工流程。**")[1].split("### ")[0]
        for case in ("都没有生效", "已迁到 `DELIVERED`", "已迁到 `RETRYING`", "已迁到 `FAILED`", "槽位已有放行记录"):
            self.assertIn(case, rule8)
        self.assertIn("不再新建重试消息", rule8)
        self.assertIn("不能再对旧槽位重试", rule8)
        for event in ("RULING_VOIDED", "DISPATCH_RESUMED"):
            self.assertIn(event, rules)
            self.assertTrue(any(line.startswith("| `" + event) or ("`" + event + "`" in line and line.startswith("| `"))
                                for line in self.text.split("\n## 9.")[1].splitlines()), f"§9 没有定义 {event}")
        recovery = self.text.split("\n## 8.")[1].split("\n## 9.")[0]
        self.assertIn("RULING_VOIDED", recovery)
        # 裁定触发的队列事件必须带 ruling_id
        queue_row = next(line for line in self.text.splitlines() if line.startswith("| `QUEUE_PAUSED` / `QUEUE_RELEASED` |"))
        self.assertIn("由裁定触发时必须带 `ruling_id`", queue_row)
        self.assertIn("RULING_APPLIED", queue_row)
        queued_row = next(line for line in self.text.splitlines() if line.startswith("| `QUEUED` |"))
        self.assertIn("重试入队", queued_row)
        self.assertIn("操作员不直接设置消息状态", queued_row)

    def test_ruling_transitions_match_the_transition_table(self):
        documented = parse_doc_table(self.text)
        for ruling, cells in self.ruling_table().items():
            if "不变" in cells[3]:
                continue
            old, new = re.findall(r"`([A-Z_]+)`", cells[3])
            self.assertIn(new, documented[old], f"{ruling}: {old} -> {new} 不在迁移表里")

    def test_terminal_retry_keeps_the_old_state_and_names_the_new_message(self):
        cells = self.ruling_table()["retry_terminal"]
        self.assertIn("不变", cells[3])
        for field in ("retry_msg_id", "QUEUED", "queue_seq"):
            self.assertIn(field, cells[4])
        self.assertTrue(cells[5].startswith("否"))
        self.assertIn("`ruling_id`", self.text)
        self.assertIn("RULING_APPLIED", self.text)

    def test_only_resolving_rulings_release_the_queue(self):
        for ruling, cells in self.ruling_table().items():
            releases = cells[5].startswith("是")
            self.assertEqual(releases, ruling in {"delivered", "abandon", "abandon_and_continue"}, ruling)

    def test_crash_recovery_covers_the_queue_rules(self):
        recovery = self.text.split("\n## 8.")[1].split("\n## 9.")[0]
        for phrase in ("`DELIVERY_UNCERTAIN`", "`ruling_id`", "幂等补做", "放行记录", "全局停止投递", "不得跳到下一条"):
            self.assertIn(phrase, recovery)
        section = self.section7().split("### 7.2")[1].split("### 7.3")[0]
        self.assertIn("全局停止投递", section)

    def test_archiving_a_failed_head_does_not_release_it(self):
        rule = next(line for line in self.section7().splitlines() if line.startswith("5. `REJECTED`"))
        for phrase in ("这不等于放行", "**持久记录**", "重启后必须能恢复"):
            self.assertIn(phrase, rule)

    def test_terminal_retry_inherits_authorisation(self):
        rule = next(line for line in self.section7().splitlines() if line.startswith("3. **新建的重试消息"))
        self.assertIn("不按当前拓扑重新鉴权", rule)
        self.assertIn("复核目标", rule)
        self.assertNotIn("待设计", rule)

    def test_fifo_ordering_key_is_queue_seq_everywhere(self):
        # 规则 10 与 §7.1 / §7.4 必须用同一个排序键;msg_id 只是标识
        rule = next(line for line in self.text.splitlines() if line.startswith("10. "))
        self.assertIn("按 `queue_seq` 顺序", rule)
        self.assertIn("`msg_id` 只是唯一标识,不作排序键", rule)
        for line in self.text.splitlines():
            if line.startswith(("- **", "  - ")):
                continue  # 修订记录是历史,不检查
            self.assertNotRegex(line, r"按 `msg_id`[^,。]*(顺序|排序|FIFO)(?!.*没有 `queue_seq`)", line[:80])
        self.assertIn("按 `queue_seq`", self.section7().split("### 7.1")[1].split("### 7.2")[0])

    def test_future_extensions_are_not_in_v1(self):
        section = self.section7().split("### 7.5")[1]
        for phrase in ("v1 不做", "`CANCELLED`", "`continue_on_failure`", "`workflow_id`"):
            self.assertIn(phrase, self.section7() if phrase == "v1 不做" else section)

    def test_verified_not_delivered_goes_to_retrying_and_failed_is_for_abandon(self):
        section = self.text.split("## 6.")[1].split("## 7.")[0]
        row = next(line for line in section.splitlines() if "确认未送达" in line and "verified" not in line.lower()
                   and line.startswith("| 经真实 herdr"))
        self.assertIn("RETRYING", row)
        self.assertNotIn("→ FAILED", row)
        self.assertIn("在 `DELIVERY_UNCERTAIN` 下,`FAILED` **只能来自操作员放弃**", section)
        self.assertNotIn("确定不可恢复的错误(例如配置缺陷) | `→ FAILED`", section)

    def test_state_query_failures_have_backoff_and_caps_and_end_in_timeout(self):
        section = self.text.split("## 3.")[1].split("## 4.")[0]
        for phrase in ("退避", "连续失败次数", "总等待时限", "TIMEOUT"):
            self.assertIn(phrase, section)
        self.assertIn("状态查询持续失败", self.text.split("## 2.")[0] + section)

    def test_accepted_and_observed_is_operational_evidence_not_a_receipt(self):
        self.assertIn("操作性证据", self.text)
        self.assertIn("与 `msg_id` 绑定的送达回执", self.text)
        self.assertIn("暂定 30 秒", self.text)

    def test_alert_policy_is_provisional_and_not_a_transition(self):
        self.assertIn("1 小时", self.text)
        self.assertIn("24 小时", self.text)
        self.assertIn("暂定,可配置;只是告警,不是状态迁移", self.text)

    def test_documented_id_formats_match_the_code(self):
        from a2a.messages import AGENT_ID_RE, MSG_ID_RE

        self.assertIn(MSG_ID_RE.pattern, self.text)
        self.assertIn(AGENT_ID_RE.pattern, self.text)

    def test_every_reject_code_used_by_the_router_is_documented(self):
        from a2a import router

        codes = {value for name, value in vars(router).items() if name.startswith("REJECT_")}
        self.assertTrue(codes)
        for code in codes:
            self.assertIn(f"`{code}`", self.text, f"协议文档没有描述拒绝代码 {code}")


@unittest.skipUnless(PROTOCOL_DOC.exists(), "找不到 herdr/claude/08-protocol.md")
class CodeMatchesDocument(unittest.TestCase):
    def test_code_table_equals_the_document(self):
        """代码迁移表必须等于文档迁移表。协议修订后、代码对齐前,这条测试会失败(有意为之)。"""
        documented = parse_doc_table(PROTOCOL_DOC.read_text(encoding="utf-8"))
        in_code = {old: set(targets) for old, targets in ALLOWED_TRANSITIONS.items()}
        self.assertEqual(
            documented, in_code,
            "messages.ALLOWED_TRANSITIONS 与 08-protocol.md 的迁移表不一致。"
            "协议是被冻结的契约,请修改 messages.py 使其对齐(不要回改文档)。")


if __name__ == "__main__":
    unittest.main()
