"""launcher 单元测试:启动命令的构造,以及启动脚本预检。"""
from __future__ import annotations

import os
import shlex
import stat
import tempfile
import unittest

from a2a import LauncherError, build_launch_command, preflight_launcher
from a2a.launcher import resolve_launcher_path


# 这个字符串与在虚拟机里实测通过的命令完全一致(见 herdr/claude/06-manual-walkthrough.md)
TESTED = """bash -c 'exec(){ builtin exec -a pi "$@"; }; source "/home/jhx/.forenyx/fnx_dv/bin/fnx_dv"'"""


class TestBuildLaunchCommand(unittest.TestCase):
    def test_matches_the_command_verified_on_the_vm(self):
        self.assertEqual(build_launch_command("/home/jhx/.forenyx/fnx_dv/bin/fnx_dv"), TESTED)

    def test_is_parsed_by_shell_into_expected_words(self):
        words = shlex.split(build_launch_command("/opt/a b/fnx_dv"))
        self.assertEqual(words[0:2], ["bash", "-c"])
        self.assertEqual(len(words), 3)  # bash -c <一个整体的脚本>
        self.assertIn('source "/opt/a b/fnx_dv"', words[2])
        self.assertIn("builtin exec -a pi", words[2])

    def test_arguments_are_forwarded_through_dollar_at(self):
        cmd = build_launch_command("/x/fnx_dv", ["--session", "abc def", "it's"])
        words = shlex.split(cmd)
        self.assertEqual(words[0:2], ["bash", "-c"])
        self.assertTrue(words[2].endswith('source "/x/fnx_dv" "$@"'))
        self.assertEqual(words[3], "_")
        self.assertEqual(words[4:], ["--session", "abc def", "it's"])  # 含空格、单引号的参数原样保留

    def test_no_args_has_no_dollar_at_forwarding(self):
        words = shlex.split(build_launch_command("/x/fnx_dv"))
        self.assertFalse(words[2].endswith('"$@"'))

    def test_rejects_relative_path(self):
        with self.assertRaises(LauncherError):
            build_launch_command("fnx_dv")
        with self.assertRaises(LauncherError):
            build_launch_command("./bin/fnx_dv")

    def test_rejects_dangerous_characters(self):
        for bad in ("/x/y'z", '/x/y"z', "/x/y`z", "/x/y\\z", "/x/y\nz", "/x/y;z$(id)"):
            with self.assertRaises(LauncherError, msg=bad):
                build_launch_command(bad)

    def test_expands_home_and_tilde(self):
        home = os.environ.get("HOME", "")
        if not home or any(c in home for c in "'\"$`\\"):
            self.skipTest("HOME 不适合此测试")
        self.assertEqual(resolve_launcher_path("~/bin/x"), os.path.join(home, "bin/x"))
        self.assertEqual(resolve_launcher_path("$HOME/bin/x"), os.path.join(home, "bin/x"))


class TestPreflight(unittest.TestCase):
    def write(self, text: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".sh")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path

    def test_good_script(self):
        p = self.write('#!/bin/bash\nFOO=1\nexport FOO\nexec "$DIR/forenyx-cli" "$@"\n')
        self.assertEqual(preflight_launcher(p), [])

    def test_two_execs_rejected(self):
        p = self.write('#!/bin/bash\nif [ x ]; then\n  exec "$A" "$@"\nfi\nexec "$B" "$@"\n')
        problems = preflight_launcher(p)
        self.assertEqual(len(problems), 1)
        self.assertIn("2 处 exec", problems[0])

    def test_no_exec_rejected(self):
        p = self.write('#!/bin/bash\n"$DIR/forenyx-cli" "$@"\n')
        self.assertIn("没有找到 exec", preflight_launcher(p)[0])

    def test_comments_are_ignored(self):
        p = self.write('#!/bin/bash\n# 这里提到 exec 和 $0 和 BASH_SOURCE 都只是注释\nexec "$X" "$@"\n')
        self.assertEqual(preflight_launcher(p), [])

    def test_dollar_zero_and_bash_source_rejected(self):
        p = self.write('#!/bin/bash\nD=$(dirname "$0")\nexec "$D/x" "$@"\n')
        self.assertTrue(any("$0" in x for x in preflight_launcher(p)))
        p = self.write('#!/bin/bash\nD=$(dirname "${BASH_SOURCE[0]}")\nexec "$D/x" "$@"\n')
        self.assertTrue(any("BASH_SOURCE" in x for x in preflight_launcher(p)))

    def test_positional_dollar_one_is_not_confused_with_dollar_zero(self):
        p = self.write('#!/bin/bash\nX="$1"; Y="$10"; Z="${1}"\nexec "$X" "$@"\n')
        self.assertEqual(preflight_launcher(p), [])

    def test_exec_word_inside_other_words_is_not_counted(self):
        p = self.write('#!/bin/bash\nexecutable=1\nmyexec=2\necho "executing"\nexec "$X" "$@"\n')
        self.assertEqual(preflight_launcher(p), [])

    def test_exec_after_semicolon_is_counted(self):
        p = self.write('#!/bin/bash\ntrue; exec "$X" "$@"\n')
        self.assertEqual(preflight_launcher(p), [])
        p = self.write('#!/bin/bash\ntrue; exec "$X" "$@"\nexec "$Y"\n')
        self.assertIn("2 处 exec", preflight_launcher(p)[0])

    def test_missing_file(self):
        self.assertIn("不存在", preflight_launcher("/no/such/launcher")[0])

    def test_relative_path_reported_as_problem_not_exception(self):
        problems = preflight_launcher("fnx_dv")
        self.assertEqual(len(problems), 1)
        self.assertIn("绝对路径", problems[0])

    def test_real_fnx_launchers_if_present(self):
        """在装了 fnx 的虚拟机里,真实的启动脚本必须通过预检。"""
        checked = 0
        for name in ("fnx_dv", "fnx_sw"):
            path = os.path.expanduser("~/.forenyx/%s/bin/%s" % (name, name))
            if os.path.isfile(path):
                checked += 1
                self.assertEqual(preflight_launcher(path), [], msg=path)
        if not checked:
            self.skipTest("本机没有安装 fnx_dv / fnx_sw")


if __name__ == "__main__":
    unittest.main()
