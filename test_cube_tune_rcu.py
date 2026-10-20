"""Offline RCU schema, sysfs and transaction regression tests."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modules import cube_tune_backend as backend
from modules.cube_tune_config import validate_config
from test_cube_tune_backend import FakeHost


def config(expedited=1, normal=0):
    return {"version": 1, "rcu": {"rcu_expedited": expedited, "rcu_normal": normal}}


class RCUHost(FakeHost):
    def __init__(self, root):
        super().__init__(root)
        self.writes = []
        self.fail_write = None
        self.ignore_write = None
        for key in backend.RCU_KEYS:
            path = self.path("/sys/kernel/" + key)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("0\n")
        boot = self.path("/proc/sys/kernel/random/boot_id")
        boot.parent.mkdir(parents=True)
        boot.write_text("00000000-0000-0000-0000-000000000001\n")

    def write_rcu(self, key, value):
        self.writes.append((key, value))
        if self.fail_write == key:
            self.fail_write = None
            raise OSError("injected RCU write failure")
        if self.ignore_write == key:
            self.ignore_write = None
            # Simulate a kernel accepting the write but retaining its old value;
            # keep the production readback code in the test.
            with patch.object(backend.os, "write", return_value=2):
                return super().write_rcu(key, value)
        return super().write_rcu(key, value)

    def set_values(self, expedited, normal):
        for key, value in config(expedited, normal)["rcu"].items():
            self.path("/sys/kernel/" + key).write_text(str(value) + "\n")


class RCUTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.host = RCUHost(self.root)
        self.state_dir = self.root / "snapshot"

    def apply(self, value=None):
        with patch.object(backend.os, "geteuid", return_value=0):
            return backend.apply(value or config(), self.state_dir, self.host)

    def snapshot(self):
        return json.loads((self.state_dir / "state.json").read_text())

    def save_snapshot(self, value):
        (self.state_dir / "state.json").write_text(json.dumps(value))

    def test_schema_accepts_only_explicit_integer_mode_pairs(self):
        for expedited, normal in ((0, 0), (1, 0), (0, 1)):
            with self.subTest(expedited=expedited, normal=normal):
                supplied = config(expedited, normal)
                result = validate_config(supplied)
                self.assertEqual(result["rcu"], supplied["rcu"])
                result["rcu"]["rcu_expedited"] = 7
                self.assertEqual(supplied["rcu"]["rcu_expedited"], expedited)
        invalid = [None, {}, {"rcu_expedited": 1}, {"rcu_normal": 0},
                   {"rcu_expedited": 1, "rcu_normal": 1},
                   {"rcu_expedited": 1, "rcu_normal": 0, "unknown": 0}]
        for value in (True, False, "1", 1.0, -1, 2, None):
            invalid.append({"rcu_expedited": value, "rcu_normal": 0})
            invalid.append({"rcu_expedited": 0, "rcu_normal": value})
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_config({"version": 1, "rcu": value})

    def test_rcu_only_plan_is_readonly_and_needs_no_service_container_or_topology(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with patch.object(self.host, "service", side_effect=AssertionError("systemd")), \
                patch.object(self.host, "container", side_effect=AssertionError("docker")), \
                patch.object(self.host, "topology", side_effect=AssertionError("topology")):
            plan = backend.make_plan(config(), self.host)
        after = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(plan["rcu"]["before"], config(0, 0)["rcu"])
        self.assertEqual(self.host.writes, [])
        self.assertEqual(self.host.calls, [])
        self.assertFalse(self.state_dir.exists())
        self.assertEqual(backend.public_plan(plan)["rcu"], plan["rcu"])

    def test_rcu_only_apply_verify_and_restore_need_no_os_commands(self):
        with patch.object(self.host, "service", side_effect=AssertionError("systemd")), \
                patch.object(self.host, "container", side_effect=AssertionError("docker")), \
                patch.object(self.host, "topology", side_effect=AssertionError("topology")):
            result = self.apply()
            self.assertTrue(result["ok"], result)
            checked = backend._execute({"action": "verify", "state_dir": str(self.state_dir)}, self.host)
            self.assertTrue(checked["ok"], checked)
            restored = backend.restore(self.state_dir, self.host)
        self.assertTrue(restored["ok"], restored)
        self.assertEqual(self.host.rcu_values(), config(0, 0)["rcu"])
        self.assertEqual(self.host.calls, [])

    def test_enable_expedited_disables_normal_first_and_restore_reverses_order(self):
        self.host.set_values(0, 1)
        result = self.apply()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.host.writes, [("rcu_normal", 0), ("rcu_expedited", 1)])
        self.host.writes.clear()
        restored = backend.restore(self.state_dir, self.host)
        self.assertTrue(restored["ok"], restored)
        self.assertEqual(self.host.writes, [("rcu_expedited", 0), ("rcu_normal", 1)])
        self.assertEqual(self.host.rcu_values(), config(0, 1)["rcu"])

    def test_enable_normal_disables_expedited_first(self):
        self.host.set_values(1, 0)
        result = self.apply(config(0, 1))
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.host.writes, [("rcu_expedited", 0), ("rcu_normal", 1)])
        self.assertEqual(self.host.rcu_values(), config(0, 1)["rcu"])

    def test_noop_pair_has_no_writes(self):
        result = self.apply(config(0, 0))
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.snapshot()["rcu_started"], [])
        self.assertTrue(backend.restore(self.state_dir, self.host)["ok"])
        self.assertEqual(self.host.writes, [])

    def test_second_write_failure_restores_first_write(self):
        self.host.set_values(0, 1)
        self.host.fail_write = "rcu_expedited"
        result = self.apply()
        self.assertFalse(result["ok"])
        self.assertTrue(result["recovery"]["ok"], result)
        self.assertEqual(self.host.rcu_values(), config(0, 1)["rcu"])
        self.assertEqual(self.host.writes, [("rcu_normal", 0), ("rcu_expedited", 1), ("rcu_normal", 1)])
        self.assertEqual(self.snapshot()["status"], "restored")

    def test_accepted_write_with_bad_readback_fails_and_restores(self):
        self.host.set_values(0, 1)
        self.host.ignore_write = "rcu_expedited"
        result = self.apply()
        self.assertFalse(result["ok"])
        self.assertIn("回读", result["error"])
        self.assertTrue(result["recovery"]["ok"], result)
        self.assertEqual(self.host.rcu_values(), config(0, 1)["rcu"])

    def test_missing_unknown_or_unwritable_sysfs_rejected_before_any_write(self):
        path = self.host.path("/sys/kernel/rcu_normal")
        for scenario in ("missing", "unknown", "unwritable"):
            with self.subTest(scenario=scenario):
                self.host.set_values(0, 0)
                path.chmod(0o644)
                if scenario == "missing":
                    path.unlink()
                elif scenario == "unknown":
                    path.write_text("2\n")
                else:
                    path.chmod(0o444)
                with self.assertRaises((OSError, ValueError)):
                    self.apply()
                self.assertEqual(self.host.writes, [])
                self.assertFalse(self.state_dir.exists())
                path.write_text("0\n") if scenario != "unwritable" else path.chmod(0o644)

    def test_unknown_configuration_key_rejected_before_mutation(self):
        supplied = config()
        supplied["rcu"]["rcu_expedtied"] = 1
        with self.assertRaises(ValueError):
            self.apply(supplied)
        self.assertEqual(self.host.writes, [])
        self.assertFalse(self.state_dir.exists())

    def test_sysfs_symlink_is_not_followed(self):
        path = self.host.path("/sys/kernel/rcu_normal")
        target = self.root / "unrelated"
        target.write_text("0\n")
        path.unlink()
        path.symlink_to(target)
        with self.assertRaises(ValueError):
            self.apply()
        self.assertEqual(target.read_text(), "0\n")
        self.assertEqual(self.host.writes, [])

    def test_plan_to_apply_rcu_drift_is_not_overwritten(self):
        real_plan = backend.make_plan
        def plan_then_change(value, host):
            result = real_plan(value, host)
            host.set_values(0, 1)
            return result
        with patch.object(backend, "make_plan", side_effect=plan_then_change):
            result = self.apply()
        self.assertFalse(result["ok"])
        self.assertEqual(self.host.rcu_values(), config(0, 1)["rcu"])
        self.assertEqual(self.host.writes, [])

    def test_restore_rejects_new_boot_before_mutation(self):
        self.assertTrue(self.apply()["ok"])
        self.host.path("/proc/sys/kernel/random/boot_id").write_text("00000000-0000-0000-0000-000000000002\n")
        self.host.writes.clear()
        with self.assertRaises(ValueError):
            backend.restore(self.state_dir, self.host)
        with self.assertRaises(ValueError):
            backend._execute({"action": "verify", "state_dir": str(self.state_dir)}, self.host)
        self.assertEqual(self.host.writes, [])
        self.assertEqual(self.host.rcu_values(), config()["rcu"])

    def test_restore_rejects_drift_of_untouched_other_rcu_mode(self):
        self.assertTrue(self.apply()["ok"])
        self.host.set_values(1, 1)
        self.host.writes.clear()
        result = backend.restore(self.state_dir, self.host)
        self.assertFalse(result["ok"])
        self.assertEqual(self.host.rcu_values(), config(1, 1)["rcu"])
        self.assertEqual(self.host.writes, [])

    def test_restore_allows_values_already_restored(self):
        self.assertTrue(self.apply()["ok"])
        self.host.set_values(0, 0)
        self.host.writes.clear()
        result = backend.restore(self.state_dir, self.host)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.host.writes, [])

    def test_restore_rejects_unexpected_pair_after_completed_apply(self):
        self.host.set_values(0, 1)
        self.assertTrue(self.apply()["ok"])
        self.host.set_values(0, 0)
        self.host.writes.clear()
        result = backend.restore(self.state_dir, self.host)
        self.assertFalse(result["ok"])
        self.assertEqual(self.host.writes, [])

    def test_clear_and_restore_observed_both_set_preserves_normal_precedence(self):
        self.host.set_values(1, 1)
        self.assertTrue(self.apply(config(0, 0))["ok"])
        self.assertEqual(self.host.writes, [("rcu_expedited", 0), ("rcu_normal", 0)])
        self.host.writes.clear()
        self.assertTrue(backend.restore(self.state_dir, self.host)["ok"])
        self.assertEqual(self.host.writes, [("rcu_normal", 1), ("rcu_expedited", 1)])

    def test_verification_reports_mismatch(self):
        supplied = validate_config(config())
        result = backend.inspect_targets(supplied, self.host)
        self.assertFalse(result["ok"])
        failed = [check for check in result["targets"][0]["checks"] if not check["ok"]]
        self.assertEqual([row["check"] for row in failed], ["rcu_expedited"])

    def test_service_failure_rolls_back_rcu_and_owned_files(self):
        supplied = config()
        supplied["services"] = [{"unit": "CubeMaster.service", "cpu_affinity": "2-3", "restart": True}]
        self.host.fail_once = ("systemctl", "restart")
        result = self.apply(supplied)
        self.assertFalse(result["ok"])
        self.assertTrue(result["recovery"]["ok"], result)
        self.assertEqual(self.host.rcu_values(), config(0, 0)["rcu"])
        self.assertFalse(backend.dropin_path(self.host, supplied["services"][0]).exists())
        self.assertEqual(self.host.runtime["CPUAffinity"], "0-7")

    def test_omitted_rcu_and_old_snapshot_never_access_rcu_interfaces(self):
        supplied = {"version": 1, "services": [{"unit": "CubeMaster.service", "cpu_affinity": "2-3"}]}
        with patch.object(self.host, "rcu_values", side_effect=AssertionError("RCU read")), \
                patch.object(self.host, "rcu_boot_id", side_effect=AssertionError("boot read")), \
                patch.object(self.host, "rcu_writable", side_effect=AssertionError("RCU preflight")), \
                patch.object(self.host, "write_rcu", side_effect=AssertionError("RCU write")):
            self.assertTrue(self.apply(supplied)["ok"])
            old = self.snapshot()
            old.pop("rcu_started")
            self.save_snapshot(old)
            self.assertNotIn("rcu", backend.load_state(self.state_dir, self.host)["plan"])
            self.assertTrue(backend.restore(self.state_dir, self.host)["ok"])

    def test_snapshot_rejects_tampered_rcu_fields(self):
        self.assertTrue(self.apply()["ok"])
        original = self.snapshot()
        mutations = [
            lambda s: s["plan"]["rcu"]["before"].update(rcu_normal=True),
            lambda s: s["plan"]["rcu"]["request"].update(rcu_normal=1),
            lambda s: s["plan"]["rcu"].update(unknown=0),
            lambda s: s.update(rcu_started=["rcu_expedited", "rcu_expedited"]),
            lambda s: s.update(rcu_started=["unknown"]),
        ]
        for mutate in mutations:
            state = copy.deepcopy(original)
            mutate(state)
            self.save_snapshot(state)
            with self.subTest(state=state), self.assertRaises(ValueError):
                backend.load_state(self.state_dir, self.host)


if __name__ == "__main__":
    unittest.main()
