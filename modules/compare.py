"""Pure comparison of two machconf JSON snapshots; no probes or mutations."""

import json
import re
from collections import defaultdict
from pathlib import Path

from . import MODULE_ORDER


STATUSES = ("equal", "different", "left_only", "right_only", "unavailable", "ambiguous")
_UNKNOWN = frozenset((
    "n.a.", "n/a", "unavailable", "missing", "permission_denied", "invalid",
    "unknown", "partial", "raced", "not_observed", "read_error", "unparseable",
    "not_applicable", "not-found", "error", "未观测到", "不可用", "未知",
))
_PID = re.compile(r"(?<![\w])pid=\d+(?!\d)")


class ReportError(ValueError):
    """An input is not one unambiguous machconf JSON snapshot."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReportError("duplicate JSON key: %s" % key)
        result[key] = value
    return result


def _invalid_constant(value):
    raise ReportError("invalid JSON constant: %s" % value)


def _decode(text):
    return json.loads(text, object_pairs_hook=_unique_object,
                      parse_constant=_invalid_constant)


def _validate_modules(modules):
    if not isinstance(modules, dict):
        raise ReportError("host content must be a module object")
    normalized = {}
    for module, items in modules.items():
        if not isinstance(module, str) or not module.strip():
            raise ReportError("module name must be a nonempty string")
        if not isinstance(items, list):
            raise ReportError("module %s must contain an item array" % module)
        normalized[module] = []
        for index, item in enumerate(items):
            context = "%s item %d" % (module, index + 1)
            if not isinstance(item, dict):
                raise ReportError("%s must be an object" % context)
            if set(item) - {"key", "value", "note"}:
                raise ReportError("%s has unsupported fields" % context)
            if not all(isinstance(item.get(field), str) for field in ("key", "value")):
                raise ReportError("%s key and value must be strings" % context)
            if not item["key"].strip():
                raise ReportError("%s key must not be empty" % context)
            if not isinstance(item.get("note", ""), str):
                raise ReportError("%s note must be a string" % context)
            # Preserve original content and optional-note presence for rendering.
            normalized[module].append(dict(item))
    return normalized


def load_report(path):
    """Return (host, modules) from a UTF-8[-BOM] single-host JSON file.

    Duplicate object keys are rejected before JSON decoding can discard evidence.
    Notes are optional; keys and values must be strings, matching machconf output.
    """
    try:
        data = _decode(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ReportError("cannot load report %s: %s" % (path, exc)) from exc
    if not isinstance(data, dict) or len(data) != 1:
        raise ReportError("report must contain exactly one host")
    host, modules = next(iter(data.items()))
    if not isinstance(host, str) or not host.strip():
        raise ReportError("host must be a nonempty string")
    return host, _validate_modules(modules)


def _unknown_string(value):
    stripped = value.strip().lower()
    if "<redacted>" in stripped or stripped in _UNKNOWN:
        return True
    # Status strings can include a short reason, but a path containing a status
    # word (e.g. /etc/unknown.conf) is still a normal observed value.
    return any(stripped.startswith(marker + delimiter)
               for marker in _UNKNOWN for delimiter in (":", " (", "（"))


def _unknown_tree(value):
    if isinstance(value, str):
        return _unknown_string(value)
    if isinstance(value, list):
        return any(_unknown_tree(item) for item in value)
    if isinstance(value, dict):
        if value.get("errors") or value.get("unreadable_count", 0):
            return True
        return any(_unknown_tree(item) for item in value.values())
    return False


def _comparable(value):
    """Return availability and a type-preserving semantic signature."""
    if _unknown_string(value):
        return False, value
    if value.lstrip().startswith(("{", "[")):
        try:
            decoded = _decode(value)
        except ReportError:
            return False, value
        except ValueError:
            # Free-text values may legitimately start with '[' or '{'.
            return True, ("text", value)
        return (not _unknown_tree(decoded),
                ("json", json.dumps(decoded, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False)))
    return True, ("text", value)


def _group(modules):
    groups = defaultdict(list)
    for module, items in modules.items():
        for item in items:
            key = item["key"]
            if module == "cube-runtime":
                if key == "采集时间":
                    continue
                if key == "进程扫描" and _comparable(item["value"])[0]:
                    continue
                key = _PID.sub("pid=*", key)
            groups[(module, key)].append(item)
    return groups


def compare_reports(left_host, left_modules, right_host, right_modules):
    """Compare observations by (module, key), retaining both original item lists.

    Repeated keys (including PID-normalization collisions) are ambiguous. Missing
    entries remain one-sided; unknown/redacted values are never reported equal.
    Notes are context only. JSON objects compare independent of key order; array
    order, counts, units and ordinary strings are preserved without inference.
    Collection timestamps and healthy process-scan diagnostics are omitted only
    for cube-runtime. Unhealthy scan diagnostics remain visible as unavailable.
    """
    for host in (left_host, right_host):
        if not isinstance(host, str) or not host.strip():
            raise ReportError("host must be a nonempty string")
    left = _group(_validate_modules(left_modules))
    right = _group(_validate_modules(right_modules))
    rows = []
    summary = dict.fromkeys(STATUSES, 0)
    priorities = {name: index for index, name in enumerate(MODULE_ORDER)}
    ordered_keys = sorted(
        set(left) | set(right),
        key=lambda pair: (priorities.get(pair[0], len(priorities)), *pair),
    )
    for module, key in ordered_keys:
        a, b = left.get((module, key), []), right.get((module, key), [])
        if len(a) > 1 or len(b) > 1:
            status = "ambiguous"
        elif not b:
            status = "left_only"
        elif not a:
            status = "right_only"
        else:
            available_a, value_a = _comparable(a[0]["value"])
            available_b, value_b = _comparable(b[0]["value"])
            if not available_a or not available_b:
                status = "unavailable"
            else:
                status = "equal" if value_a == value_b else "different"
        rows.append({"module": module, "key": key, "left": a, "right": b,
                     "status": status})
        summary[status] += 1
    return {"hosts": [left_host, right_host], "rows": rows, "summary": summary}
