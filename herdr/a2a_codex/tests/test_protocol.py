"""a2a_codex 的状态迁移表必须与协议 v2 文档一致。"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from a2a_codex.messages import (
    ALLOWED_TRANSITIONS,
    ALL_STATES,
    DELIVERY_UNCERTAIN,
    FAILED,
    NON_TERMINAL_STATES,
    QUEUED,
    REJECTED,
    TERMINAL_STATES,
    WAITING_TARGET,
    can_transition,
)

PROTOCOL_DOC = Path(__file__).resolve().parents[2] / "claude" / "08-protocol.md"


def parse_doc_table(text: str) -> dict[str, frozenset[str]]:
    match = re.search(r"<!-- transitions:begin -->\s*```text\n(.*?)```\s*<!-- transitions:end -->", text, re.S)
    if not match:
        raise AssertionError("协议文档缺少机器可读的迁移表")
    table: dict[str, frozenset[str]] = {}
    for line in match.group(1).strip().splitlines():
        old, separator, targets = line.partition("->")
        if not separator:
            raise AssertionError(f"迁移行格式错误: {line}")
        table[old.strip()] = frozenset(targets.split())
    return table


def reachable(start: str) -> set[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        for target in ALLOWED_TRANSITIONS.get(stack.pop(), ()):
            if target not in seen:
                seen.add(target)
                stack.append(target)
    return seen


@unittest.skipUnless(PROTOCOL_DOC.exists(), "找不到 herdr/claude/08-protocol.md")
class ProtocolV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc_table = parse_doc_table(PROTOCOL_DOC.read_text(encoding="utf-8"))

    def test_code_table_equals_protocol_document(self) -> None:
        self.assertEqual(dict(ALLOWED_TRANSITIONS), self.doc_table)

    def test_state_sets_and_terminal_invariants(self) -> None:
        self.assertEqual(set(ALLOWED_TRANSITIONS), set(NON_TERMINAL_STATES))
        self.assertEqual(ALL_STATES, NON_TERMINAL_STATES | TERMINAL_STATES)
        self.assertFalse(NON_TERMINAL_STATES & TERMINAL_STATES)
        self.assertIn(DELIVERY_UNCERTAIN, NON_TERMINAL_STATES)
        self.assertIn(REJECTED, TERMINAL_STATES)
        for old, targets in ALLOWED_TRANSITIONS.items():
            self.assertNotIn(old, targets)
            self.assertNotIn(QUEUED, targets)
            self.assertNotIn(REJECTED, targets)
            self.assertIn(FAILED, targets)

    def test_protocol_graph_properties(self) -> None:
        for old in NON_TERMINAL_STATES:
            self.assertIn(FAILED, ALLOWED_TRANSITIONS[old])
            self.assertTrue(reachable(old) & TERMINAL_STATES)
        delivered_sources = {old for old, targets in ALLOWED_TRANSITIONS.items() if "DELIVERED" in targets}
        uncertain_sources = {old for old, targets in ALLOWED_TRANSITIONS.items()
                             if DELIVERY_UNCERTAIN in targets}
        self.assertEqual(delivered_sources, {"DISPATCHING", DELIVERY_UNCERTAIN})
        self.assertEqual(uncertain_sources, {"DISPATCHING"})
        self.assertFalse(can_transition(DELIVERY_UNCERTAIN, "DISPATCHING"))
        self.assertFalse(can_transition(DELIVERY_UNCERTAIN, "TIMEOUT"))

    def test_v2_removed_dispatching_to_waiting_and_added_retry_failure_edges(self) -> None:
        self.assertFalse(can_transition("DISPATCHING", WAITING_TARGET))
        self.assertTrue(can_transition("RETRYING", "TARGET_BLOCKED"))
        self.assertTrue(can_transition("RETRYING", "TARGET_MISSING"))

    def test_uncertain_cannot_be_retried_without_retrying_state(self) -> None:
        self.assertFalse(can_transition(DELIVERY_UNCERTAIN, "DISPATCHING"))
        self.assertTrue(can_transition(DELIVERY_UNCERTAIN, "RETRYING"))


if __name__ == "__main__":
    unittest.main()
