"""Broker 判定策略(policy.py)的测试,含与 08-protocol.md §5 分类表的逐行对照。"""
from __future__ import annotations

import itertools
import re
import subprocess
import unittest
from pathlib import Path

from a2a.errors import (
    CODE_TO_EXCEPTION,
    HerdrBinaryNotFound,
    HerdrError,
    HerdrNotFound,
    HerdrPromptFailed,
    HerdrUsageError,
    classify,
)
from a2a.herdr_client import herdr_version
from a2a.policy import (
    ACCEPTED,
    BLOCKED,
    CONFIG_ERROR,
    MISSING,
    NOT_SUBMITTED,
    OUTCOME_TO_STATE,
    UNCERTAIN,
    VERIFIED_HERDR_VERSIONS,
    BrokerConfig,
    backoff_delays,
    classify_prompt_result,
    semantics_verified,
)

PROTOCOL_DOC = Path(__file__).resolve().parents[2] / "claude" / "08-protocol.md"


def err(code):
    return classify(code, "x")


class ErrorMapping(unittest.TestCase):
    def test_prompt_failed_and_not_found_are_mapped_explicitly(self):
        self.assertIs(CODE_TO_EXCEPTION["agent_prompt_failed"], HerdrPromptFailed)
        self.assertIs(CODE_TO_EXCEPTION["agent_not_found"], HerdrNotFound)
        self.assertIsInstance(err("agent_prompt_failed"), HerdrPromptFailed)


class ClassifyPromptResult(unittest.TestCase):
    def c(self, error, verified=True):
        return classify_prompt_result(error, semantics_verified=verified)

    def test_success_is_only_accepted_not_delivered(self):
        self.assertEqual(self.c(None), ACCEPTED)
        self.assertNotIn(ACCEPTED, OUTCOME_TO_STATE)  # 接受之后还要观察,不能直接 DELIVERED

    def test_verified_semantics(self):
        self.assertEqual(self.c(err("agent_blocked")), BLOCKED)
        self.assertEqual(self.c(err("agent_not_found")), MISSING)
        self.assertEqual(self.c(err("agent_not_ready")), NOT_SUBMITTED)
        self.assertEqual(self.c(err("server_not_running")), NOT_SUBMITTED)

    def test_unverified_herdr_version_falls_back_to_uncertain(self):
        for code in ("agent_blocked", "agent_not_found", "agent_not_ready", "server_not_running"):
            self.assertEqual(self.c(err(code), verified=False), UNCERTAIN, code)

    def test_written_or_unknown_outcomes_are_uncertain_regardless_of_version(self):
        for verified in (True, False):
            for code in ("agent_prompt_failed", "agent_prompt_stalled", "timeout", "client_timeout",
                         "some_new_code", None):
                self.assertEqual(self.c(HerdrError("x", code=code), verified), UNCERTAIN, (code, verified))
            self.assertEqual(self.c(RuntimeError("boom"), verified), UNCERTAIN)
            self.assertEqual(self.c(HerdrError("无法解析的输出"), verified), UNCERTAIN)

    def test_request_never_sent_and_config_defects(self):
        self.assertEqual(self.c(HerdrBinaryNotFound("x"), verified=False), NOT_SUBMITTED)
        self.assertEqual(self.c(HerdrUsageError("x")), CONFIG_ERROR)
        self.assertEqual(self.c(err("agent_name_taken")), CONFIG_ERROR)
        self.assertEqual(self.c(err("invalid_agent_name")), CONFIG_ERROR)

    def test_every_known_code_is_classified_deliberately(self):
        # 映射表里的每个错误码都要有明确分类(不是靠"未知码 -> 不确定"的兜底)
        from a2a import policy

        known = set(policy._ALWAYS) | set(policy._VERIFIED_ONLY)
        self.assertTrue(set(CODE_TO_EXCEPTION) <= known, set(CODE_TO_EXCEPTION) - known)

    def test_version_check(self):
        self.assertTrue(semantics_verified("0.9.3"))
        self.assertIn("0.9.3", VERIFIED_HERDR_VERSIONS)
        for version in ("0.9.4", "1.0.0", "", None):
            self.assertFalse(semantics_verified(version), version)


@unittest.skipUnless(PROTOCOL_DOC.exists(), "找不到 08-protocol.md")
class MatchesProtocolSection5(unittest.TestCase):
    """逐行对照 08 §5 表格:每个出现错误码的行,其"处置"列的状态必须等于分类函数给出的状态。"""

    def test_each_row_matches_the_classifier(self):
        text = PROTOCOL_DOC.read_text(encoding="utf-8")
        section = text.split("## 5.")[1].split("## 6.")[0]
        checked = 0
        for line in section.splitlines():
            if not line.startswith("| ") or line.startswith("| ---") or line.startswith("| herdr 返回"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            codes = re.findall(r"`([a-z_]+)`", cells[0])
            if not codes:
                continue
            states = re.findall(r"`([A-Z_]+)`", cells[-1])
            self.assertTrue(states, line[:60])
            for code in codes:
                outcome = classify_prompt_result(HerdrError("x", code=code), semantics_verified=True)
                self.assertEqual(OUTCOME_TO_STATE[outcome], states[0], f"{code}: 文档 {states[0]},代码 {outcome}")
                checked += 1
        self.assertGreaterEqual(checked, 9)


class Config(unittest.TestCase):
    def test_defaults_match_the_protocol(self):
        cfg = BrokerConfig()
        self.assertEqual((cfg.wait_ready_timeout_s, cfg.observe_window_s), (300.0, 30.0))
        self.assertEqual((cfg.query_backoff_initial_s, cfg.query_backoff_max_s, cfg.query_max_consecutive_failures),
                         (1.0, 30.0, 5))
        self.assertEqual((cfg.alert_remind_s, cfg.alert_escalate_s), (3600.0, 86400.0))

    def test_no_retry_count_cap_exists(self):
        # 用户确认:不加"RETRYING 次数上限转 FAILED",由总等待时限转 TIMEOUT 兜底
        self.assertFalse(any("retry" in f for f in BrokerConfig.__dataclass_fields__))

    def test_validation(self):
        for bad in ({"observe_window_s": 0}, {"wait_ready_timeout_s": -1}, {"query_max_consecutive_failures": True},
                    {"query_max_consecutive_failures": 2.5}, {"query_backoff_initial_s": 60},
                    {"alert_remind_s": 90000}, {"observe_window_s": "30"}):
            with self.assertRaises(ValueError, msg=bad):
                BrokerConfig.from_mapping(bad)
        with self.assertRaises(ValueError):
            BrokerConfig.from_mapping({"observe_windows": 10})  # 拼错的键不能被悄悄忽略
        self.assertEqual(BrokerConfig.from_mapping({"observe_window_s": 5}).observe_window_s, 5)
        self.assertEqual(BrokerConfig.from_mapping(None), BrokerConfig())

    def test_backoff_sequence(self):
        self.assertEqual(list(itertools.islice(backoff_delays(1, 30), 8)), [1, 2, 4, 8, 16, 30, 30, 30])
        with self.assertRaises(ValueError):
            next(backoff_delays(0, 30))


class HerdrVersion(unittest.TestCase):
    def runner(self, out, rc=0):
        return lambda argv, timeout: subprocess.CompletedProcess(argv, rc, out, "")

    def test_parses_version(self):
        self.assertEqual(herdr_version(runner=self.runner("herdr 0.9.3\n")), "0.9.3")
        self.assertEqual(herdr_version(runner=self.runner("herdr v1.2.0-beta\n")), "1.2.0-beta")

    def test_unparseable_or_failing(self):
        with self.assertRaises(HerdrError):
            herdr_version(runner=self.runner("something else"))
        with self.assertRaises(HerdrError):
            herdr_version(runner=self.runner("herdr 0.9.3", rc=1))

    def test_missing_binary(self):
        def missing(argv, timeout):
            raise FileNotFoundError

        with self.assertRaises(HerdrBinaryNotFound):
            herdr_version(runner=missing)


if __name__ == "__main__":
    unittest.main()
