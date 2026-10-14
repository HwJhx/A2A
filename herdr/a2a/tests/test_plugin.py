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
TOOL_NAME_RE = "a2a_send|项目级"


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

    # ---- 在线安装(pi 包)与软链接并存的防护 ----
    def write_settings(self, packages):
        (self.fnx / "agent" / "settings.json").write_text(json.dumps({"theme": "dark", "packages": packages}))

    def test_status_reports_an_online_package_of_this_repo(self):
        self.write_settings(["git:github.com/HwJhx/A2A@a2a-plugin-v0.1.0"])
        st = plugin.status(str(self.fnx))
        self.assertEqual(st["state"], "online_package")
        self.assertEqual(st["online_packages"], ["git:github.com/HwJhx/A2A@a2a-plugin-v0.1.0"])
        self.assertNotIn("duplicate", st)

    def test_install_refuses_when_already_installed_online(self):
        for source in ["git:github.com/HwJhx/A2A@a2a-plugin-v0.1.0", "https://github.com/hwjhx/a2a.git",
                       "git:git@github.com:HwJhx/A2A@v1", {"source": "ssh://git@github.com/HwJhx/A2A", "extensions": []},
                       "HTTPS://GitHub.com/HwJhx/A2A", "https://github.com/HwJhx/A2A#a2a-plugin-v0.1.0",
                       "git:github.com/HwJhx/A2A@feature/x", "ssh://git@github.com:22/HwJhx/A2A", "GIT:github.com/HwJhx/A2A"]:
            with self.subTest(source=source):
                self.write_settings([source])
                with self.assertRaisesRegex(PluginError, "已在线安装"):
                    plugin.install(str(self.fnx))
                self.assertFalse(os.path.lexists(self.link))

    def test_a_local_path_to_this_repo_counts_as_online_package(self):
        repo = PLUGIN_SOURCE.parents[3]
        rel = os.path.relpath(repo, self.fnx / "agent")      # pi 写进 settings 的是相对 settings.json 的路径
        for source in [str(repo), rel, str(PLUGIN_SOURCE), repo.as_uri(), PLUGIN_SOURCE.as_uri()]:
            with self.subTest(source=source):
                self.write_settings([source])
                self.assertEqual(plugin.status(str(self.fnx))["state"], "online_package")
                with self.assertRaises(PluginError):
                    plugin.install(str(self.fnx))

    def test_installed_copy_is_found_whatever_the_source_spelling(self):
        # pi 把 git 包克隆到 <agent>/git/<主机>/<路径>;来源写法再怪,只要装上了,按 package.json 的 name 就能认出
        for sub in [("git", "github.com", "HwJhx", "A2A"), ("git", "gitlab.example.com", "mirror", "a2a-fork"),
                    ("npm", "node_modules", "a2a-pi-plugin")]:
            with self.subTest(sub=sub):
                copy = self.fnx.joinpath("agent", *sub)
                copy.mkdir(parents=True)
                (copy / "package.json").write_text(json.dumps({"name": plugin.PACKAGE_NAME}))
                self.write_settings(["git:some.weird/spelling#we-cannot-parse"])
                st = plugin.status(str(self.fnx))
                self.assertEqual(st["state"], "online_package")
                self.assertIn(str(copy), st["online_packages"])
                with self.assertRaisesRegex(PluginError, "已在线安装"):
                    plugin.install(str(self.fnx))
                import shutil
                shutil.rmtree(self.fnx / "agent" / sub[0])

    def test_other_installed_packages_are_ignored(self):
        other = self.fnx / "agent" / "git" / "github.com" / "someone" / "tool"
        other.mkdir(parents=True)
        (other / "package.json").write_text(json.dumps({"name": "some-tool"}))
        nested = other / "node_modules" / "a2a-pi-plugin"                 # 别的包依赖里的同名包不算
        nested.mkdir(parents=True)
        (nested / "package.json").write_text(json.dumps({"name": plugin.PACKAGE_NAME}))
        self.assertEqual(plugin.install(str(self.fnx))["state"], "linked")

    def test_package_name_matches_the_repo_manifest(self):
        manifest = json.loads((PLUGIN_SOURCE.parents[3] / "package.json").read_text())
        self.assertEqual(manifest["name"], plugin.PACKAGE_NAME)
        self.assertEqual(manifest["pi"]["extensions"], ["./herdr/a2a/pi-extension/a2a.ts"])
        self.assertIn("pi-package", manifest["keywords"])

    # ---- 项目级安装(<项目>/<configDir>/) ----
    def make_project(self, config_dir=".forenyx"):
        (self.fnx / "libexec").mkdir(exist_ok=True)
        (self.fnx / "libexec" / "package.json").write_text(json.dumps({"piConfig": {"configDir": config_dir}}))
        project = self.fnx.parent / "proj"
        (project / config_dir).mkdir(parents=True, exist_ok=True)
        return project

    def test_config_dir_name_follows_fnx(self):
        self.assertEqual(plugin.config_dir_name(str(self.fnx)), ".pi")             # 读不到时与 pi 默认一致
        self.make_project()
        self.assertEqual(plugin.config_dir_name(str(self.fnx)), ".forenyx")

    def test_project_level_package_blocks_install(self):
        project = self.make_project()
        cases = {
            "settings": lambda: (project / ".forenyx" / "settings.json").write_text(
                json.dumps({"packages": ["git:github.com/HwJhx/A2A@a2a-plugin-v0.1.0"]})),
            "clone": lambda: (lambda d: (d.mkdir(parents=True), (d / "package.json").write_text(
                json.dumps({"name": plugin.PACKAGE_NAME}))))(project / ".forenyx" / "git" / "github.com" / "HwJhx" / "A2A"),
            "extension": lambda: ((project / ".forenyx" / "extensions").mkdir(),
                                  (project / ".forenyx" / "extensions" / "a2a.ts").symlink_to(PLUGIN_SOURCE)),
            "copied extension": lambda: ((project / ".forenyx" / "extensions").mkdir(),
                                         (project / ".forenyx" / "extensions" / "old.ts").write_text('name: "a2a_send"')),
        }
        import shutil
        for label, make in cases.items():
            with self.subTest(label):
                make()
                st = plugin.status(str(self.fnx), [str(project)])
                self.assertTrue(st["project_packages"])
                with self.assertRaisesRegex(PluginError, "项目级"):
                    plugin.install(str(self.fnx), [str(project)])
                self.assertFalse(os.path.lexists(self.link))
                self.assertEqual(plugin.install(str(self.fnx))["state"], "linked")   # 不指定项目:只管 agent 级
                st = plugin.status(str(self.fnx), [str(project)])
                self.assertIn("项目级", st["duplicate"])
                plugin.uninstall(str(self.fnx))
                shutil.rmtree(project / ".forenyx")
                (project / ".forenyx").mkdir()

    def test_project_relative_source_is_resolved_from_the_config_dir(self):
        # pi 0.79.10:getBaseDirForScope("project") = join(cwd, configDir),写 settings 与加载都按它解析。
        # "../A2A_link":按配置目录是 <项目>/A2A_link,按项目根目录则是 <项目>/../A2A_link
        project = self.make_project()
        (project / ".forenyx" / "settings.json").write_text(json.dumps({"packages": ["../A2A_link"]}))
        (project.parent / "A2A_link").symlink_to(PLUGIN_SOURCE.parents[3])      # 只有"按项目根目录"的解释才指向本仓库
        self.assertEqual(plugin.status(str(self.fnx), [str(project)])["project_packages"], [])
        (project / "A2A_link").symlink_to(PLUGIN_SOURCE.parents[3])             # pi 实际解析到的位置
        self.assertEqual(plugin.status(str(self.fnx), [str(project)])["project_packages"], ["../A2A_link"])

    def test_nested_extensions_are_found_like_pi_does(self):
        # pi 0.79.10 的发现规则:子目录的 package.json pi.extensions 入口 / index.ts / index.js;extensions/ 本身的清单
        import shutil
        registers = 'pi.registerTool({ name: "a2a_send" })\n'
        layouts = {
            "子目录 index.ts": {"a2a/index.ts": registers},
            "子目录 index.js": {"a2a/index.js": registers},
            "子目录清单": {"pkg/package.json": json.dumps({"pi": {"extensions": ["./src/main.ts"]}}),
                           "pkg/src/main.ts": registers},
            "extensions 本身的清单": {"package.json": json.dumps({"pi": {"extensions": ["./x/y.ts"]}}),
                                       "x/y.ts": registers},
        }
        project = self.make_project()
        for label, files in layouts.items():
            for ext in (self.ext, project / ".forenyx" / "extensions"):
                with self.subTest(label, level=ext.parent.name):
                    for rel, text in files.items():
                        (ext / rel).parent.mkdir(parents=True, exist_ok=True)
                        (ext / rel).write_text(text)
                    with self.assertRaisesRegex(PluginError, TOOL_NAME_RE):
                        plugin.install(str(self.fnx), [str(project)])
                    self.assertFalse(os.path.lexists(self.link))
                    shutil.rmtree(ext)
        # 指向本仓库插件目录的子目录软链接(按 index / 清单解析不到时不算;这里放一个 index.ts 软链接)
        sub = self.ext / "a2a_dir"
        sub.mkdir(parents=True)
        (sub / "index.ts").symlink_to(PLUGIN_SOURCE)
        self.assertEqual(plugin.status(str(self.fnx))["conflicts"], [str(sub / "index.ts")])

    def test_root_index_does_not_hide_other_top_level_extensions(self):
        # Codex 第六轮:extensions/index.ts 与 other.ts 并存。fnx CLI 只加载 index.ts,SDK 的规则两个都加载;取并集
        self.ext.mkdir()
        (self.ext / "index.ts").write_text("export default function () {}\n")
        (self.ext / "other.ts").write_text('pi.registerTool({ name: "a2a_send" })\n')
        with self.assertRaisesRegex(PluginError, "a2a_send"):
            plugin.install(str(self.fnx))
        self.assertEqual(plugin.status(str(self.fnx))["conflicts"], [str(self.ext / "other.ts")])

    def test_nested_files_that_pi_would_not_load_are_ignored(self):
        (self.ext / "lib").mkdir(parents=True)
        (self.ext / "lib" / "helper.ts").write_text('name: "a2a_send"')   # 没有 index / 清单:pi 不加载
        (self.ext / "notes.md").write_text('name: "a2a_send"')
        self.assertEqual(plugin.install(str(self.fnx))["state"], "linked")

    def test_status_says_project_scope_was_not_checked(self):
        self.assertIn("--project-dir", plugin.status(str(self.fnx))["project_scope"])
        project = self.make_project()
        st = plugin.status(str(self.fnx), [str(project)])
        self.assertEqual((st["project_packages"], "project_scope" in st), ([], False))

    def test_project_checks_fail_closed(self):
        project = self.make_project()
        (project / ".forenyx" / "settings.json").write_text("{bad")
        with self.assertRaisesRegex(PluginError, "无法确认"):
            plugin.install(str(self.fnx), [str(project)])
        with self.assertRaisesRegex(PluginError, "不存在"):
            plugin.install(str(self.fnx), [str(project / "missing")])
        self.assertFalse(os.path.lexists(self.link))

    def test_unrelated_packages_do_not_block(self):
        self.write_settings(["git:github.com/someone/A2A", "npm:@foo/bar", "/tmp/elsewhere", {"other": 1}])
        self.assertEqual(plugin.install(str(self.fnx))["state"], "linked")

    def test_both_installed_is_reported_as_duplicate(self):
        plugin.install(str(self.fnx))
        self.write_settings(["git:github.com/HwJhx/A2A@a2a-plugin-v0.1.0"])   # 之后又在线安装(pi 的命令挡不住)
        st = plugin.status(str(self.fnx))
        self.assertEqual(st["state"], "linked")
        self.assertIn("两份", st["duplicate"])

    def test_unreadable_settings_fail_closed(self):
        (self.fnx / "agent" / "settings.json").write_text("{not json")
        with self.assertRaisesRegex(PluginError, "无法确认"):
            plugin.install(str(self.fnx))
        self.assertFalse(os.path.lexists(self.link))
        st = plugin.status(str(self.fnx))
        self.assertIn("无法确认", st["online_packages_error"])
        self.write_settings("not a list")
        with self.assertRaises(PluginError):
            plugin.install(str(self.fnx))

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
            project = Path(d) / "proj" / ".pi" / "extensions"            # 项目级已有 a2a.ts:CLI 带 --project-dir 拒绝
            project.mkdir(parents=True)
            (project / "a2a.ts").symlink_to(PLUGIN_SOURCE)
            rc, _, err = self.a2a("install", "--agent-dir", d, "--project-dir", str(Path(d) / "proj"))
            self.assertEqual(rc, 6)
            self.assertIn("项目级", json.loads(err)["error"])
            rc, out, _ = self.a2a("status", "--agent-dir", d, "--project-dir", str(Path(d) / "proj"))
            self.assertEqual(len(json.loads(out)["project_packages"]), 1)

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
