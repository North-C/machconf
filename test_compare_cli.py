"""Two-host comparison CLI tests; all collection is offline or mocked."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import machconf
from modules import MODULES, SshError
from modules.base import Item


class CompareCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.left = self.root / "left.json"
        self.right = self.root / "right.json"
        for path, host, value in ((self.left, "machine-a", "0"), (self.right, "machine-b", "1")):
            path.write_text(json.dumps({host: {"rcu": [
                {"key": "rcu_expedited", "value": value, "note": "runtime"},
                {"key": "rcu_normal", "value": "0", "note": ""}]}}))

    def invoke(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = machconf.main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def files_args(self):
        return ["--compare-files", str(self.left), str(self.right)]

    def test_offline_json_never_collects_and_retains_difference(self):
        with patch.object(machconf, "collect", side_effect=AssertionError("network")):
            code, text, error = self.invoke(self.files_args() + ["-f", "json"])
        self.assertEqual(code, 0, error)
        data = json.loads(text)
        self.assertEqual(data["hosts"], ["machine-a", "machine-b"])
        rows = {row["key"]: row for row in data["rows"]}
        self.assertEqual(rows["rcu_expedited"]["status"], "different")
        self.assertEqual(rows["rcu_normal"]["status"], "equal")

    def test_diff_only_and_module_selection(self):
        code, text, error = self.invoke(self.files_args() + ["-m", "rcu", "--diff-only", "-f", "json"])
        self.assertEqual(code, 0, error)
        data = json.loads(text)
        self.assertEqual([row["key"] for row in data["rows"]], ["rcu_expedited"])
        self.assertEqual(data["summary"]["equal"], 1)
        self.assertEqual(self.invoke(self.files_args() + ["-m", "numa"])[0], 1)

    def test_offline_markdown_and_csv(self):
        for output_format in ("table", "markdown", "csv", "csv-wide"):
            with self.subTest(format=output_format):
                code, text, error = self.invoke(self.files_args() + ["-f", output_format])
                self.assertEqual(code, 0, error)
                self.assertIn("machine-a", text)
                self.assertIn("machine-b", text)
                self.assertIn("rcu_expedited", text)
                self.assertIn("不同", text)

    def test_invalid_report_or_missing_path_is_failure_without_network(self):
        for text in ('{"host":', '{"a": {}, "b": {}}', '{"host":{"rcu":[{"key":"x","value":0}]}}'):
            self.left.write_text(text)
            with patch.object(machconf, "collect", side_effect=AssertionError("network")):
                code, output, error = self.invoke(self.files_args())
            self.assertEqual(code, 1)
            self.assertEqual(output, "")
            self.assertIn("对比失败", error)
        self.left.unlink()
        self.assertEqual(self.invoke(self.files_args())[0], 1)

    def test_online_comparison_collects_two_hosts_using_requested_modules(self):
        results = [
            [(MODULES["rcu"], [Item("rcu_expedited", "0")])],
            [(MODULES["rcu"], [Item("rcu_expedited", "1")])],
        ]
        with patch.object(machconf, "collect", side_effect=results) as collect:
            code, text, error = self.invoke(["--compare", "-H", "host-a,host-b", "-m", "rcu", "--ask-pass", "-f", "json"])
        self.assertEqual(code, 0, error)
        self.assertEqual([call.args[0] for call in collect.call_args_list], ["host-a", "host-b"])
        self.assertTrue(all(call.args[1] == {"rcu"} and call.kwargs["ask_pass"] for call in collect.call_args_list))
        self.assertEqual(json.loads(text)["rows"][0]["status"], "different")

    def test_failed_host_never_produces_a_successful_comparison(self):
        for results in (
            [SshError("offline"), [(MODULES["rcu"], [Item("rcu_normal", "0")])]],
            [[(MODULES["rcu"], [Item("rcu_normal", "0")])], SshError("offline")],
            [SshError("offline"), SshError("offline")],
        ):
            with patch.object(machconf, "collect", side_effect=results):
                code, output, error = self.invoke(["--compare", "-H", "host-a,host-b", "-m", "rcu"])
            self.assertEqual(code, 1)
            self.assertEqual(output, "")
            self.assertIn("offline", error)

    def test_comparison_rejects_mutations_or_ambiguous_target_modes_before_tools(self):
        cases = [
            ["--compare", "-H", "a"],
            ["--compare", "-H", "a,a"],
            ["--compare", "-H", "a,b,c"],
            ["--compare", "--local"],
            ["--diff-only", "-H", "a"],
            ["--compare", "-H", "a,b", "--set-numa", "0"],
            ["--compare", "-H", "a,b", "--set-thp", "never"],
            ["--compare", "-H", "a,b", "--clear-hugepages"],
            ["--compare", "-H", "a,b", "--set-kvm-arm", "ipiv_enabled=1"],
            ["--compare", "-H", "a,b", "--list-modules"],
            self.files_args() + ["-H", "a,b"],
            self.files_args() + ["--local"],
            self.files_args() + ["--ask-pass"],
            self.files_args() + ["--cube-root", "/opt/cube"],
            self.files_args() + ["--compare"],
        ]
        for args in cases:
            with self.subTest(args=args), patch.object(machconf, "collect") as collect, \
                    patch.object(machconf, "make_runner") as runner:
                with self.assertRaises(SystemExit) as error:
                    self.invoke(args)
                self.assertEqual(error.exception.code, 2)
                collect.assert_not_called()
                runner.assert_not_called()

    def test_real_fixture_reports_can_be_compared_offline(self):
        fixture_root = Path(__file__).parent / "tests" / "fixtures"
        for path, host, fixture in (
            (self.left, "kunpeng", "huawei-s920x20-kunpeng920"),
            (self.right, "x86", "x86-synthetic"),
        ):
            sections = {source.stem: source.read_text() for source in (fixture_root / fixture).glob("*.txt")}
            results = [(MODULES[name], MODULES[name].parse(sections)) for name in ("bios", "os", "numa", "hardware")]
            path.write_text(machconf.render_json([(host, results)]))
        code, text, error = self.invoke(self.files_args() + ["-f", "json"])
        self.assertEqual(code, 0, error)
        data = json.loads(text)
        self.assertGreater(data["summary"]["different"], 0)
        self.assertTrue({"bios", "os", "numa", "hardware"} <= {row["module"] for row in data["rows"]})


if __name__ == "__main__":
    unittest.main()
