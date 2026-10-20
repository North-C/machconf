"""Exercise boot changes against temporary files; never edit a host bootloader."""

from pathlib import Path
import copy
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from modules import kvm_arm_boot as boot


KEY = boot.KEYS[0]


class FakeGrubby:
    """Read fixtures like grubby and change only the selected Linux line."""

    def __init__(self, config, env, mode=None):
        self.config, self.env, self.mode = config, env, mode
        self.calls = []
        self.updated = False
        self.unreadable = False

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[0] == "checker":
            if self.mode == "syntax":
                raise boot.BootError("invalid candidate syntax")
            return ""
        options = dict(arg[2:].partition("=")[::2] for arg in argv[1:])
        config = Path(options["config-file"])
        env = Path(options["env"])
        if "update-kernel" in options:
            if "remove-args" in options and "args" in options:
                raise AssertionError("grubby 8.40 requires separate remove/add updates")
            lines = config.read_text().splitlines(keepends=True)
            remove = options.get("remove-args", "").split()
            tokens = lines[1].strip().split()
            tokens = [t for t in tokens if t.split("=", 1)[0] not in remove]
            lines[1] = "  " + " ".join(tokens + options.get("args", "").split()) + "\n"
            final_update = "args" in options
            if self.mode == "other-entry" and final_update:
                lines[4] = lines[4].replace("quiet", "verbose")
            if self.mode == "other-config-line" and final_update:
                lines[0] = lines[0].replace("primary", "changed")
            config.write_text("".join(lines))
            if self.mode == "candidate-env" and final_update:
                env.write_text("saved_entry=1\n")
            if self.mode == "equivalent-env" and final_update:
                env.write_text("saved_entry=entry-0\n")
            if self.mode == "unrelated-env" and final_update:
                env.write_text("saved_entry=entry-0\nnext_entry=1\n")
            if self.mode == "concurrent" and final_update:
                self.config.write_text(self.config.read_text() + "# external change\n")
            self.updated = True
            return ""
        if "default-kernel" in options:
            if self.mode == "quoted-kernel":
                return '"/boot/vmlinuz-current"'
            return "/boot/vmlinuz-current"
        if "default-index" in options:
            return "0"
        if "info" in options:
            if self.updated and config == self.config:
                if self.mode == "postinstall-deleted":
                    config.unlink()
                    raise FileNotFoundError("postinstall config disappeared")
                if self.mode == "postinstall-unreadable":
                    self.unreadable = True
                    raise PermissionError("postinstall config cannot be read")
            if self.mode in ("postinstall", "postinstall-concurrent") and self.updated and config == self.config:
                if self.mode == "postinstall-concurrent":
                    config.write_text(config.read_text() + "# external postinstall change\n")
                raise boot.BootError("postinstall readback failed")
            entries = []
            for index, line in enumerate(line for line in config.read_text().splitlines()
                                         if line.strip().startswith("linux ")):
                _, kernel, args = line.strip().split(" ", 2)
                kernel_value = "/boot" + kernel
                if self.mode == "quoted-kernel":
                    kernel_value = '"' + kernel_value + '"'
                entries.append('index=%s\nkernel=%s\nargs="%s"\ntitle=entry-%s'
                               % (index, kernel_value, args, index))
            return "\n".join(entries)
        raise AssertionError("unexpected call: %r" % argv)


class BootChangeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="machconf boot ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.env = self.root / "grub.cfg", self.root / "grubenv"
        self.original = ("menuentry 'primary' {\n"
                         "  linux /vmlinuz-current root=/dev/root quiet\n}\n"
                         "menuentry 'fallback' {\n"
                         "  linux /vmlinuz-old root=/dev/root quiet " + KEY + "=0\n}\n").encode()
        self.config.write_bytes(self.original)
        self.env.write_text("saved_entry=0\n")
        self.paths = ("grubby", "checker", self.config, self.env)
        root_patch = patch.object(boot.os, "geteuid", return_value=0)
        root_patch.start()
        self.addCleanup(root_patch.stop)

    def change(self, mode=None, dry_run=False):
        self.fake = FakeGrubby(self.config, self.env, mode)
        return boot.change({KEY: "1"}, dry_run=dry_run, paths=self.paths, invoke=self.fake)

    def test_preview_does_not_write_or_stage_anything(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        result = self.change(dry_run=True)
        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["changes"][KEY], {"before": [], "after": "1"})
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
        self.assertFalse(self.fake.updated)

    def test_apply_preserves_fallback_and_keeps_backup(self):
        result = self.change()
        self.assertTrue(result["ok"], result)
        backup = Path(result["backup"])
        self.assertEqual((backup / "original.cfg").read_bytes(), self.original)
        self.assertEqual((backup / "grubenv").read_bytes(), self.env.read_bytes())
        self.assertTrue((backup / "plan.json").is_file())
        lines = self.config.read_text().splitlines()
        self.assertTrue(lines[1].endswith(KEY + "=1"))
        self.assertEqual(lines[3:], self.original.decode().splitlines()[3:])

    def test_updates_remove_then_add_on_candidate_only(self):
        result = self.change()
        self.assertTrue(result["ok"], result)
        updates = [call for call in self.fake.calls if any(arg.startswith("--update-kernel=") for arg in call)]
        self.assertEqual(len(updates), 2)
        self.assertTrue(any(arg.startswith("--remove-args=") for arg in updates[0]))
        self.assertFalse(any(arg.startswith("--args=") for arg in updates[0]))
        self.assertTrue(any(arg.startswith("--args=") for arg in updates[1]))
        for call in updates:
            self.assertIn("--config-file=" + str(Path(result["backup"]) / "candidate.cfg"), call)

    def test_equivalent_grubby_saved_entry_rewrite_is_reset(self):
        result = self.change(mode="equivalent-env")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.env.read_bytes(), b"saved_entry=0\n")
        self.assertEqual((Path(result["backup"]) / "grubenv").read_bytes(), self.env.read_bytes())

    def test_quoted_kernel_output_is_accepted(self):
        result = self.change(mode="quoted-kernel")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["kernel"], "/boot/vmlinuz-current")

    def test_duplicate_target_parameters_are_replaced_with_one_value(self):
        self.config.write_text(self.original.decode().replace(
            "current root=/dev/root quiet", "current root=/dev/root quiet " + KEY + "=0 " + KEY + "=1"))
        result = self.change()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["changes"][KEY]["before"], ["0", "1"])
        self.assertEqual(self.config.read_text().splitlines()[1].count(KEY + "="), 1)

    def test_matching_value_is_noop_without_backup(self):
        self.config.write_text(self.original.decode().replace(
            "current root=/dev/root quiet", "current root=/dev/root quiet " + KEY + "=1"))
        before = self.config.read_bytes()
        result = self.change()
        self.assertTrue(result["ok"])
        self.assertNotIn("backup", result)
        self.assertFalse(self.fake.updated)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(len(list(self.root.iterdir())), 2)

    def test_unsafe_candidates_are_not_installed(self):
        for mode in ("syntax", "other-entry", "other-config-line", "candidate-env", "unrelated-env"):
            with self.subTest(mode=mode):
                result = self.change(mode=mode)
                self.assertFalse(result["ok"], result)
                self.assertEqual(self.config.read_bytes(), self.original)
                self.assertEqual(self.env.read_text(), "saved_entry=0\n")
                self.assertEqual((Path(result["backup"]) / "original.cfg").read_bytes(), self.original)

    def test_concurrent_change_is_not_overwritten(self):
        result = self.change(mode="concurrent")
        self.assertFalse(result["ok"], result)
        self.assertIn("并发", result["error"])
        self.assertEqual(self.config.read_bytes(), self.original + b"# external change\n")

    def test_postinstall_readback_failure_restores_original(self):
        result = self.change(mode="postinstall")
        self.assertFalse(result["ok"], result)
        self.assertTrue(result["rollback_verified"], result)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(self.env.read_text(), "saved_entry=0\n")

    def test_postinstall_missing_config_reports_backup_and_rollback_error(self):
        result = self.change(mode="postinstall-deleted")
        self.assertFalse(result["ok"], result)
        self.assertIn("rollback_error", result)
        self.assertEqual((Path(result["backup"]) / "original.cfg").read_bytes(), self.original)
        self.assertFalse(self.config.exists())

    def test_postinstall_unreadable_config_reports_backup_and_rollback_error(self):
        fake = FakeGrubby(self.config, self.env, "postinstall-unreadable")
        read_bytes = Path.read_bytes

        def guarded_read(path):
            if path == self.config and fake.unreadable:
                raise PermissionError("postinstall config cannot be read")
            return read_bytes(path)

        with patch.object(Path, "read_bytes", guarded_read):
            result = boot.change({KEY: "1"}, paths=self.paths, invoke=fake)
        self.assertFalse(result["ok"], result)
        self.assertIn("rollback_error", result)
        self.assertEqual((Path(result["backup"]) / "original.cfg").read_bytes(), self.original)

    def test_apply_requires_root_before_creating_backup(self):
        with patch.object(boot.os, "geteuid", return_value=1000):
            with self.assertRaisesRegex(boot.BootError, "root"):
                self.change()
        self.assertEqual(len(list(self.root.iterdir())), 2)

    def test_postinstall_concurrent_change_is_not_overwritten_by_rollback(self):
        result = self.change(mode="postinstall-concurrent")
        self.assertFalse(result["ok"], result)
        self.assertNotIn("rollback_verified", result)
        self.assertIn("并发变化", result["status"])
        self.assertTrue(self.config.read_bytes().endswith(b"# external postinstall change\n"))
        self.assertEqual((Path(result["backup"]) / "original.cfg").read_bytes(), self.original)


class BootValidationTests(unittest.TestCase):
    def test_verify_rejects_default_metadata_or_unrelated_argument_changes(self):
        before = {"kernel": "/boot/vmlinuz-current", "index": "0", "entries": [
            {"index": "0", "kernel": "/boot/vmlinuz-current", "args": '"quiet"', "title": "current"},
            {"index": "1", "kernel": "/boot/vmlinuz-old", "args": '"quiet"'}]}
        after = copy.deepcopy(before)
        after["entries"][0]["args"] = '"quiet ' + KEY + '=1"'
        boot.verify_change(before, after, {KEY: "1"})
        mutations = (
            lambda value: value.update(kernel="/boot/vmlinuz-old"),
            lambda value: value.update(index="1"),
            lambda value: value["entries"].pop(),
            lambda value: value["entries"][0].update(title="changed"),
            lambda value: value["entries"][0].update(args='"verbose ' + KEY + '=1"'),
            lambda value: value["entries"][0].update(args='"quiet ' + KEY + '=1 ' + KEY + '=0"'),
            lambda value: value["entries"][0].update(args='"quiet ' + KEY + '=0"'),
        )
        for index, mutate in enumerate(mutations):
            invalid = copy.deepcopy(after)
            mutate(invalid)
            with self.subTest(case=index), self.assertRaises(boot.BootError):
                boot.verify_change(before, invalid, {KEY: "1"})

    def test_normalize_whitelist_and_duplicate_checks(self):
        self.assertEqual(boot.normalize_settings(["vgic_v4_enable=1", "kvm-arm.ipiv_enabled=0"]),
                         {KEY: "1", "kvm-arm.ipiv_enabled": "0"})
        for values in ([], ["vgic_v4_enable=2"], ["unknown=1"], ["vgic_v4_enable"],
                       ["vgic_v4_enable=1", KEY + "=0"]):
            with self.subTest(values=values), self.assertRaises(boot.BootError):
                boot.normalize_settings(values)

    def test_parse_rejects_ambiguous_entries(self):
        for output in ("", "kernel=/boot/vmlinuz", "index=0\nindex=0", "index=0\nargs=a\nargs=b"):
            with self.subTest(output=output), self.assertRaises(boot.BootError):
                boot.parse_entries(output)

    def test_config_validation_rejects_extra_or_non_linux_changes(self):
        old = b"menuentry original {\n linux /vmlinuz quiet\n}\n"
        boot.verify_config_bytes(old, old.replace(b"quiet", b"quiet test=1"))
        for new in (old, old + b"\n", old.replace(b"original", b"changed"),
                    old.replace(b"original", b"changed").replace(b"quiet", b"quiet test=1")):
            with self.subTest(new=new), self.assertRaises(boot.BootError):
                boot.verify_config_bytes(old, new)

    def test_non_arm_discovery_is_rejected(self):
        with patch.object(boot.platform, "machine", return_value="x86_64"):
            with self.assertRaisesRegex(boot.BootError, "ARM64"):
                boot.discover()

    def test_bls_and_stub_discovery_are_rejected(self):
        config = MagicMock()
        config.__str__.return_value = "/boot/efi/EFI/openEuler/grub.cfg"
        config.name = "grub.cfg"
        config.is_file.return_value = True
        link = MagicMock()
        link.is_symlink.return_value = True
        link.resolve.return_value = config
        with patch.object(boot.platform, "machine", return_value="aarch64"), \
                patch.object(boot.shutil, "which", side_effect=lambda binary: binary), \
                patch.object(boot, "Path", return_value=link):
            for content in ("blscfg\n", "configfile /boot/grub2/grub.cfg\n", "# stub\n"):
                config.read_text.return_value = content
                with self.subTest(content=content), self.assertRaisesRegex(boot.BootError, "BLS"):
                    boot.discover()


if __name__ == "__main__":
    unittest.main()
