#!/usr/bin/env python3
"""Read-only Cube runtime probe; standalone Python 3.8+, no dependencies.

Output is a single JSON object. Environment values are allowlisted before they
leave this process. Neither unit files nor application configuration are sourced
or executed. systemd properties describe requested settings; /proc and cgroup
files describe separately sampled kernel state, not an atomic snapshot.
"""

import argparse
import datetime
import errno
import json
import os
import re
import subprocess


ENV_ALLOWLIST = frozenset((
    "GOMAXPROCS", "GOGC", "GOMEMLIMIT", "GODEBUG", "WORKER_THREADS",
    "TOKIO_WORKER_THREADS",
))
SERVICES = (
    ("cube-sandbox-cubemaster.service", "CubeMaster", "cubemaster", "CubeMaster/bin/cubemaster"),
    ("cube-sandbox-cubelet.service", "cubelet", "cubelet", "Cubelet/bin/cubelet"),
    ("cube-sandbox-cube-api.service", "cube-api", "cube-api", "CubeAPI/bin/cube-api"),
)
PROPERTIES = (
    "LoadState", "ActiveState", "SubState", "MainPID",
    "EnvironmentFiles", "PassEnvironment",
    "UnsetEnvironment", "CPUAffinity", "AllowedCPUs", "EffectiveCPUs",
    "AllowedMemoryNodes", "EffectiveMemoryNodes", "NUMAPolicy", "NUMAMask",
    "CPUQuotaPerSecUSec", "MemoryMax", "LimitNOFILE", "ControlGroup",
    "NeedDaemonReload",
)
MAX_READ = 1024 * 1024
MAX_THREADS = 32768
MAX_CGROUP_LEVELS = 128
CGROUP_FILES = {
    "2": ("cpuset.cpus", "cpuset.cpus.effective", "cpuset.mems",
          "cpuset.mems.effective", "cpu.max", "cpu.weight", "memory.max",
          "memory.high", "memory.swap.max"),
    "cpuset": ("cpuset.cpus", "cpuset.effective_cpus", "cpuset.mems",
               "cpuset.effective_mems"),
    "cpu": ("cpu.cfs_quota_us", "cpu.cfs_period_us", "cpu.shares"),
    "memory": ("memory.limit_in_bytes", "memory.soft_limit_in_bytes",
               "memory.memsw.limit_in_bytes", "memory.use_hierarchy"),
}
CONFIG_PATHS = ("CubeMaster/conf.yaml", "Cubelet/config/config.toml",
                "Cubelet/dynamicconf/conf.yaml")
CONFIG_KEYS = frozenset(("create_concurrent_limit",
                         "pool_workers", "max_concurrent_requests", "concurrent",
                         "max_concurrent_downloads", "creation_concurrent_num",
                         "vm_memory_overhead_base", "vm_memory_overhead_coefficient",
                         "host_memory_overhead_base"))


def _error(exc):
    """Never include raw stderr, file content or exception messages."""
    if isinstance(exc, PermissionError):
        return "permission-denied"
    if isinstance(exc, FileNotFoundError):
        return "not-found-or-process-exited"
    if isinstance(exc, subprocess.TimeoutExpired):
        return "command-timeout"
    if isinstance(exc, OSError):
        return "os-error-%s" % (exc.errno if exc.errno is not None else "unknown")
    return "invalid-or-unavailable-data"


def _read(path, binary=False):
    with open(path, "rb") as stream:
        data = stream.read(MAX_READ + 1)
    if len(data) > MAX_READ:
        raise ValueError("read limit exceeded")
    return data if binary else data.decode("utf-8", errors="replace")


def _unescape(text):
    """Decode systemd's C escapes without shell expansion or evaluation."""
    simple = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r",
              "s": " ", "t": "\t", "v": "\v", "\\": "\\", '"': '"', "'": "'"}
    out, i = [], 0
    while i < len(text):
        if text[i] != "\\":
            out.append(text[i])
            i += 1
            continue
        i += 1
        if i == len(text):
            raise ValueError("incomplete escape")
        ch = text[i]
        if ch in simple:
            out.append(simple[ch])
            i += 1
        elif ch in "xuU":
            size = {"x": 2, "u": 4, "U": 8}[ch]
            value = text[i + 1:i + 1 + size]
            if len(value) != size or not re.fullmatch(r"[0-9a-fA-F]+", value):
                raise ValueError("invalid escape")
            out.append(chr(int(value, 16)))
            i += size + 1
        elif ch in "01234567":
            value = text[i:i + 3]
            if len(value) != 3 or not re.fullmatch(r"[0-7]{3}", value):
                raise ValueError("invalid octal escape")
            out.append(chr(int(value, 8)))
            i += 3
        else:
            raise ValueError("unsupported escape")
    return "".join(out)


def _words(text):
    """Split quoted systemctl-show words, rejecting ambiguous input."""
    words, buf, quote, started, i = [], [], None, False, 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            if i + 1 == len(text):
                raise ValueError("incomplete escape")
            # Preserve escapes for the C decoder; escaped quotes cannot close a word.
            buf.extend((ch, text[i + 1]))
            started = True
            i += 2
            continue
        if quote:
            if ch == quote:
                quote = None
            else:
                buf.append(ch)
        elif ch in "\"'":
            quote = ch
            started = True
        elif ch.isspace():
            if started:
                words.append(_unescape("".join(buf)))
                buf, started = [], False
        else:
            buf.append(ch)
            started = True
        i += 1
    if quote:
        raise ValueError("unclosed quote")
    if started:
        words.append(_unescape("".join(buf)))
    return words


def _environment(entries):
    result = {}
    for entry in entries:
        name, separator, value = entry.partition("=")
        # Malformed names are not output because they may themselves carry a value.
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        if name in ENV_ALLOWLIST:
            result[name] = value
    return result


def _systemd(name):
    out = {"status": "unavailable", "properties": {}, "environment": {},
           "environment_status": "unavailable", "errors": []}
    try:
        result = subprocess.run(
            ["systemctl", "show", "--no-pager", "--property=" +
             ",".join(PROPERTIES + ("Environment",)), "--", name],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=8, check=False, env=dict(os.environ, LC_ALL="C", SYSTEMD_COLORS="0"))
        if len(result.stdout) > MAX_READ:
            raise ValueError("output limit exceeded")
        if result.returncode != 0:
            out["errors"].append("systemctl-exit-%d" % result.returncode)
        raw = {}
        for line in result.stdout.decode("utf-8", errors="replace").splitlines():
            key, sep, value = line.partition("=")
            if sep and key in PROPERTIES + ("Environment",):
                raw[key] = value
        # Keep only resource-related environment references. UnsetEnvironment
        # can include NAME=value, whose value must never be echoed here.
        for key in PROPERTIES:
            if key not in raw:
                continue
            if key in ("PassEnvironment", "UnsetEnvironment"):
                try:
                    out["properties"][key] = [
                        word.split("=", 1)[0] + ("=<redacted>" if "=" in word else "")
                        for word in _words(raw[key])
                        if word.split("=", 1)[0] in ENV_ALLOWLIST]
                except ValueError:
                    out["errors"].append(key + "-parse-failed")
            else:
                out["properties"][key] = raw[key]
        if raw.get("LoadState") == "not-found":
            out["status"] = "not-found"
        elif result.returncode == 0 and raw.get("LoadState"):
            out["status"] = "ok"
        if "Environment" in raw:
            try:
                out["environment"] = _environment(_words(raw["Environment"]))
                out["environment_status"] = "ok" if raw["Environment"].strip() else "empty"
            except (ValueError, OverflowError):
                out["errors"].append("Environment-parse-failed")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        out["errors"].append(_error(exc))
    return out


def _starttime(pid):
    # comm (field 2) may contain whitespace and ')' characters.
    raw = _read("/proc/%d/stat" % pid)
    tail = raw[raw.rindex(")") + 2:].split()
    return int(tail[19])  # field 22


def _basename(exe):
    return os.path.basename(exe[:-10] if exe.endswith(" (deleted)") else exe)


def _threads(pid):
    out = {"status": "unavailable", "observed_count": 0, "unreadable_count": 0,
           "affinity_groups": [], "errors": []}
    try:
        names = os.listdir("/proc/%d/task" % pid)
    except OSError as exc:
        out["errors"].append(_error(exc))
        return out
    groups = {}
    tids = sorted(int(name) for name in names if name.isdigit())
    if len(tids) > MAX_THREADS:
        out["errors"].append("thread-read-limit-exceeded")
    for tid in tids[:MAX_THREADS]:
        try:
            raw = _read("/proc/%d/task/%d/status" % (pid, tid))
            values = dict(line.split(":", 1) for line in raw.splitlines() if ":" in line)
            cpus = values["Cpus_allowed_list"].strip()
            mems = values["Mems_allowed_list"].strip()
            if not all(re.fullmatch(r"[0-9,-]*", value) for value in (cpus, mems)):
                raise ValueError("invalid affinity")
            groups[(cpus, mems)] = groups.get((cpus, mems), 0) + 1
            out["observed_count"] += 1
        except (OSError, ValueError, KeyError):
            out["unreadable_count"] += 1
    out["affinity_groups"] = [
        {"cpus_allowed_list": key[0], "mems_allowed_list": key[1], "count": count}
        for key, count in sorted(groups.items())]
    out["status"] = "partial" if out["unreadable_count"] or out["errors"] else "ok"
    if not out["observed_count"]:
        out["status"] = "unavailable"
    return out


def _mount_unescape(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def _mounts():
    mounts = []
    for line in _read("/proc/self/mountinfo").splitlines():
        before, sep, after = line.partition(" - ")
        fields, tail = before.split(), after.split()
        if not sep or len(fields) < 6 or len(tail) < 3 or tail[0] not in ("cgroup", "cgroup2"):
            continue
        mounts.append({"version": "2" if tail[0] == "cgroup2" else "1",
                       "root": _mount_unescape(fields[3]),
                       "point": _mount_unescape(fields[4]),
                       "controllers": tail[2].split(",")})
    return mounts


def _contained(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def _cgroups(pid, mounts):
    out = {"status": "unavailable", "memberships": [], "errors": []}
    try:
        lines = _read("/proc/%d/cgroup" % pid).splitlines()
    except (OSError, ValueError) as exc:
        out["errors"].append(_error(exc))
        return out
    for line in lines:
        parts = line.split(":", 2)
        if len(parts) != 3:
            out["errors"].append("invalid-cgroup-membership")
            continue
        _, controllers, path = parts
        version = "1" if controllers else "2"
        ctrls = controllers.split(",") if controllers else []
        relevant = [ctrl for ctrl in ctrls if ctrl in ("cpu", "cpuset", "memory")]
        if version == "1" and not relevant:
            continue
        entry = {"version": version, "controllers": relevant, "path": path,
                 "mount_point": None, "mount_root": None, "levels": [], "errors": []}
        out["memberships"].append(entry)
        if not path.startswith("/") or ".." in path.split("/"):
            entry["errors"].append("cgroup-path-outside-visible-namespace")
            continue
        candidates = [mount for mount in mounts if mount["version"] == version
                      and (version == "2" or all(ctrl in mount["controllers"] for ctrl in relevant))
                      and _contained(path, mount["root"])]
        if not candidates:
            entry["errors"].append("matching-cgroup-mount-unavailable")
            continue
        # Prefer the broadest visible hierarchy so parent limits are included.
        mount = min(candidates, key=lambda item: len(item["root"]))
        entry.update(mount_point=mount["point"], mount_root=mount["root"])
        directory = os.path.normpath(os.path.join(mount["point"], path[len(mount["root"]):].lstrip("/")))
        if not _contained(directory, mount["point"]):
            entry["errors"].append("cgroup-path-outside-visible-mount")
            continue
        filenames = CGROUP_FILES["2"] if version == "2" else tuple(
            filename for ctrl in relevant for filename in CGROUP_FILES[ctrl])
        for _ in range(MAX_CGROUP_LEVELS):
            level = {"path": directory, "values": {}, "errors": []}
            entry["levels"].append(level)
            for filename in filenames:
                try:
                    value = _read(os.path.join(directory, filename)).strip()
                    # These kernel files only contain numbers, ranges, whitespace or max.
                    if not re.fullmatch(r"(?:[0-9,\s-]|max)*", value):
                        raise ValueError("invalid cgroup value")
                    level["values"][filename] = value
                except FileNotFoundError:
                    # Controller/file absent in this hierarchy or kernel version.
                    continue
                except (OSError, ValueError) as exc:
                    level["errors"].append(filename + ":" + _error(exc))
            if directory == mount["point"]:
                break
            parent = os.path.dirname(directory)
            if parent == directory or not _contained(parent, mount["point"]):
                break
            directory = parent
        else:
            entry["errors"].append("cgroup-depth-limit-exceeded")
    has_values = any(level["values"] for entry in out["memberships"] for level in entry["levels"])
    has_errors = (out["errors"] or any(entry["errors"] or any(level["errors"] for level in entry["levels"])
                                     for entry in out["memberships"]))
    out["status"] = "partial" if has_values and has_errors else "ok" if has_values else "unavailable"
    return out


def _process(pid, sources, expected, expected_path, mounts):
    out = {"pid": pid, "exe": None, "source": sorted(sources), "identity_status": "unknown",
           "expected_executable": expected, "expected_path": expected_path, "path_status": "unknown",
           "environment": {}, "environment_status": "unavailable",
           "threads": {"status": "unavailable", "affinity_groups": []},
           "cgroups": {"status": "unavailable", "memberships": []}, "errors": []}
    try:
        before = _starttime(pid)
    except (OSError, ValueError, IndexError) as exc:
        out["errors"].append("process-start:" + _error(exc))
        return out
    try:
        out["exe"] = os.readlink("/proc/%d/exe" % pid)
        out["identity_status"] = "match" if _basename(out["exe"]) == expected else "unexpected-executable"
        actual_path = out["exe"][:-10] if out["exe"].endswith(" (deleted)") else out["exe"]
        out["path_status"] = "expected-path" if os.path.normpath(actual_path) == os.path.normpath(expected_path) else "alternate-path"
    except OSError as exc:
        out["errors"].append("exe:" + _error(exc))
    try:
        raw = _read("/proc/%d/environ" % pid, binary=True)
        out["environment"] = _environment(raw.decode("utf-8", errors="replace").split("\0"))
        out["environment_status"] = "ok" if raw else "empty"
    except (OSError, ValueError) as exc:
        out["errors"].append("environment:" + _error(exc))
    out["threads"] = _threads(pid)
    out["cgroups"] = _cgroups(pid, mounts)
    try:
        after = _starttime(pid)
        exe_after = os.readlink("/proc/%d/exe" % pid) if out["exe"] else None
        stable = before == after and out["exe"] == exe_after
    except (OSError, ValueError, IndexError):
        stable = False
    if not stable:
        out.update(identity_status="raced", path_status="raced", exe=None, environment={}, environment_status="raced",
                   threads={"status": "raced", "affinity_groups": []},
                   cgroups={"status": "raced", "memberships": []})
        out["errors"].append("process-exited-execed-or-pid-reused-during-sampling")
    else:
        out["starttime_ticks"] = before
    return out


def _config_files(root):
    """Extract only named numeric settings, not a YAML/TOML parser."""
    results = []
    for relative in CONFIG_PATHS:
        path = os.path.join(root, relative)
        item = {"path": path, "status": "unavailable", "items": [],
                "note": "磁盘配置摘录，未验证运行时加载；不解析启动参数指定替代路径"}
        results.append(item)
        try:
            raw = _read(path)
        except FileNotFoundError:
            item["status"] = "missing"
            continue
        except (OSError, ValueError) as exc:
            item["error"] = _error(exc)
            continue
        item["status"] = "ok"
        section, parents = "", []
        toml = relative.endswith(".toml")
        for number, line in enumerate(raw.splitlines(), 1):
            if toml:
                component = r'''(?:[A-Za-z0-9_-]+|"[A-Za-z0-9_.-]+"|'[A-Za-z0-9_.-]+')'''
                header = re.fullmatch(r"\s*\[(" + component + r"(?:\." + component + r")*)\]\s*(?:#.*)?", line)
                if header:
                    section = header.group(1)
                    continue
                # Do not retain a previous section for an unsupported header.
                if line.lstrip().startswith("["):
                    section = "<unparsed-section>"
                match = re.fullmatch(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*(?:#.*)?", line)
            else:
                match = re.fullmatch(r"( *)([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*?)\s*(?:#.*)?", line)
                if not match:
                    continue
                indentation, key, value = match.groups()
                depth = len(indentation)
                while parents and parents[-1][0] >= depth:
                    parents.pop()
                section = ".".join(parent[1] for parent in parents)
                if not value:
                    parents.append((depth, key))
                    continue
            if not match:
                continue
            key, value = match.groups() if toml else (key, value)
            if key not in CONFIG_KEYS:
                continue
            if len(value) > 128:
                continue
            if value[:1] in ("'", '"') and value[-1:] == value[:1]:
                value = value[1:-1]
            if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?(?:ns|us|µs|ms|s|m|h|B|KB|MB|GB|TB|Ki|Mi|Gi|Ti|KiB|MiB|GiB|TiB)?", value):
                continue
            item["items"].append({"line": number, "section": section, "key": key, "value": value})
    return results


def collect(root="/usr/local/services/cubetoolbox"):
    out = {"schema_version": 1,
           "collected_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
           "cube_root": root, "services": [], "errors": [], "config_files": _config_files(root),
           "notes": ["Environment is a startup/proc snapshot, not proof of current Go runtime values.",
                     "CubeAPI environment is sampled from /proc; absent variables do not establish runtime defaults.",
                     "systemd settings and sampled kernel affinity/cgroup limits are distinct.",
                     "Cgroup ancestor limits are raw observations; no effective quota is inferred."]}
    mounts = []
    try:
        mounts = _mounts()
    except (OSError, ValueError) as exc:
        out["errors"].append("cgroup-mounts:" + _error(exc))
    discoveries = {spec[2]: {} for spec in SERVICES}
    unreadable = 0
    scan_failed = False
    try:
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                exe = os.readlink("/proc/%s/exe" % name)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EPERM):
                    unreadable += 1
                continue
            basename = _basename(exe)
            if basename in discoveries:
                discoveries[basename][int(name)] = {"proc-exe-scan"}
    except OSError as exc:
        scan_failed = True
        out["errors"].append("process-scan:" + _error(exc))
    out["process_scan"] = {"status": "partial" if unreadable else "ok",
                           "permission_denied_count": unreadable}
    if scan_failed or not os.path.isdir("/proc/self"):
        out["process_scan"]["status"] = "unavailable"
    for name, label, expected, relative in SERVICES:
        service = {"name": name, "label": label, "systemd": _systemd(name),
                   "processes": [], "errors": []}
        out["services"].append(service)
        pids = discoveries[expected]
        try:
            main_pid = int(service["systemd"]["properties"].get("MainPID", "0"))
            if main_pid > 0:
                pids.setdefault(main_pid, set()).add("systemd-mainpid")
        except ValueError:
            service["errors"].append("invalid-systemd-MainPID")
        for pid, sources in sorted(pids.items()):
            service["processes"].append(_process(pid, sources, expected, os.path.join(root, relative), mounts))
        if not pids:
            service["errors"].append("no-readable-matching-process-found")
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cube-root", default="/usr/local/services/cubetoolbox")
    args = parser.parse_args()
    print(json.dumps(collect(args.cube_root), ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    main()
