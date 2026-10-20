"""Read-only Cube resource inspection; independent of release comparison."""

import json
import shlex
from pathlib import Path

from .base import Item, Module
from .cube_runtime_probe import ENV_ALLOWLIST, PROPERTIES, CONFIG_KEYS, CGROUP_FILES

DEFAULT_ROOT = "/usr/local/services/cubetoolbox"
TAG = "cube_runtime"


def probe_script(root=None):
    source = Path(__file__).with_name("cube_runtime_probe.py").read_text()
    # Run the same source locally/remotely, without uploading or installing files.
    return ("if command -v python3 >/dev/null 2>&1; then\n"
            "python3 - --cube-root %s <<'MACHCONF_RUNTIME_PY'\n%s\n"
            "MACHCONF_RUNTIME_PY\nelse\n"
            "printf '%%s\\n' '{\"schema_version\":1,\"services\":[],"
            "\"errors\":[\"python3 unavailable on target\"]}'\nfi\n"
            % (shlex.quote(root or DEFAULT_ROOT), source))


def decode_report(text):
    try:
        data = json.loads(text)
        if not isinstance(data, dict) or not isinstance(data.get("services"), list):
            raise ValueError("invalid schema")
        return data
    except (ValueError, TypeError):
        return {"schema_version": 1, "services": [],
                "errors": ["runtime probe returned no valid JSON (no raw output displayed)"]}


def _display(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value) if value is not None else "N.A."


def report_items(data):
    items = [Item("采集时间", data.get("collected_at", "N.A.")),
             Item("检测口径", "资源配置：Go / worker / CPU / NUMA / 内存 / 文件句柄 / 并发",
                  "systemd 配置与运行进程/cgroup 分列；环境变量不是 Go API 实时值；仅输出资源白名单")]
    if data.get("process_scan") and data["process_scan"].get("status") != "ok":
        items.append(Item("进程扫描", _display(data["process_scan"]), "扫描可见性不足时，未观测到不等于没有运行"))
    for err in data.get("errors", []):
        items.append(Item("探测错误", "N.A.", str(err)))
    for svc in data.get("services", []):
        label = svc.get("label", svc.get("name", "Cube"))
        sd = svc.get("systemd", {})
        properties = sd.get("properties", {})
        state_keys = ("LoadState", "ActiveState", "SubState", "MainPID")
        state = "; ".join("%s=%s" % (k, properties[k]) for k in state_keys if k in properties)
        items.append(Item(label + " systemd", sd.get("status", "unavailable"),
                          "; ".join(x for x in (svc.get("name", ""), state) if x)))
        for key, value in properties.items():
            if key not in PROPERTIES or key in state_keys:
                continue
            if key in ("PassEnvironment", "UnsetEnvironment"):
                # The collector also filters these; retain that boundary when
                # rendering an older report supplied directly by a caller.
                if not isinstance(value, list):
                    continue
                value = [entry.split("=", 1)[0] + ("=<redacted>" if "=" in entry else "")
                         for entry in value if entry.split("=", 1)[0] in ENV_ALLOWLIST]
                if not value:
                    continue
            items.append(Item(label + " systemd." + key, _display(value) or "(未设置)",
                              "systemd manager 当前加载值；不代表 /proc 实际限制"))
        items.append(Item(label + " systemd.Environment 状态", sd.get("environment_status", "unavailable"),
                          "仅 Environment；EnvironmentFiles 列引用，文件内容不执行或求值"))
        for key, value in sd.get("environment", {}).items():
            if key in ENV_ALLOWLIST:
                items.append(Item(label + " systemd.env." + key, _display(value)))
        for err in sd.get("errors", []):
            items.append(Item(label + " systemd 错误", "N.A.", str(err)))
        processes = svc.get("processes", [])
        if not processes:
            items.append(Item(label + " 运行进程", "未观测到", "服务未运行、工具不可用或权限不足；不推断运行配置"))
        for proc in processes:
            prefix = "%s pid=%s" % (label, proc.get("pid", "?"))
            items.append(Item(prefix + " exe", proc.get("exe") or "N.A.",
                              "identity=%s; path=%s; expected=%s; source=%s" % (proc.get("identity_status"), proc.get("path_status", "unknown"), proc.get("expected_path", "?"), _display(proc.get("source", [])))))
            items.append(Item(prefix + " 环境读取", proc.get("environment_status", "unavailable"),
                              "/proc/<pid>/environ；启动环境快照，非程序内部实时参数"))
            for key, value in proc.get("environment", {}).items():
                if key in ENV_ALLOWLIST:
                    items.append(Item(prefix + " env." + key, _display(value)))
            sd_env, proc_env = sd.get("environment", {}), proc.get("environment", {})
            if ("systemd-mainpid" in proc.get("source", [])
                    and proc.get("identity_status") == "match"
                    and sd.get("environment_status") in ("ok", "empty")
                    and proc.get("environment_status") in ("ok", "empty")):
                for key in sorted(ENV_ALLOWLIST):
                    if key in sd_env or key in proc_env:
                        a, b = sd_env.get(key), proc_env.get(key)
                        verdict = "相同" if a == b else "不同"
                        items.append(Item(prefix + " env对照." + key, verdict,
                                          "systemd Environment=%s; process=%s；不同可能来自 EnvironmentFile、启动脚本或尚未重启"
                                          % (a if a is not None else "未直接设置", b if b is not None else "未设置")))
            threads = proc.get("threads", {})
            items.append(Item(prefix + " 线程亲和性", _display(threads),
                              "实际观测线程的 CPU / NUMA allowed list；不等于内存分配位置"))
            cg = proc.get("cgroups", {})
            items.append(Item(prefix + " cgroup 状态", cg.get("status", "unavailable")))
            for membership in cg.get("memberships", []):
                controllers = [c for c in membership.get("controllers", []) if c in ("cpu", "cpuset", "memory")]
                if str(membership.get("version")) == "1" and not controllers:
                    continue
                desc = "v%s %s" % (membership.get("version", "?"), _display(controllers))
                for err in membership.get("errors", []):
                    items.append(Item(prefix + " " + desc + " cgroup 错误", "N.A.", str(err)))
                for level in membership.get("levels", []):
                    allowed = {name for names in CGROUP_FILES.values() for name in names}
                    values = {key: value for key, value in level.get("values", {}).items() if key in allowed}
                    if values:
                        items.append(Item(prefix + " " + desc + " " + str(level.get("path")) + " 资源",
                                          _display(values), "实际 cgroup 文件；同层合并展示，保留祖先限制，不推断统一有效限额"))
                    for err in level.get("errors", []):
                        items.append(Item(prefix + " cgroup 读取错误", "N.A.", str(err)))
            for err in cg.get("errors", []) + proc.get("errors", []):
                items.append(Item(prefix + " 读取错误", "N.A.", str(err)))
        for err in svc.get("errors", []):
            items.append(Item(label + " 错误", "N.A.", str(err)))
    for config in data.get("config_files", []):
        path = config.get("path", "?")
        items.append(Item("应用文件 " + path, config.get("status", "unavailable"),
                          config.get("note", "磁盘配置，未验证运行时加载")))
        for entry in config.get("items", []):
            if entry.get("key") not in CONFIG_KEYS:
                continue
            items.append(Item("%s:%s %s.%s" % (path, entry.get("line", "?"), entry.get("section", ""), entry.get("key", "?")),
                              _display(entry.get("value")), "配置文件白名单摘录；不是当前运行时配置接口"))
        for err in config.get("errors", []) + ([config["error"]] if config.get("error") else []):
            items.append(Item("应用文件错误 " + path, "N.A.", str(err)))
    return items


class CubeRuntimeModule(Module):
    name = "cube-runtime"
    title = "Cube 运行资源配置"
    group = "Cube 运行资源配置 (只读)"
    timeout = 120

    def __init__(self, root=None):
        self.root = root

    def probes(self):
        return [(TAG, probe_script(self.root))]

    def parse(self, sections):
        return report_items(decode_report(sections.get(TAG, "")))


def collect_runtime(runner, root=None):
    # Unlike build_script(), retain a remote Python failure as a runner error.
    return decode_report(runner.run_script(probe_script(root)))
