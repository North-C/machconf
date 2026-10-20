"""Strict, side-effect-free configuration for CubeSandbox runtime tuning.

Only explicitly supplied settings are rendered.  This module has no host or
topology assumptions; the execution backend validates availability on the host.
"""

import re


MARKER = "# Managed by machconf cube-tune. Do not edit."
GO_KEYS = ("GOMAXPROCS", "GOGC", "GOMEMLIMIT", "GOTRACEBACK", "GODEBUG")
MAX_ID = 1048575
MAX_IDS = 65536
RCU_KEYS = ("rcu_expedited", "rcu_normal")


class ConfigError(ValueError):
    """The input cannot safely and unambiguously express a tuning request."""


def _fail(message):
    raise ConfigError(message)


def _object(value, keys, where):
    if not isinstance(value, dict):
        _fail("{} must be an object".format(where))
    unknown = set(value) - set(keys)
    if unknown:
        _fail("{} contains unknown keys: {}".format(where, ", ".join(sorted(map(str, unknown)))))


def parse_id_list(value):
    """Parse a bounded Linux CPU/node list; reject ambiguous or huge inputs."""
    if not isinstance(value, str) or not value or len(value) > 262144:
        _fail("CPU/node list must be a nonempty bounded string")
    ids = set()
    expansion_work = 0
    for item in value.split(","):
        if not re.fullmatch(r"[0-9]{1,7}(?:-[0-9]{1,7})?", item):
            _fail("invalid CPU/node list: {!r}".format(value[:120]))
        start, _, end = item.partition("-")
        start, end = int(start), int(end or start)
        if start > end or end > MAX_ID or end - start + 1 > MAX_IDS:
            _fail("CPU/node range is reversed or too large")
        expansion_work += end - start + 1
        if expansion_work > MAX_IDS * 4:
            _fail("CPU/node list contains too many overlapping ranges")
        ids.update(range(start, end + 1))
        if len(ids) > MAX_IDS:
            _fail("CPU/node list expands to too many IDs")
    return ids


def _canonical_ids(value):
    ids = sorted(parse_id_list(value))
    groups = []
    start = previous = ids[0]
    for number in ids[1:]:
        if number != previous + 1:
            groups.append(str(start) if start == previous else "{}-{}".format(start, previous))
            start = number
        previous = number
    groups.append(str(start) if start == previous else "{}-{}".format(start, previous))
    return ",".join(groups)


def _go_value(key, value):
    if not isinstance(value, str) or not value or len(value) > 4096:
        _fail("{} must be a nonempty string of at most 4096 characters".format(key))
    if any(ord(char) < 32 or ord(char) > 126 for char in value) or "%" in value:
        _fail("{} contains unsafe control characters or systemd specifiers".format(key))
    if key == "GOMAXPROCS":
        valid = re.fullmatch(r"[0-9]{1,10}", value) and 1 <= int(value) <= 2147483647
    elif key == "GOGC":
        valid = value == "off" or (re.fullmatch(r"[0-9]{1,10}", value) and int(value) <= 2147483647)
    elif key == "GOMEMLIMIT":
        match = re.fullmatch(r"([0-9]{1,19})(B|KiB|MiB|GiB|TiB)?", value)
        valid = value == "off"
        if match:
            scale = {None: 1, "B": 1, "KiB": 1024, "MiB": 1024 ** 2,
                     "GiB": 1024 ** 3, "TiB": 1024 ** 4}[match.group(2)]
            valid = int(match.group(1)) * scale <= 9223372036854775807
    elif key == "GOTRACEBACK":
        valid = value in ("none", "single", "all", "system", "crash", "wer", "0", "1", "2")
    else:
        # GODEBUG is an extensible list of key=value pairs. Do not freeze its
        # keys to one Go release, but exclude quoting/specifier injection.
        valid = all(re.fullmatch(r"#?[A-Za-z][A-Za-z0-9_]*=[A-Za-z0-9_.:+/\-]+", part)
                    for part in value.split(","))
    if not valid:
        _fail("invalid {} value: {!r}".format(key, value))
    return value


def _service(value):
    _object(value, ("unit", "runtime", "go_env", "cpu_affinity", "numa_policy", "numa_nodes", "restart"), "service")
    unit = value.get("unit")
    if (not isinstance(unit, str) or len(unit) > 255 or
            not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.:@-]*\.service", unit) or
            unit.count("@") > 1 or "@.service" in unit):
        _fail("service.unit must name an explicit safe .service unit")
    runtime = value.get("runtime", "other")
    if runtime not in ("go", "rust", "other"):
        _fail("service.runtime must be go, rust or other")
    restart = value.get("restart", False)
    if type(restart) is not bool:
        _fail("service.restart must be a boolean")
    result = {"unit": unit, "runtime": runtime, "restart": restart}
    if "go_env" in value:
        _object(value["go_env"], GO_KEYS, "service.go_env")
        if runtime != "go":
            _fail("service.go_env requires runtime=go")
        if not value["go_env"]:
            _fail("service.go_env must contain at least one setting")
        result["go_env"] = {key: _go_value(key, value["go_env"][key])
                            for key in GO_KEYS if key in value["go_env"]}
    if "cpu_affinity" in value:
        result["cpu_affinity"] = _canonical_ids(value["cpu_affinity"])
    if "numa_policy" in value:
        policy = value["numa_policy"]
        if policy not in ("default", "bind", "interleave", "preferred", "local"):
            _fail("invalid service.numa_policy")
        result["numa_policy"] = policy
        if policy in ("bind", "interleave", "preferred"):
            if "numa_nodes" not in value:
                _fail("NUMA {} policy requires numa_nodes".format(policy))
            result["numa_nodes"] = _canonical_ids(value["numa_nodes"])
            if policy == "preferred" and len(parse_id_list(result["numa_nodes"])) != 1:
                _fail("NUMA preferred policy requires exactly one node")
        elif "numa_nodes" in value:
            _fail("NUMA default/local policy does not accept numa_nodes")
    elif "numa_nodes" in value:
        _fail("numa_nodes requires numa_policy")
    if not set(result) & {"go_env", "cpu_affinity", "numa_policy"}:
        _fail("service must specify at least one tuning setting")
    return result


def _container(value):
    _object(value, ("name", "cpuset_cpus", "cpuset_mems"), "container")
    name = value.get("name")
    if (not isinstance(name, str) or len(name) > 255 or
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name)):
        _fail("container.name must be an explicit safe Docker name")
    result = {"name": name}
    for key in ("cpuset_cpus", "cpuset_mems"):
        if key in value:
            result[key] = _canonical_ids(value[key])
    if len(result) == 1:
        _fail("container must specify cpuset_cpus or cpuset_mems")
    return result


def validate_config(data):
    """Return a fresh canonical config, or raise ConfigError (ValueError)."""
    _object(data, ("version", "services", "containers", "rcu"), "config")
    if type(data.get("version")) is not int or data["version"] != 1:
        _fail("config.version must be 1")
    result = {"version": 1}
    for key, convert, identity in (("services", _service, "unit"),
                                    ("containers", _container, "name")):
        values = data.get(key, [])
        if not isinstance(values, list):
            _fail("config.{} must be a list".format(key))
        result[key] = [convert(value) for value in values]
        names = [value[identity] for value in result[key]]
        if len(names) != len(set(names)):
            _fail("duplicate {} targets".format(key))
    if "rcu" in data:
        _object(data["rcu"], RCU_KEYS, "rcu")
        if set(data["rcu"]) != set(RCU_KEYS):
            _fail("rcu must explicitly specify both rcu_expedited and rcu_normal")
        if any(type(data["rcu"][k]) is not int or data["rcu"][k] not in (0, 1) for k in RCU_KEYS):
            _fail("RCU values must be JSON integers 0 or 1")
        if all(data["rcu"].values()):
            _fail("rcu_normal=1 overrides rcu_expedited=1; choose one explicitly")
        result["rcu"] = {k: data["rcu"][k] for k in RCU_KEYS}
    if not result["services"] and not result["containers"] and "rcu" not in result:
        _fail("config must contain at least one target")
    return result


def render_dropin(service):
    """Render a validated, self-contained systemd drop-in for one service."""
    service = _service(service)
    lines = [MARKER, "[Service]"]
    for key, value in service.get("go_env", {}).items():
        escaped = (key + "=" + value).replace("\\", "\\\\").replace('"', '\\"')
        lines.append('Environment="{}"'.format(escaped))
    if service.get("go_env"):
        lines.append("EnvironmentFile=" + environment_path(service))
    if "cpu_affinity" in service:
        lines.extend(("CPUAffinity=", "CPUAffinity=" + service["cpu_affinity"].replace(",", " ")))
    if "numa_policy" in service:
        lines.extend(("NUMAPolicy=" + service["numa_policy"], "NUMAMask="))
        if "numa_nodes" in service:
            lines.append("NUMAMask=" + service["numa_nodes"].replace(",", " "))
    return "\n".join(lines) + "\n"


def environment_path(service):
    """Return the explicit, per-unit path of the owned environment file."""
    service = _service(service)
    return "/etc/systemd/system/{}.d/zzzz-machconf-tune.env".format(service["unit"])


def render_environment(service):
    """Render safe assignments; an empty file clears a prior owned Go recipe."""
    service = _service(service)
    lines = [MARKER]
    lines.extend(key + "=" + value for key, value in service.get("go_env", {}).items())
    return "\n".join(lines) + "\n"
