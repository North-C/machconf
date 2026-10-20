"""Pure fixture tests plus shell-probe checks with a stubbed dmesg."""

import subprocess
import unittest

from modules.kvm_arm import (
    KVM_ARM_KEYS, KvmArmModule, argument_values, log_evidence,
    parse_grub_assignments,
)


def ok(text):
    return "MACHCONF_STATUS=ok\n" + text


def fixture(**overrides):
    sections = {
        "kvm_arm_arch": ok("aarch64"),
        "kvm_arm_cmdline": ok("root=/dev/sda quiet"),
        "kvm_arm_grub": ok('GRUB_CMDLINE_LINUX="quiet"\nGRUB_CMDLINE_LINUX_DEFAULT=""'),
        "kvm_arm_default_kernel": ok("/boot/vmlinuz-next"),
        "kvm_arm_default_entry": ok('index=0\nkernel="/boot/vmlinuz-next"\nargs="quiet"'),
        "kvm_arm_dmesg": ok(""),
    }
    sections.update(overrides)
    return sections


def by_key(sections):
    return {item.key: item for item in KvmArmModule().parse(sections)}


class ArgumentTests(unittest.TestCase):
    def test_exact_token_names_not_substrings(self):
        key = KVM_ARM_KEYS[0]
        values = argument_values("other=" + key + "=1 prefix." + key + "=1 "
                                 + key + "_extra=1 " + key + "=0")
        self.assertEqual(values[key], ["0"])
        self.assertEqual(values[KVM_ARM_KEYS[1]], [])

    def test_bare_empty_unsupported_values_and_all_occurrences(self):
        key = KVM_ARM_KEYS[0]
        values = argument_values(key + " " + key + "= " + key + "=1 " + key + "=unknown")
        self.assertEqual(values[key], ["<无等号>", "", "1", "unknown"])

    def test_duplicate_and_conflict_separate(self):
        first, second = KVM_ARM_KEYS[:2]
        items = by_key(fixture(kvm_arm_cmdline=ok(
            first + "=1 " + first + "=1 " + second + "=1 " + second + "=0")))
        self.assertEqual(items["当前 cmdline " + first].value, "duplicate: 1, 1")
        self.assertEqual(items["当前 cmdline " + second].value, "conflict: 1, 0")

    def test_absent_is_not_disabled(self):
        item = by_key(fixture())["当前 cmdline " + KVM_ARM_KEYS[0]]
        self.assertEqual(item.value, "absent (未设置)")
        self.assertIn("不等于 disabled", item.note)

    def test_malformed_quotes_is_unparseable(self):
        items = by_key(fixture(kvm_arm_cmdline=ok('quiet "unterminated')))
        self.assertEqual(items["当前 cmdline"].value, "unparseable")


class GrubTests(unittest.TestCase):
    def test_literal_and_export_and_comments(self):
        data = parse_grub_assignments(
            '#GRUB_CMDLINE_LINUX="bad"\n'
            'export GRUB_CMDLINE_LINUX="quiet kvm-arm.ipiv_enabled=1" # comment\n'
            "GRUB_CMDLINE_LINUX_DEFAULT='literal$not_expanded'\n")
        self.assertEqual(data["GRUB_CMDLINE_LINUX"]["value"], "quiet kvm-arm.ipiv_enabled=1")
        self.assertEqual(data["GRUB_CMDLINE_LINUX_DEFAULT"]["value"], "literal$not_expanded")

    def test_dynamic_and_compound_syntax_rejected(self):
        for raw in ('"$BASE quiet"', '"$(danger)"', '`danger`', '"quiet"; danger',
                    '"quiet"\\\n"more"', '${BASE}', '"quiet""more"'):
            with self.subTest(raw=raw):
                result = parse_grub_assignments("GRUB_CMDLINE_LINUX=" + raw)
                self.assertEqual(result["GRUB_CMDLINE_LINUX"]["status"], "unparseable")

    def test_duplicate_or_conditional_assignments_rejected(self):
        for text in ('GRUB_CMDLINE_LINUX="quiet"\nGRUB_CMDLINE_LINUX="again"',
                     'if true; then GRUB_CMDLINE_LINUX="quiet"; fi',
                     'GRUB_CMDLINE_LINUX+="quiet"',
                     'if test -f /file; then\nGRUB_CMDLINE_LINUX="quiet"\nfi',
                     'source /some/config\nGRUB_CMDLINE_LINUX="quiet"'):
            with self.subTest(text=text):
                result = parse_grub_assignments(text)
                self.assertEqual(result["GRUB_CMDLINE_LINUX"]["status"], "unparseable")

    def test_absent_and_empty_different(self):
        result = parse_grub_assignments('GRUB_CMDLINE_LINUX=""')
        self.assertEqual(result["GRUB_CMDLINE_LINUX"]["status"], "ok")
        self.assertEqual(result["GRUB_CMDLINE_LINUX_DEFAULT"]["status"], "absent")

    def test_default_entry_is_separate_from_current(self):
        key = KVM_ARM_KEYS[0]
        items = by_key(fixture(kvm_arm_default_entry=ok(
            'index=0\nkernel="/boot/vmlinuz-next"\nargs="' + key + '=1"')))
        self.assertEqual(items["默认启动项 " + key].value, "1")
        self.assertEqual(items["当前 cmdline " + key].value, "absent (未设置)")
        self.assertIn("不代表当前内核", items["默认启动项 kernel"].note)

    def test_multiple_default_entries_rejected(self):
        entry = 'index=0\nkernel="/boot/vmlinuz-next"\nargs="quiet"'
        items = by_key(fixture(kvm_arm_default_entry=ok(entry + "\n" + entry)))
        self.assertEqual(items["默认启动项"].value, "unparseable")

    def test_default_kernel_race_reported(self):
        items = by_key(fixture(kvm_arm_default_kernel=ok("/boot/other")))
        self.assertEqual(items["默认启动项"].value, "conflict")

    def test_grubby_unavailable_does_not_hide_other_results(self):
        items = by_key(fixture(kvm_arm_default_kernel="MACHCONF_STATUS=error\ngrubby missing"))
        self.assertEqual(items["默认启动项"].value, "unavailable")
        self.assertIn("当前 cmdline " + KVM_ARM_KEYS[0], items)


class DmesgTests(unittest.TestCase):
    def test_controller_enabled_and_kvm_disabled_are_distinct(self):
        logs = ('[0.1] GICv3: Using GICv4.1 mode\n'
                '[0.2] ITS: Enabling GICv4 support\n'
                '[1.0] kvm [1]: GICv4 support disabled\n'
                '[1.1] kvm [1]: KVM dvmbm disabled\n'
                '[1.2] kvm [1]: KVM ipiv disabled')
        records, controller = log_evidence(logs)
        self.assertEqual(records["GICv4"]["status"], "disabled")
        self.assertEqual(records["dvmbm"]["status"], "disabled")
        self.assertEqual(records["ipiv"]["status"], "disabled")
        self.assertEqual(records["virt_msi_bypass"]["status"], "not_observed")
        self.assertEqual(len(controller), 2)

    def test_conflicting_log_evidence_is_not_last_wins(self):
        records, _ = log_evidence('kvm [1]: GICv4 support disabled\n'
                                 'kvm [1]: GICv4 support enabled')
        self.assertEqual(records["GICv4"]["status"], "conflict")
        self.assertEqual(len(records["GICv4"]["evidence"]), 2)

    def test_features_in_same_line_keep_independent_states(self):
        records, _ = log_evidence('kvm: GICv4 disabled, dvmbm enabled')
        self.assertEqual(records["GICv4"]["status"], "disabled")
        self.assertEqual(records["dvmbm"]["status"], "enabled")

    def test_unknown_distinct_from_no_match(self):
        records, _ = log_evidence('kvm: virt_msi_bypass unavailable\nkvm: ipiv enabled')
        self.assertEqual(records["virt_msi_bypass"]["status"], "unknown")
        self.assertEqual(records["ipiv"]["status"], "enabled")
        self.assertEqual(records["dvmbm"]["status"], "not_observed")

    def test_multiple_log_lines_render_as_single_line_notes(self):
        logs = ('GICv3: Using GICv4.1 mode\nITS: Enabling GICv4 support\n'
                'kvm [1]: GICv4 support disabled\nkvm [1]: GICv4 support enabled')
        items = by_key(fixture(kvm_arm_dmesg=ok(logs)))
        for key in ("KVM GICv4", "GICv4 控制器日志"):
            self.assertNotIn("\n", items[key].note)
            self.assertIn("; ", items[key].note)

    def test_permission_error_is_not_disabled_or_absent(self):
        items = by_key(fixture(kvm_arm_dmesg="MACHCONF_STATUS=error\nOperation not permitted"))
        self.assertEqual(items["dmesg 读取"].value, "unavailable")
        self.assertEqual(items["KVM GICv4"].value, "unavailable")
        self.assertIn("Operation not permitted", items["dmesg 读取"].note)

    def test_successful_probe_outputs_only_relevant_lines(self):
        probe = dict(KvmArmModule().probes())["kvm_arm_dmesg"]
        stub = "dmesg() { printf '%s\\n' 'private unrelated line' 'kvm [1]: GICv4 support disabled'; };\n"
        result = subprocess.run(["bash", "-c", stub + probe], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("private unrelated", result.stdout)
        self.assertIn("MACHCONF_STATUS=ok", result.stdout)
        self.assertIn("GICv4 support disabled", result.stdout)

    def test_failed_probe_reports_error(self):
        probe = dict(KvmArmModule().probes())["kvm_arm_dmesg"]
        stub = "dmesg() { printf '%s\\n' 'denied' >&2; return 1; };\n"
        result = subprocess.run(["bash", "-c", stub + probe], text=True, capture_output=True)
        self.assertIn("MACHCONF_STATUS=error\ndenied", result.stdout)

    def test_non_arm_and_missing_arch(self):
        items = by_key(fixture(kvm_arm_arch=ok("x86_64")))
        self.assertEqual(items["ARM KVM 检测"].value, "not_applicable")
        self.assertNotIn("KVM GICv4", items)
        items = by_key(fixture(kvm_arm_arch=""))
        self.assertEqual(items["ARM KVM 检测"].value, "unavailable")

    def test_missing_probe_markers_never_pass(self):
        items = by_key(fixture(kvm_arm_cmdline="", kvm_arm_grub="", kvm_arm_dmesg=""))
        for key in ("当前 cmdline", "GRUB 源文件", "dmesg 读取"):
            self.assertEqual(items[key].value, "unavailable")


if __name__ == "__main__":
    unittest.main()
