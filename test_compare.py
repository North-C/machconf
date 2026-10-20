"""Snapshot comparisons must distinguish differences from unavailable evidence."""

import json
import tempfile
import unittest
from pathlib import Path

from modules.compare import ReportError, STATUSES, compare_reports, load_report


def item(key, value, note=""):
    return {"key": key, "value": value, "note": note}


def compare(left, right, module="os"):
    return compare_reports("left", {module: left}, "right", {module: right})


class LoadReportTests(unittest.TestCase):
    def load(self, text, encoding="utf-8"):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(text, encoding=encoding)
            return load_report(path)

    def test_existing_shape_and_optional_notes_bom(self):
        data = {"机器一": {"os": [{"key": "内核", "value": "6.1"}]}}
        host, modules = self.load(json.dumps(data), "utf-8-sig")
        self.assertEqual(host, "机器一")
        self.assertEqual(modules, data[host])

    def test_duplicate_json_members_rejected_at_each_level(self):
        for text in ('{"host":{},"host":{}}',
                     '{"host":{"os":[],"os":[]}}',
                     '{"host":{"os":[{"key":"a","key":"b","value":"1"}]}}'):
            with self.subTest(text=text), self.assertRaisesRegex(ReportError, "duplicate"):
                self.load(text)

    def test_bad_shapes_and_field_types(self):
        bad = [[], {}, {"a": {}, "b": {}}, {"": {}}, {"a": []},
               {"a": {"os": {}}}, {"a": {"os": ["text"]}},
               {"a": {"os": [{"key": "x"}]}},
               {"a": {"os": [{"key": "x", "value": 1}]}},
               {"a": {"os": [{"key": "x", "value": "1", "note": None}]}},
               {"a": {"os": [{"key": "x", "value": "1", "extra": "v"}]}},
               {"a": {"os": [{"key": "", "value": "1"}]}}]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(ReportError):
                self.load(json.dumps(data))

    def test_malformed_and_nonstandard_json(self):
        for text in ('{"a":', '{"a":{"os":NaN}}', '{"a":{"os":Infinity}}'):
            with self.subTest(text=text), self.assertRaises(ReportError):
                self.load(text)

    def test_invalid_utf8_and_missing_file_are_report_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            with self.assertRaises(ReportError):
                load_report(path)
            path.write_bytes(b"\xff")
            with self.assertRaises(ReportError):
                load_report(path)


class CompareTests(unittest.TestCase):
    def test_heterogeneous_modules_and_key_scope(self):
        left = {"hardware": [item("model", "ARM")], "os": [item("model", "6.1")]}
        right = {"os": [item("model", "6.1")], "kvm-arm": [item("enabled", "1")]}
        result = compare_reports("arm", left, "x86", right)
        self.assertEqual(result["hosts"], ["arm", "x86"])
        self.assertEqual([row["status"] for row in result["rows"]],
                         ["left_only", "equal", "right_only"])
        self.assertEqual(result["summary"], dict(equal=1, different=0, left_only=1,
                                               right_only=1, unavailable=0, ambiguous=0))

    def test_hardware_bios_os_first_independent_of_snapshot_order(self):
        names = ["z-custom", "cube-runtime", "rcu", "numa", "bios",
                 "os", "cube", "kvm-arm", "hardware", "a-custom"]
        left = {name: [item("z", "1"), item("a", "2")] for name in names}
        result = compare_reports("left", left, "right", {})
        self.assertEqual(
            list(dict.fromkeys(row["module"] for row in result["rows"])),
            ["hardware", "bios", "os", "numa", "rcu", "kvm-arm",
             "cube", "cube-runtime", "a-custom", "z-custom"],
        )
        self.assertEqual([row["key"] for row in result["rows"][:2]], ["a", "z"])

    def test_notes_preserved_but_do_not_change_value_verdict(self):
        a, b = item("CPU", "32", "first context"), item("CPU", "32", "other context")
        row = compare([a], [b])["rows"][0]
        self.assertEqual(row["status"], "equal")
        self.assertEqual(row["left"], [a])
        self.assertEqual(row["right"], [b])

    def test_missing_is_not_empty(self):
        rows = compare([item("empty", ""), item("unknown", "N.A.")], [])["rows"]
        self.assertEqual([row["status"] for row in rows], ["left_only", "left_only"])
        self.assertEqual(rows[0]["right"], [])

    def test_duplicates_preserved_even_identical_and_one_sided(self):
        repeated = [item("memory", "32"), item("memory", "32")]
        for right in ([], [item("memory", "32")], repeated):
            with self.subTest(right=right):
                row = compare(repeated, right)["rows"][0]
                self.assertEqual(row["status"], "ambiguous")
                self.assertEqual(row["left"], repeated)
                self.assertEqual(row["right"], right)

    def test_unknown_values_never_equal(self):
        for value in ("N.A.", "unavailable", "missing", "permission_denied",
                      "invalid", "unknown", "partial", "raced", "not_observed",
                      "read_error", "unparseable", "未观测到", "<redacted>",
                      '["GODEBUG=<redacted>"]', "unavailable (timeout)"):
            with self.subTest(value=value):
                result = compare([item("x", value)], [item("x", value)])
                self.assertEqual(result["rows"][0]["status"], "unavailable")
                self.assertEqual(compare([item("x", value)], [item("x", "ok")])
                                 ["rows"][0]["status"], "unavailable")

    def test_paths_and_known_unset_are_observations(self):
        for value in ("/etc/unknown.conf", "", "(未设置)", "not-set", "disabled"):
            with self.subTest(value=value):
                self.assertEqual(compare([item("x", value)], [item("x", value)])
                                 ["rows"][0]["status"], "equal")

    def test_pid_alignment_is_limited_to_runtime_and_retains_original_keys(self):
        a, b = item("CubeMaster pid=123 env.GOGC", "100"), item("CubeMaster pid=456 env.GOGC", "200")
        row = compare([a], [b], "cube-runtime")["rows"][0]
        self.assertEqual(row["key"], "CubeMaster pid=* env.GOGC")
        self.assertEqual(row["status"], "different")
        self.assertEqual(row["left"][0]["key"], a["key"])
        self.assertEqual(len(compare([a], [b], "custom")["rows"]), 2)

    def test_multiple_processes_are_ambiguous_not_last_wins(self):
        left = [item("Cubelet pid=1 CPU", "0-3"), item("Cubelet pid=2 CPU", "4-7")]
        right = [item("Cubelet pid=3 CPU", "4-7")]
        row = compare(left, right, "cube-runtime")["rows"][0]
        self.assertEqual(row["status"], "ambiguous")
        self.assertEqual(len(row["left"]), 2)

    def test_runtime_time_and_healthy_scan_excluded_but_scan_errors_retained(self):
        healthy = json.dumps({"status": "ok", "observed_count": 123})
        unhealthy = json.dumps({"status": "partial", "unreadable_count": 2})
        left = [item("采集时间", "yesterday"), item("进程扫描", healthy)]
        right = [item("采集时间", "today"), item("进程扫描", unhealthy)]
        result = compare(left, right, "cube-runtime")
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["key"], "进程扫描")
        self.assertEqual(result["rows"][0]["status"], "right_only")
        self.assertEqual(compare(right, right, "cube-runtime")["rows"][0]["status"], "unavailable")

    def test_json_key_order_semantic_but_thread_counts_different(self):
        a = '{"status":"ok","observed_count":2,"affinity_groups":[{"cpus":"0-3","count":2}]}'
        b = '{ "affinity_groups": [{"count": 2,"cpus":"0-3"}], "observed_count":2,"status":"ok"}'
        self.assertEqual(compare([item("threads", a)], [item("threads", b)])
                         ["rows"][0]["status"], "equal")
        b = b.replace('"observed_count":2', '"observed_count":3')
        self.assertEqual(compare([item("threads", a)], [item("threads", b)])
                         ["rows"][0]["status"], "different")

    def test_nested_unavailable_or_errors_are_incomparable(self):
        for value in ('{"cpu":{"status":"permission_denied"}}',
                      '{"cpu.max":"N.A."}', '{"status":"ok","errors":["lost thread"]}',
                      '{"cpu":1,"cpu":2}', '{"status":"ok","unreadable_count":1}'):
            with self.subTest(value=value):
                self.assertEqual(compare([item("x", value)], [item("x", value)])
                                 ["rows"][0]["status"], "unavailable")

    def test_json_types_array_order_and_units_not_coerced(self):
        for left, right in (('{"x":true}', '{"x":1}'), ('[1,2]', '[2,1]'),
                            ('1024 MB', '1 GB'), ('32', '032')):
            with self.subTest(left=left):
                self.assertEqual(compare([item("x", left)], [item("x", right)])
                                 ["rows"][0]["status"], "different")

    def test_empty_and_same_host_names_supported(self):
        result = compare_reports("local", {}, "local", {})
        self.assertEqual(result["rows"], [])
        self.assertEqual(set(result["summary"]), set(STATUSES))
        self.assertEqual(sum(result["summary"].values()), 0)

    def test_direct_api_validation_and_no_input_mutation(self):
        with self.assertRaises(ReportError):
            compare_reports("left", {"os": [item("x", 1)]}, "right", {})
        source = {"cube-runtime": [{"key": "Cubelet pid=1 x", "value": "1"}]}
        before = json.dumps(source)
        result = compare_reports("left", source, "right", source)
        result["rows"][0]["left"][0]["value"] = "edited"
        self.assertEqual(json.dumps(source), before)


if __name__ == "__main__":
    unittest.main()
