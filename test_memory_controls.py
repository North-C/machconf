"""在临时目录模拟 sysfs，执行真实 bash 脚本；不修改主机内核设置。"""

import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch

import machconf
from modules.base import LocalRunner, RunnerError
from modules.memory_controls import (
    clear_hugepages_command, parse_change_output, set_thp_command,
)


class MemoryScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="machconf memory ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        # sysfs enabled 回读为模式列表；普通文件只保存写入值，用 cat 模拟其接口。
        cat = self.bin / "cat"
        cat.write_text("#!%s\n" % sys.executable + '''
import pathlib, sys
try:
    path = pathlib.Path(sys.argv[1])
    override = pathlib.Path(str(path) + ".readback")
    value = (override if override.exists() else path).read_text().strip()
    if path.name == "enabled" and value in ("always", "madvise", "never", "inherit"):
        value = " ".join("[" + m + "]" if m == value else m
                         for m in ("always", "madvise", "never", "inherit"))
    print(value)
except OSError:
    sys.exit(1)
''')
        cat.chmod(0o755)

    def run_script(self, command):
        env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"])
        with patch.dict(os.environ, env):
            return parse_change_output(LocalRunner().run_command(command))

    def pool(self, size, nr=16, free=0, resv=0, surplus=0):
        directory = self.root / "hugepages" / ("hugepages-%skB" % size)
        directory.mkdir(parents=True)
        for key, value in zip(("nr_hugepages", "free_hugepages", "resv_hugepages", "surplus_hugepages"),
                              (nr, free, resv, surplus)):
            (directory / key).write_text(str(value))
        return directory

    def thp(self, relative="enabled", value="madvise"):
        path = self.root / "thp" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def test_clear_all_sizes_and_repeat(self):
        pools = [self.pool(2048), self.pool(1048576)]
        for _ in range(2):
            ok, lines = self.run_script(clear_hugepages_command(self.root / "hugepages"))
            self.assertTrue(ok, lines)
            self.assertEqual(len(lines), 2)
            for pool in pools:
                self.assertEqual((pool / "nr_hugepages").read_text().strip(), "0")

    def test_clear_reports_in_use_or_reserved_pages(self):
        for field in ("surplus_hugepages", "resv_hugepages", "free_hugepages"):
            with self.subTest(field=field):
                pool = self.pool(field)
                (pool / field).write_text("3")
                ok, lines = self.run_script(clear_hugepages_command(self.root / "hugepages"))
                self.assertFalse(ok)
                self.assertIn(field + "=3", "\n".join(lines))
                self.assertEqual((pool / "nr_hugepages").read_text().strip(), "0")

    def test_clear_readback_nonzero(self):
        pool = self.pool(2048)
        (pool / "nr_hugepages.readback").write_text("8")
        ok, lines = self.run_script(clear_hugepages_command(self.root / "hugepages"))
        self.assertFalse(ok)
        self.assertIn("nr_hugepages=8", "\n".join(lines))

    def test_clear_preflight_blocks_partial_writes(self):
        first = self.pool(2048)
        second = self.pool(1048576)
        (second / "resv_hugepages").write_text("invalid")
        ok, _ = self.run_script(clear_hugepages_command(self.root / "hugepages"))
        self.assertFalse(ok)
        self.assertEqual((first / "nr_hugepages").read_text(), "16")

    def test_missing_interfaces_fail(self):
        for command in (clear_hugepages_command(self.root / "absent"),
                        set_thp_command("never", self.root / "absent")):
            ok, lines = self.run_script(command)
            self.assertFalse(ok)
            self.assertIn("ERROR", "\n".join(lines))

    def test_thp_legacy_top_level_and_idempotence(self):
        path = self.thp()
        for _ in range(2):
            ok, lines = self.run_script(set_thp_command("never", self.root / "thp"))
            self.assertTrue(ok, lines)
            self.assertEqual(path.read_text().strip(), "never")

    def test_thp_all_sizes_disable_and_reenable(self):
        paths = [self.thp(), self.thp("hugepages-2048kB/enabled", "inherit"),
                 self.thp("hugepages-64kB/enabled", "never")]
        (self.root / "thp" / "hugepages-32kB").mkdir()  # 没有 enabled 的尺寸跳过
        for value in ("never", "always", "madvise"):
            ok, lines = self.run_script(set_thp_command(value, self.root / "thp"))
            self.assertTrue(ok, lines)
            self.assertEqual(len(lines), 3)
            self.assertTrue(all(path.read_text().strip() == value for path in paths))

    def test_thp_unsupported_mode_preflight(self):
        top = self.thp()
        self.thp("hugepages-64kB/enabled", "[never] inherit")
        ok, _ = self.run_script(set_thp_command("always", self.root / "thp"))
        self.assertFalse(ok)
        self.assertEqual(top.read_text(), "madvise")

    def test_thp_readback_mismatch_and_continue(self):
        top = self.thp()
        Path(str(top) + ".readback").write_text("madvise")
        leaf = self.thp("hugepages-2048kB/enabled", "always")
        ok, lines = self.run_script(set_thp_command("never", self.root / "thp"))
        self.assertFalse(ok)
        self.assertIn("回读与期望不符", "\n".join(lines))
        self.assertEqual(leaf.read_text().strip(), "never")

    def test_write_failure_is_reported(self):
        # 模拟内核在预检后拒绝写入；真实 bash 仍执行错误处理及回读路径。
        self.thp()
        self.pool(2048)
        reject_write = r'''
printf() {
    case "$1" in '%s\n'|'0\n') return 1 ;; esac
    builtin printf "$@"
}
'''
        for command in (set_thp_command("never", self.root / "thp"),
                        clear_hugepages_command(self.root / "hugepages")):
            ok, lines = self.run_script(reject_write + command)
            self.assertFalse(ok)
            self.assertIn("写入失败", "\n".join(lines))

    def test_reject_untrusted_mode(self):
        with self.assertRaises(ValueError):
            set_thp_command("never; touch unexpected")


class MemoryCliTests(unittest.TestCase):
    def call(self, args):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return machconf.main(args)

    def test_both_actions_multihost_and_partial_failure(self):
        runner = Mock()
        runner.run_command.side_effect = ["done\n@@MC_STATUS:0@@\n",
                                          "ERROR busy\n@@MC_STATUS:1@@\n",
                                          "done\n@@MC_STATUS:0@@\n",
                                          "done\n@@MC_STATUS:0@@\n"]
        with patch.object(machconf, "make_runner", return_value=runner) as factory:
            rc = self.call(["-H", "host-a,host-b", "--ask-pass",
                            "--clear-hugepages", "--set-thp", "never"])
        self.assertEqual(rc, 1)
        self.assertEqual(runner.run_command.call_count, 4)
        self.assertEqual(factory.call_args_list[0].kwargs, {"ask_pass": True})

    def test_local_success(self):
        runner = Mock()
        runner.run_command.return_value = "done\n@@MC_STATUS:0@@\n"
        with patch.object(machconf, "make_runner", return_value=runner) as factory:
            self.assertEqual(self.call(["--local", "--clear-hugepages"]), 0)
        factory.assert_called_once_with("local", ask_pass=False)

    def test_remote_failure_continues_other_hosts(self):
        runner = Mock()
        runner.run_command.side_effect = [RunnerError("SSH failed"), "@@MC_STATUS:0@@\n"]
        with patch.object(machconf, "make_runner", return_value=runner):
            self.assertEqual(self.call(["-H", "a,b", "--set-thp", "never"]), 1)
        self.assertEqual(runner.run_command.call_count, 2)

    def test_missing_completion_marker_fails(self):
        for output in ("", "partial output", "@@MC_STATUS:0@@\ntruncated"):
            self.assertFalse(parse_change_output(output)[0])

    def test_invalid_combinations_do_not_execute(self):
        cases = [["--local", "--set-thp", "invalid"],
                 ["--local", "--set-numa", "0", "--clear-hugepages"],
                 ["--local", "--set-numa", "0", "--set-thp", "never"],
                 ["-H", ",", "--clear-hugepages"]]
        with patch.object(machconf, "make_runner") as factory:
            for args in cases:
                with self.subTest(args=args), self.assertRaises(SystemExit):
                    self.call(args)
            factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
