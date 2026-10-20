import io
import json
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import Mock, patch

import machconf
from modules.base import RunnerError


class KvmArmCliTests(unittest.TestCase):
    def call(self, args):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return machconf.main(args)

    def test_invalid_requests_never_contact_target(self):
        for args in (["--dry-run"], ["--set-kvm-arm", "invalid=1"],
                     ["--set-kvm-arm", "ipiv_enabled=yes"],
                     ["--set-kvm-arm", "ipiv_enabled=1", "--set-kvm-arm", "kvm-arm.ipiv_enabled=0"],
                     ["--set-kvm-arm", "ipiv_enabled=1", "--clear-hugepages"],
                     ["--set-kvm-arm", "ipiv_enabled=1", "--list-modules"]):
            with self.subTest(args=args), patch.object(machconf, "make_runner") as runner:
                with self.assertRaises(SystemExit):
                    self.call(["--local"] + args)
                runner.assert_not_called()

    def test_remote_preview_and_local_apply_dispatch(self):
        runner = Mock()
        runner.run_script.return_value = json.dumps({"ok": True, "status": "preview"})
        with patch.object(machconf, "make_runner", return_value=runner) as factory:
            self.assertEqual(self.call(["-H", "host", "--ask-pass", "--set-kvm-arm",
                                        "ipiv_enabled=1", "--dry-run"]), 0)
            factory.assert_called_with("host", ask_pass=True, command_timeout=180)
            command = runner.run_script.call_args.args[0]
            self.assertIn('"kvm-arm.ipiv_enabled": "1"', command.splitlines()[0])
            self.assertIn("--dry-run", command.splitlines()[0])
            self.assertEqual(self.call(["--local", "--set-kvm-arm", "vgic_v4_enable=0"]), 0)
            self.assertNotIn("--dry-run", runner.run_script.call_args.args[0].splitlines()[0])

    def test_failed_result_and_connection_error_continue_hosts(self):
        runner = Mock()
        runner.run_script.side_effect = [json.dumps({"ok": False, "backup": "/boot/backup"}),
                                         RunnerError("connection lost"), "not json"]
        with patch.object(machconf, "make_runner", return_value=runner):
            self.assertEqual(self.call(["-H", "a,b,c", "--set-kvm-arm", "ipiv_enabled=1"]), 1)
        self.assertEqual(runner.run_script.call_count, 3)


if __name__ == "__main__":
    unittest.main()
