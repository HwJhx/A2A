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


GUARD = Path(__file__).resolve().parent / "pi-ext" / "a2a-probe-guard.ts"


def _node_ts():
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        return None
    ok = subprocess.run([node, "-e", "process.exit(Number(process.features.typescript ? 0 : 1))"],
                        capture_output=True).returncode == 0
    return node if ok else None


@unittest.skipUnless(_node_ts(), "需要能直接加载 .ts 的 node")
class ProbeGuard(unittest.TestCase):
    """探测 8 的执行前限制(Codex 审核):只放行 bash 执行 echo probe8,其他工具调用在执行前被拦下。"""

    def decide(self, calls, enabled=True):
        import json
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            if enabled:
                (Path(d) / "probe-guard.enabled").touch()
            script = (f"const m = await import({json.dumps(GUARD.as_uri())});"
                      "const h = []; m.default({ on: (e, f) => e === 'tool_call' && h.push(f) });"
                      f"const out = []; for (const [t, i] of {json.dumps(calls)}) "
                      "out.push(h.length ? ((await h[0]({ toolName: t, input: i })) ?? null) : 'no-hook');"
                      "console.log(JSON.stringify(out));")
            proc = subprocess.run([_node_ts(), "--input-type=module", "-e", script], capture_output=True, text=True,
                                  env={"A2A_STATE_DIR": d, "PATH": "/usr/bin:/bin"}, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout)

    def test_only_echo_probe8_is_allowed(self):
        calls = [["bash", {"command": "echo probe8"}], ["bash", {"command": "  echo probe8\n"}],
                 ["bash", {"command": "echo probe8; rm -rf ~"}], ["bash", {"command": "rm -rf /tmp/x"}],
                 ["edit", {"path": "/etc/hosts"}], ["write", {"path": "x", "content": "y"}],
                 ["read", {"path": "/etc/passwd"}], ["a2a_send", {"edge_id": "dv_done"}]]
        out = self.decide(calls)
        self.assertEqual(out[:2], [None, None])
        for decision in out[2:]:
            self.assertTrue(decision["block"], decision)

    def test_inactive_without_the_marker_file(self):
        self.assertEqual(self.decide([["bash", {"command": "rm -rf x"}]], enabled=False), ["no-hook"])


if __name__ == "__main__":
    unittest.main()
