"""路线图 313:pi 插件以软链接安装(a2a plugin install / status / uninstall)。只用临时目录,不碰真实 fnx。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from a2a import plugin
from a2a.plugin import CREATED_MARKER, PLUGIN_SOURCE, PluginError

SRC = Path(__file__).resolve().parent.parent / "src"


class PluginInstall(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.fnx = Path(temp.name) / "fnx_dv"
        (self.fnx / "agent").mkdir(parents=True)          # fnx 运行过后就有 agent/
        self.ext = self.fnx / "agent" / "extensions"
        self.link = self.ext / "a2a.ts"

    def test_install_links_to_the_repo_plugin_and_is_idempotent(self):
        self.assertEqual(plugin.status(str(self.fnx))["state"], "not_installed")
        result = plugin.install(str(self.fnx))
        self.assertEqual((result["state"], result["changed"]), ("linked", True))
        self.assertTrue(self.link.is_symlink())
        self.assertEqual(Path(os.readlink(self.link)), PLUGIN_SOURCE)
        self.assertEqual(self.link.read_text(), PLUGIN_SOURCE.read_text())   # 读到的就是仓库里的文件
        self.assertEqual(plugin.install(str(self.fnx))["changed"], False)

    def test_uninstall_removes_the_link_and_the_directory_it_created(self):
        plugin.install(str(self.fnx))
        self.assertTrue((self.fnx / "agent" / CREATED_MARKER).exists())
        result = plugin.uninstall(str(self.fnx))
        self.assertEqual((result["state"], result["changed"]), ("not_installed", True))
        self.assertFalse(self.ext.exists())
        self.assertFalse((self.fnx / "agent" / CREATED_MARKER).exists())
        self.assertEqual(plugin.uninstall(str(self.fnx))["changed"], False)   # 再执行无事可做

    def test_existing_extensions_directory_is_kept(self):
        self.ext.mkdir()
        (self.ext / "other.ts").write_text("export default function () {}\n")
        plugin.install(str(self.fnx))
        self.assertFalse((self.fnx / "agent" / CREATED_MARKER).exists())
        plugin.uninstall(str(self.fnx))
        self.assertEqual([p.name for p in self.ext.iterdir()], ["other.ts"])

    def test_an_empty_extensions_directory_we_did_not_create_is_kept(self):
        self.ext.mkdir()
        plugin.install(str(self.fnx))
        plugin.uninstall(str(self.fnx))
        self.assertTrue(self.ext.is_dir())                                    # 原本就有:即使空了也不删

    def test_created_directory_is_kept_while_someone_else_uses_it(self):
        plugin.install(str(self.fnx))
        (self.ext / "later.ts").write_text("x")
        plugin.uninstall(str(self.fnx))
        self.assertTrue(self.ext.exists())                                    # 不是空的:保留,标记也保留
        (self.ext / "later.ts").unlink()
        plugin.uninstall(str(self.fnx))
        self.assertFalse(self.ext.exists())

    def test_a_recreated_extensions_directory_is_not_taken_for_ours(self):
        # Codex 审核:旧标记还在,extensions/ 被外部删除后重建 → 卸载不能删别人新建的目录
        plugin.install(str(self.fnx))
        self.ext.rename(self.fnx / "old_ext")          # 旧目录先挪走,保证新目录的 inode 不同
        self.ext.mkdir()
        plugin.install(str(self.fnx))
        plugin.uninstall(str(self.fnx))
        self.assertTrue(self.ext.is_dir())
        self.assertFalse((self.fnx / "agent" / CREATED_MARKER).exists())   # 作废的标记被清掉

    def test_refuses_to_touch_a_file_that_is_not_our_link(self):
        self.ext.mkdir()
        other = Path(self.fnx) / "elsewhere.ts"
        other.write_text("x")
        for make, state in ((lambda: self.link.write_text("someone's plugin"), "regular_file"),
                            (lambda: self.link.symlink_to(other), "linked_elsewhere"),
                            (lambda: self.link.symlink_to(self.fnx / "missing.ts"), "linked_elsewhere")):
            with self.subTest(state=state):
                make()
                before = os.readlink(self.link) if self.link.is_symlink() else self.link.read_text()
                self.assertEqual(plugin.status(str(self.fnx))["state"], state)
                with self.assertRaisesRegex(PluginError, "不覆盖"):
                    plugin.install(str(self.fnx))
                with self.assertRaisesRegex(PluginError, "不删除"):
                    plugin.uninstall(str(self.fnx))
                after = os.readlink(self.link) if self.link.is_symlink() else self.link.read_text()
                self.assertEqual(before, after)
                self.link.unlink()

    def test_refuses_when_another_extension_registers_a2a_send(self):
        self.ext.mkdir()
        (self.ext / "old_copy.ts").write_text('pi.registerTool({\n  name: "a2a_send",\n})\n')
        self.assertEqual(plugin.status(str(self.fnx))["conflicts"], [str(self.ext / "old_copy.ts")])
        with self.assertRaisesRegex(PluginError, "也注册 a2a_send"):
            plugin.install(str(self.fnx))
        self.assertFalse(os.path.lexists(self.link))

    def test_an_extension_that_only_reads_a2a_send_is_not_a_conflict(self):
        self.ext.mkdir()
        (self.ext / "inspect.ts").write_text('pi.getAllTools().find((t) => t.name === "a2a_send");\n')
        self.assertEqual(plugin.install(str(self.fnx))["conflicts"], [])

    def test_agent_dir_must_be_a_fnx_directory(self):
        with self.assertRaisesRegex(PluginError, "不存在"):
            plugin.install(str(self.fnx.parent / "typo"))


class PluginCli(unittest.TestCase):
    def a2a(self, *argv):
        env = dict(os.environ, PYTHONPATH=str(SRC), PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.run([sys.executable, "-m", "a2a.cli", "plugin", *argv], env=env, capture_output=True,
                              text=True, timeout=30)
        return proc.returncode, proc.stdout, proc.stderr

    def test_install_status_uninstall(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "agent").mkdir()
            rc, out, _ = self.a2a("install", "--agent-dir", d)
            self.assertEqual((rc, json.loads(out)["state"]), (0, "linked"))
            rc, out, _ = self.a2a("status", "--agent-dir", d)
            self.assertEqual((rc, json.loads(out)["state"]), (0, "linked"))
            rc, out, _ = self.a2a("uninstall", "--agent-dir", d)
            self.assertEqual((rc, json.loads(out)["state"]), (0, "not_installed"))
            rc, _, err = self.a2a("install", "--agent-dir", str(Path(d) / "typo"))
            self.assertEqual(rc, 6)
            self.assertIn("error", json.loads(err))

    @unittest.skipIf(os.geteuid() == 0, "root 不受目录权限限制")
    def test_os_errors_are_reported_as_json_with_exit_code_6(self):
        # Codex 审核:权限不足、只读目录等 OSError 也按约定输出 JSON、退出码 6
        with tempfile.TemporaryDirectory() as d:
            agent = Path(d) / "agent"
            agent.mkdir()
            agent.chmod(0o500)                           # 不能在里面建 extensions/
            try:
                rc, _, err = self.a2a("install", "--agent-dir", d)
            finally:
                agent.chmod(0o700)
            self.assertEqual(rc, 6, err)
            self.assertIn("PermissionError", json.loads(err)["error"])


if __name__ == "__main__":
    unittest.main()
