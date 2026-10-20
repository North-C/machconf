"""Offline tests for complete and safe comparison reports."""

import copy
import csv
import io
import json
import re
import unittest

from modules.base import disp_width
from modules.compare_render import _markdown_cell, render_comparison


def entry(value, key="CPU", note=""):
    return {"key": key, "value": value, "note": note}


def report():
    rows = [
        {"module": "cpu", "key": "CPU", "left": [entry("128")],
         "right": [entry("128")], "status": "equal"},
        {"module": "numa", "key": "节点", "left": [entry("4")],
         "right": [entry("2")], "status": "different"},
        {"module": "rcu", "key": "normal", "left": [entry("0")],
         "right": [], "status": "left_only"},
        {"module": "rcu", "key": "expedited", "left": [],
         "right": [entry("1")], "status": "right_only"},
        {"module": "bios", "key": "版本", "left": [entry("", note="权限不足")],
         "right": [entry("1")], "status": "unavailable"},
        {"module": "memory", "key": "容量",
         "left": [entry("1", key="a"), entry("2", key="b")],
         "right": [entry("3")], "status": "ambiguous"},
    ]
    return {"hosts": ["host-a", "host-b"], "rows": rows,
            "summary": {row["status"]: 1 for row in rows}}


class ComparisonRenderTests(unittest.TestCase):
    def test_missing_duplicate_and_empty_values_remain_distinct(self):
        result = render_comparison(report())
        for text in ("未采集", "（空值）", "[1] a: 1", "[2] b: 2",
                     "不可判定", "多项歧义", "左机器值（host-a）"):
            self.assertIn(text, result)

    def test_json_filter_retains_summary_notes_and_source(self):
        source = report()
        original = copy.deepcopy(source)
        output = json.loads(render_comparison(source, "json", diff_only=True))
        self.assertEqual(len(output["rows"]), 5)
        self.assertEqual(output["total_rows"], 6)
        self.assertEqual(output["displayed_rows"], 5)
        self.assertEqual(output["summary"], original["summary"])
        self.assertEqual(output["rows"][3]["left"][0]["note"], "权限不足")
        self.assertEqual(source, original)

    def test_filter_only_omits_equal_status(self):
        for format in ("table", "markdown", "csv"):
            result = render_comparison(report(), format, diff_only=True)
            self.assertNotIn("128", result)
            for label in ("不同", "仅左侧", "仅右侧", "不可判定", "多项歧义"):
                self.assertIn(label, result)
        self.assertIn("相同 1", render_comparison(report(), diff_only=True))
        self.assertIn("展示 5 / 6 项", render_comparison(report(), diff_only=True))

    def test_table_wraps_long_values_and_aligns_chinese(self):
        source = report()
        source["rows"] = source["rows"][:1]
        value = "长配置" * 25 + "tail"
        source["rows"][0]["left"][0]["value"] = value
        source["hosts"][0] = "very-long-host-name" * 10
        result = render_comparison(source)
        table_lines = [line for line in result.splitlines() if line.startswith("|")]
        self.assertEqual(len({disp_width(line) for line in table_lines}), 1)
        # Extract the entire left value from all physical rows after the header.
        borders = [i for i, line in enumerate(result.splitlines()) if line.startswith("+")]
        data_lines = result.splitlines()[borders[1] + 1:borders[2]]
        self.assertEqual("".join(line.split("|")[3].strip() for line in data_lines), value)
        self.assertLessEqual(max(map(disp_width, data_lines)), 138)

    def test_terminal_controls_removed_and_newlines_preserved(self):
        source = report()
        source["rows"][0]["left"][0]["value"] = (
            "\x1b[31mred\x1b[0m\nnext\x07\x08\u202e"
        )
        result = render_comparison(source)
        self.assertIn("red", result)
        self.assertIn("next", result)
        for control in ("\x1b", "\x07", "\x08", "\u202e"):
            self.assertNotIn(control, result)

    def test_markdown_cannot_inject_table_html_or_link(self):
        source = report()
        source["rows"][0]["left"][0]["value"] = (
            '<script>alert(1)</script> & a|b\n[link](javascript:x) `code`'
        )
        result = render_comparison(source, "markdown")
        self.assertIn(
            "` <script>alert(1)</script> & a `\\|` b `<br>"
            "`` [link](javascript:x) `code` ``", result,
        )
        self.assertNotIn("&#", result)
        for line in result.splitlines():
            if line.startswith("|"):
                self.assertEqual(len(re.findall(r"(?<!\\)\|", line)), 6)

    def test_markdown_configuration_source_remains_readable(self):
        for value in ('rcu_expedited', '["0-3", "4-7"]',
                      '{"cpus_allowed_list": "96-191"}', 'kvm-arm.ipiv_enabled=1'):
            self.assertEqual(_markdown_cell(value), "` " + value + " `")
        self.assertEqual(_markdown_cell("`one` ``two``"),
                         "``` `one` ``two`` ```")
        self.assertEqual(_markdown_cell(" a "), "`  a  `")
        self.assertEqual(_markdown_cell(""), "")
        self.assertEqual(_markdown_cell("a\\|b"), "` a\\ `\\|` b `")
        self.assertEqual(_markdown_cell("||"), r"\|\|")

    def test_csv_has_bom_quotes_newlines_and_rectangular_data(self):
        source = report()
        source["rows"][0]["left"][0]["value"] = 'quoted "text",\nnext'
        for format in ("csv", "csv-wide"):
            result = render_comparison(source, format)
            self.assertTrue(result.startswith("\ufeff"))
            records = list(csv.reader(io.StringIO(result[1:])))
            self.assertEqual(len(records), 7)
            self.assertTrue(all(len(record) == 5 for record in records))
            self.assertEqual(records[1][2], 'quoted "text",\nnext')

    def test_empty_filtered_table_keeps_header_and_summary(self):
        source = report()
        source["rows"] = source["rows"][:1]
        source["summary"] = {"equal": 1}
        result = render_comparison(source, diff_only=True)
        self.assertIn("模块", result)
        self.assertIn("展示 0 / 1 项", result)
        self.assertIn("相同 1", result)

    def test_unknown_format_rejected(self):
        with self.assertRaises(ValueError):
            render_comparison(report(), "html")


if __name__ == "__main__":
    unittest.main()
