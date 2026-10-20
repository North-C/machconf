"""Render a two-machine comparison without losing missing or duplicate entries."""

import csv
import io
import json
import re
import unicodedata

from .base import disp_width, pad


STATUS_LABELS = {
    "equal": "相同",
    "different": "不同",
    "left_only": "仅左侧",
    "right_only": "仅右侧",
    "unavailable": "不可判定",
    "ambiguous": "多项歧义",
}
_NOTE = "按配置值比较；说明（note）保留在 JSON 输出中。"
_ANSI = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|"
    r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]"
)


def _safe_text(value):
    """Remove terminal escapes and invisible direction/control characters."""
    text = "" if value is None else str(value)
    text = _ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\t", "    ")
    return "".join(
        char for char in text
        if char == "\n" or not unicodedata.category(char).startswith("C")
    )


def _entries_text(entries):
    if not entries:
        return "未采集"
    if len(entries) == 1:
        return _safe_text(entries[0].get("value")) or "（空值）"
    return "\n".join(
        "[%d] %s: %s" % (
            index, _safe_text(entry.get("key", "")),
            _safe_text(entry.get("value")) or "（空值）",
        )
        for index, entry in enumerate(entries, 1)
    )


def _cells(row):
    return [
        _safe_text(row["module"]), _safe_text(row["key"]),
        _entries_text(row["left"]), _entries_text(row["right"]),
        STATUS_LABELS[row["status"]],
    ]


def _wrap(text, width):
    result = []
    for line in text.split("\n"):
        chunk = ""
        for char in line:
            if chunk and disp_width(chunk + char) > width:
                result.append(chunk)
                chunk = ""
            chunk += char
        result.append(chunk)
    return result


def _table(headers, rows):
    records = [headers] + rows
    widths = [
        min(limit, max(disp_width(line) for record in records
                       for line in record[index].split("\n")))
        for index, limit in enumerate((18, 30, 32, 32, 10))
    ]
    border = "+-" + "-+-".join("-" * width for width in widths) + "-+"
    output = [border]
    for index, record in enumerate(records):
        wrapped = [_wrap(cell, width) for cell, width in zip(record, widths)]
        for line_index in range(max(map(len, wrapped))):
            output.append("| " + " | ".join(
                pad(lines[line_index] if line_index < len(lines) else "", width)
                for lines, width in zip(wrapped, widths)
            ) + " |")
        if index == 0:
            output.append(border)
    if rows:
        output.append(border)
    return "\n".join(output)


def _markdown_literal(text):
    # Code spans keep JSON, underscores and HTML-like values readable and literal.
    # Choose a delimiter longer than any input run so values cannot close it.
    if not text:
        return ""
    delimiter = "`" * (1 + max(
        (len(run) for run in re.findall(r"`+", text)), default=0))
    if not text.isspace():
        text = " " + text + " "
    return delimiter + text + delimiter


def _markdown_cell(value):
    # GFM parses pipes even within code. Keep escaped pipes outside code spans
    # so a preceding literal backslash cannot cancel their table escaping.
    return "<br>".join(
        r"\|".join(_markdown_literal(part) for part in line.split("|"))
        for line in value.split("\n")
    )


def _summary(report, displayed):
    counts = report.get("summary", {})
    return "汇总：%s；展示 %d / %d 项。" % (
        "，".join("%s %d" % (label, counts.get(status, 0))
                  for status, label in STATUS_LABELS.items()),
        displayed, len(report["rows"]),
    )


def render_comparison(report, format="table", diff_only=False):
    """Render a report; diff_only hides equal rows, preserving the full summary.

    JSON retains all original entries and notes, and adds ``displayed_rows`` and
    ``total_rows``. CSV (including csv-wide) is a BOM-prefixed rectangular table.
    Text and Markdown values are terminal-safe; long table cells wrap in full.
    """
    rows = [row for row in report["rows"]
            if not diff_only or row["status"] != "equal"]
    if format == "json":
        output = dict(report)
        output.update(rows=rows, displayed_rows=len(rows),
                      total_rows=len(report["rows"]))
        return json.dumps(output, ensure_ascii=False, indent=2)
    headers = [
        "模块", "配置项", "左机器值（%s）" % _safe_text(report["hosts"][0]),
        "右机器值（%s）" % _safe_text(report["hosts"][1]), "结果",
    ]
    records = [_cells(row) for row in rows]
    if format in ("csv", "csv-wide"):
        stream = io.StringIO(newline="")
        stream.write("\ufeff")
        writer = csv.writer(stream)
        writer.writerow(headers)
        writer.writerows(records)
        return stream.getvalue()
    if format == "table":
        body = _table(headers, records)
    elif format == "markdown":
        lines = ["| " + " | ".join(map(_markdown_cell, headers)) + " |",
                 "| " + " | ".join(["---"] * len(headers)) + " |"]
        lines.extend("| " + " | ".join(map(_markdown_cell, record)) + " |"
                     for record in records)
        body = "\n".join(lines)
    else:
        raise ValueError("不支持的对比输出格式: %s" % format)
    return "%s\n\n%s\n%s" % (body, _summary(report, len(rows)), _NOTE)
