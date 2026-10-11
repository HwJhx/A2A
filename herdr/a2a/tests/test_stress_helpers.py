"""压测脚本里的纯函数(不启动 herdr / fnx,不调用模型)。"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

_PATH = Path(__file__).resolve().parent / "stress" / "real_fnx_scale.py"
_spec = importlib.util.spec_from_file_location("real_fnx_scale", _PATH)
real = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real)


class RateLimited(unittest.TestCase):
    def test_real_429_messages(self):
        # B1 实测:128 条全是这一种
        self.assertTrue(real.is_rate_limited("429 status code (no body)"))
        self.assertTrue(real.is_rate_limited("429 Too Many Requests"))
        self.assertTrue(real.is_rate_limited("  429 rate limit exceeded"))

    def test_other_errors_mentioning_429_are_not_rate_limits(self):
        for message in ("500 upstream error: retry after 429 ms", "400 invalid request: max_tokens 4290",
                        "Connection error", "", None, "4290 status code"):
            with self.subTest(message=message):
                self.assertFalse(real.is_rate_limited(message))


class ExtensionsListed(unittest.TestCase):
    SCREEN = ("jhx@ubuntu:~$ bash -c ...\n ForeNyx CLI · fnx_dv v0.4.7\n[Skills]\n  ic-a, ic-b\n\n"
              "[Extensions]\n  a2a_scale_1.ts\n\n────\n")

    def test_lists_only_the_extensions_section(self):
        self.assertEqual(real.extensions_listed(self.SCREEN), ["a2a_scale_1.ts"])

    def test_several_extensions_and_missing_section(self):
        self.assertEqual(real.extensions_listed(self.SCREEN.replace("a2a_scale_1.ts", "a2a_scale_1.ts, other.ts")),
                         ["a2a_scale_1.ts", "other.ts"])
        self.assertEqual(real.extensions_listed("no extensions here"), [])


if __name__ == "__main__":
    unittest.main()
