"""RCU read-only parsing and shell protocol tests; no host sysfs changes."""

import os
import subprocess
import tempfile
import unittest

from modules.rcu import RCU_BOOT_KEYS, RcuModule, argument_values, read_file_probe


def ok(value):
    return "MACHCONF_STATUS=ok\n" + value


def fixture(expedited="0", normal="0", **overrides):
    sections = {
        "rcu_expedited": ok(expedited),
        "rcu_normal": ok(normal),
        "rcu_normal_after_boot": ok("0"),
        "rcu_cmdline": ok("quiet"),
        "rcu_kernel_config": ok("source=/proc/config.gz\nCONFIG_TREE_RCU=y\n"
                                "# CONFIG_TINY_RCU is not set\n"
                                "# CONFIG_PREEMPT_RT is not set"),
    }
    sections.update(overrides)
    return sections


def items(sections):
    return {item.key: item for item in RcuModule().parse(sections)}


class RcuRuntimeTests(unittest.TestCase):
    def test_all_four_combinations_and_normal_precedence(self):
        for expedited, normal, mode in (
            ("0", "0", "unforced (未强制)"), ("1", "0", "expedited"),
            ("0", "1", "normal"), ("1", "1", "normal"),
        ):
            with self.subTest(expedited=expedited, normal=normal):
                item = items(fixture(expedited, normal))["RCU 运行时组合策略"]
                self.assertEqual(item.value, mode)
                self.assertIn("不证明", item.note)
        item = items(fixture())["RCU 运行时组合策略"]
        self.assertIn("不是关闭 RCU", item.note)

    def test_missing_permission_and_error_are_distinct(self):
        for status in ("missing", "permission_denied", "read_error"):
            with self.subTest(status=status):
                result = items(fixture(rcu_expedited="MACHCONF_STATUS=" + status))
                self.assertEqual(result["rcu_expedited"].value, status)
                self.assertEqual(result["RCU 运行时组合策略"].value, "unavailable")
                self.assertEqual(result["rcu_normal"].value, "0")

    def test_invalid_content_never_defaults_to_zero(self):
        for text in ("", "2", "-1", "Y", "0\n1", "0x0", "0 extra"):
            with self.subTest(text=text):
                result = items(fixture(normal=text))
                self.assertEqual(result["rcu_normal"].value, "invalid")
                self.assertEqual(result["RCU 运行时组合策略"].value, "unavailable")

    def test_absent_or_broken_probe_is_unavailable(self):
        for payload in ("", "0", "MACHCONF_STATUS=unexpected\n0"):
            result = items(fixture(rcu_normal=payload))
            self.assertEqual(result["rcu_normal"].value, "unavailable")
            self.assertEqual(result["RCU 运行时组合策略"].value, "unavailable")

    def test_after_boot_does_not_override_observed_pair(self):
        result = items(fixture(rcu_normal_after_boot=ok("1")))
        self.assertEqual(result["RCU 运行时组合策略"].value, "unforced (未强制)")

    def test_empty_sections_give_no_false_success(self):
        result = items({})
        self.assertEqual(result["RCU 运行时组合策略"].value, "unavailable")
        self.assertEqual(result["RCU 当前 cmdline"].value, "unavailable")


class RcuCmdlineTests(unittest.TestCase):
    def test_exact_names_and_missing_values(self):
        key = RCU_BOOT_KEYS[0]
        values = argument_values("prefix." + key + "=1 " + key + "_suffix=1 "
                                 + key + " " + key + "= " + key + "=2")
        self.assertEqual(values[key], ["<无等号>", "", "2"])

    def test_duplicate_and_conflict_preserved(self):
        first, second, _ = RCU_BOOT_KEYS
        result = items(fixture(rcu_cmdline=ok(
            first + "=1 " + first + "=1 " + second + "=0 " + second + "=1")))
        self.assertEqual(result["当前 cmdline " + first].value, "duplicate: 1, 1")
        self.assertEqual(result["当前 cmdline " + second].value, "conflict: 0, 1")
        self.assertEqual(result["RCU 运行时组合策略"].value, "unforced (未强制)")

    def test_absent_boot_is_not_assumed_runtime_default(self):
        result = items(fixture("1", "1"))
        for key in RCU_BOOT_KEYS:
            self.assertEqual(result["当前 cmdline " + key].value, "absent (未设置)")
        self.assertEqual(result["RCU 运行时组合策略"].value, "normal")

    def test_empty_bare_and_malformed_quotes(self):
        first, second, _ = RCU_BOOT_KEYS
        result = items(fixture(rcu_cmdline=ok(first + "= " + second)))
        self.assertEqual(result["当前 cmdline " + first].value, "<空值>")
        self.assertEqual(result["当前 cmdline " + second].value, "<无等号>")
        result = items(fixture(rcu_cmdline=ok('quiet "bad')))
        self.assertEqual(result["RCU 当前 cmdline"].value, "unparseable")


class RcuKernelConfigTests(unittest.TestCase):
    def test_explicit_disabled_is_distinct_from_missing(self):
        result = items(fixture())
        self.assertEqual(result["CONFIG_TREE_RCU"].value, "y")
        self.assertEqual(result["CONFIG_TINY_RCU"].value, "n")
        result = items(fixture(rcu_kernel_config=ok("source=/proc/config.gz")))
        self.assertEqual(result["CONFIG_TINY_RCU"].value, "unknown (未见配置项)")

    def test_duplicates_and_disk_source_are_not_overstated(self):
        result = items(fixture(rcu_kernel_config=ok(
            "source=/boot/config-test\nCONFIG_TREE_RCU=y\nCONFIG_TREE_RCU=n")))
        self.assertIn("conflict", result["CONFIG_TREE_RCU"].value)
        self.assertIn("不保证", result["RCU 内核配置来源"].note)


class RcuProbeTests(unittest.TestCase):
    def test_file_read_and_missing_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "one ' quoted")
            with open(path, "w") as stream:
                stream.write("1\n")
            completed = subprocess.run(["bash", "-c", read_file_probe(path)],
                                       capture_output=True, text=True, check=True)
            self.assertEqual(completed.stdout.strip(), ok("1"))
            missing = subprocess.run(["bash", "-c", read_file_probe(path + "-missing")],
                                     capture_output=True, text=True, check=True)
            self.assertEqual(missing.stdout.strip(), "MACHCONF_STATUS=missing")

    def test_failed_cat_is_not_success(self):
        with tempfile.NamedTemporaryFile() as stream:
            stub = "cat() { printf '%s\\n' 'denied' >&2; return 1; }\n"
            completed = subprocess.run(["bash", "-c", stub + read_file_probe(stream.name)],
                                       capture_output=True, text=True, check=True)
            self.assertEqual(completed.stdout.strip(), "MACHCONF_STATUS=read_error\ndenied")

    def test_all_probes_have_valid_shell_syntax(self):
        for tag, command in RcuModule().probes():
            with self.subTest(tag=tag):
                subprocess.run(["bash", "-n"], input=command, text=True,
                               capture_output=True, check=True)


if __name__ == "__main__":
    unittest.main()
