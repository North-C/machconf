"""Read-only ARM KVM boot arguments and kernel-log evidence.

Arguments in /proc/cmdline are observations, not proof that a kernel implements
or accepted them. GRUB defaults and grubby's default entry describe different
configuration sources; neither is assumed to be the currently running kernel.
"""

import re
import shlex

from .base import Item, Module

KVM_ARM_KEYS = tuple("kvm-arm." + key for key in (
    "vgic_v4_enable", "virt_msi_bypass", "dvmbm_enabled", "ipiv_enabled"))
GRUB_VARIABLES = ("GRUB_CMDLINE_LINUX", "GRUB_CMDLINE_LINUX_DEFAULT")
_OK = "MACHCONF_STATUS=ok"
_ERROR = "MACHCONF_STATUS=error"


def _read_probe(command):
    return ("if machconf_out=$(%s 2>&1); then\n"
            "  printf '%%s\\n%%s\\n' '%s' \"$machconf_out\"\n"
            "else\n  printf '%%s\\n%%s\\n' '%s' \"$machconf_out\"\nfi"
            % (command, _OK, _ERROR))


def _section(sections, tag):
    lines = sections.get(tag, "").splitlines()
    if not lines or lines[0] not in (_OK, _ERROR):
        return "unavailable", "未收到有效探测结果"
    if lines[0] == _ERROR:
        return "unavailable", " ".join(lines[1:])[:400] or "命令执行失败"
    return "ok", "\n".join(lines[1:])


def _literal(raw):
    """Read one shell word, conservatively rejecting expansion/compound syntax."""
    raw = raw.strip()
    # Single quotes make all characters literal; double quotes allow expansion.
    match = re.fullmatch(r"'([^']*)'\s*(?:#.*)?", raw)
    if match:
        return match.group(1)
    match = re.fullmatch(r'"([^"\\$`\n]*)"\s*(?:#.*)?', raw)
    if match:
        return match.group(1)
    match = re.fullmatch(r"([A-Za-z0-9_./=:,+@%\-]*)\s*(?:#.*)?", raw)
    if match:
        return match.group(1)
    raise ValueError("动态表达式或复杂 shell 语法；不执行求值")


def parse_grub_assignments(text):
    """Extract only unambiguous one-line literal assignments; never source a file."""
    # Conditional blocks, sourcing, and function bodies can change whether an
    # apparently simple assignment runs. Do not claim a resolved literal there.
    control_flow = any(
        re.match(r"\s*(?:if|then|elif|else|fi|case|esac|for|while|until|do|done|"
                 r"source|eval|function)\b|\s*\.\s|\s*[A-Za-z_]\w*\s*\(\s*\)", line)
        for line in text.splitlines() if not line.lstrip().startswith("#"))
    result = {}
    for name in GRUB_VARIABLES:
        candidates = []
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            if re.search(r"\b" + name + r"\s*\+?=", line):
                candidates.append(line)
        if not candidates:
            result[name] = {"status": "absent", "value": "", "reason": "未见赋值"}
            continue
        if control_flow:
            result[name] = {"status": "unparseable", "value": "",
                            "reason": "文件含条件/来源/函数等 shell 语法；不能确定赋值是否生效"}
            continue
        if len(candidates) != 1:
            result[name] = {"status": "unparseable", "value": "",
                            "reason": "发现多重赋值；不推断最终值"}
            continue
        match = re.fullmatch(r"\s*(?:export\s+)?" + name + r"=(.*)", candidates[0])
        try:
            if not match:
                raise ValueError("不是独立的简单赋值")
            value = _literal(match.group(1))
        except ValueError as exc:
            result[name] = {"status": "unparseable", "value": "", "reason": str(exc)}
        else:
            result[name] = {"status": "ok", "value": value, "reason": "静态字面量"}
    return result


def argument_values(text):
    """Keep all occurrences and values of exact argument names, including bare names."""
    tokens = shlex.split(text)
    result = {key: [] for key in KVM_ARM_KEYS}
    for token in tokens:
        name, sep, value = token.partition("=")
        if name in result:
            result[name].append(value if sep else "<无等号>")
    return result


def _argument_items(prefix, text, note):
    try:
        values = argument_values(text)
    except ValueError:
        return [Item(prefix, "unparseable", "引号不完整；不能可靠解析参数")]
    items = []
    for key in KVM_ARM_KEYS:
        observed = values[key]
        if not observed:
            status = "absent (未设置)"
        elif len(set(observed)) > 1:
            status = "conflict: " + ", ".join(observed)
        elif len(observed) > 1:
            status = "duplicate: " + ", ".join(observed)
        else:
            status = observed[0] if observed[0] else "<空值>"
        items.append(Item(prefix + " " + key, status, note + "；未设置不等于 disabled；重复项不推断有效顺序"))
    return items


_KVM_PREFIX = re.compile(r"\bkvm(?:\s*\[\s*\d+\s*\])?\s*:", re.I)
_FEATURES = {
    "GICv4": re.compile(r"\bGICv4(?:\.\d+)?\b", re.I),
    "dvmbm": re.compile(r"\bdvmbm(?:_enabled)?\b", re.I),
    "ipiv": re.compile(r"\bipiv(?:_enabled)?\b", re.I),
    "virt_msi_bypass": re.compile(r"\b(?:virt(?:ual)?[ _-]msi[ _-]bypass)\b", re.I),
}


def log_evidence(text):
    """Classify explicit KVM status lines separately from interrupt-controller logs."""
    records = {name: {"status": "not_observed", "evidence": []} for name in _FEATURES}
    controller = []
    for line in text.splitlines():
        if not _KVM_PREFIX.search(line):
            if _FEATURES["GICv4"].search(line):
                controller.append(line.strip())
            continue
        for name, pattern in _FEATURES.items():
            match = pattern.search(line)
            if match:
                records[name]["evidence"].append(line.strip())
    for name, record in records.items():
        states = set()
        for line in record["evidence"]:
            # A single line may mention several features with different states.
            # Bind state words only to the clause following this feature name.
            match = _FEATURES[name].search(line)
            clause = line[match.end():]
            stops = [m.start() for pattern in _FEATURES.values()
                     for m in [pattern.search(clause)] if m]
            if stops:
                clause = clause[:min(stops)]
            states.update(re.findall(r"\b(enabled|disabled)\b", clause.lower()))
        if len(states) > 1:
            record["status"] = "conflict"
        elif states:
            record["status"] = states.pop()
        elif record["evidence"]:
            record["status"] = "unknown"
    return records, controller


def _default_entry_items(default_kernel, info):
    """grubby --info=DEFAULT must describe exactly one identifiable boot entry."""
    kernels = re.findall(r"^kernel=(.*)$", info, re.M)
    args = re.findall(r"^args=(.*)$", info, re.M)
    indexes = re.findall(r"^index=(.*)$", info, re.M)
    if len(kernels) != 1 or len(args) != 1 or len(indexes) != 1:
        return [Item("默认启动项", "unparseable", "grubby 未返回唯一完整条目；不推断下次启动参数")]
    try:
        kernel, arguments = _literal(kernels[0]), _literal(args[0])
    except ValueError as exc:
        return [Item("默认启动项", "unparseable", str(exc))]
    if kernel != default_kernel.strip():
        return [Item("默认启动项", "conflict", "grubby 两次读取的默认内核不一致；可能配置并发变化")]
    note = "grubby 默认启动项；不代表当前内核；不保证固件/一次性引导选择"
    return ([Item("默认启动项 kernel", kernel, note)]
            + _argument_items("默认启动项", arguments, note))


class KvmArmModule(Module):
    name = "kvm-arm"
    title = "ARM KVM 中断加速启动配置"
    group = "OS 配置"

    def probes(self):
        return [
            ("kvm_arm_arch", _read_probe("uname -m")),
            ("kvm_arm_cmdline", _read_probe("cat /proc/cmdline")),
            ("kvm_arm_grub", _read_probe("cat /etc/default/grub")),
            ("kvm_arm_default_kernel", _read_probe("grubby --default-kernel")),
            ("kvm_arm_default_entry", _read_probe("grubby --info=DEFAULT")),
            ("kvm_arm_dmesg", """if machconf_out=$(dmesg 2>&1); then
  printf '%s\\n' 'MACHCONF_STATUS=ok'
  printf '%s\\n' "$machconf_out" | grep -Ei 'gicv4|dvmbm|ipiv|virt[ _-]?(ual[ _-]?)?msi[ _-]?bypass' || true
else
  printf '%s\\n%s\\n' 'MACHCONF_STATUS=error' "$machconf_out"
fi"""),
        ]

    def parse(self, sections):
        status, arch = _section(sections, "kvm_arm_arch")
        items = [Item("架构", arch if status == "ok" else status,
                      "" if status == "ok" else arch)]
        if status != "ok":
            return items + [Item("ARM KVM 检测", "unavailable", "无法确定架构")]
        if not re.fullmatch(r"(?:aarch64|arm64|armv\d+\w*)", arch.strip(), re.I):
            return items + [Item("ARM KVM 检测", "not_applicable", "非 ARM 架构")]
        items.append(Item("检测口径", "启动参数 / GRUB 源文件 / 默认启动项 / KVM 日志分别列示",
                          "参数存在不证明内核支持或已经启用；日志可能受权限、环形缓冲覆盖影响"))
        status, cmdline = _section(sections, "kvm_arm_cmdline")
        if status == "ok":
            items.extend(_argument_items("当前 cmdline", cmdline, "/proc/cmdline，当前启动参数"))
        else:
            items.append(Item("当前 cmdline", status, cmdline))
        status, grub = _section(sections, "kvm_arm_grub")
        if status == "ok":
            for name, record in parse_grub_assignments(grub).items():
                items.append(Item("GRUB " + name, record["status"], record["reason"]
                                  + "；/etc/default/grub 源文件，不执行 source；不代表已生成的启动项"))
                if record["status"] == "ok":
                    items.extend(_argument_items(name, record["value"], "源文件静态字面量摘录"))
        else:
            items.append(Item("GRUB 源文件", status, grub))
        kernel_status, kernel = _section(sections, "kvm_arm_default_kernel")
        entry_status, entry = _section(sections, "kvm_arm_default_entry")
        if kernel_status == "ok" and entry_status == "ok":
            items.extend(_default_entry_items(kernel, entry))
        else:
            reasons = []
            if kernel_status != "ok":
                reasons.append("--default-kernel: " + kernel)
            if entry_status != "ok":
                reasons.append("--info=DEFAULT: " + entry)
            items.append(Item("默认启动项", "unavailable", "; ".join(reasons)))
        status, logs = _section(sections, "kvm_arm_dmesg")
        items.append(Item("dmesg 读取", status, "" if status == "ok" else logs))
        if status == "ok":
            evidence, controller = log_evidence(logs)
            for name, record in evidence.items():
                items.append(Item("KVM " + name, record["status"],
                                  "; ".join(record["evidence"]) or "未命中明确 KVM 日志，不能推断启用/禁用"))
            items.append(Item("GICv4 控制器日志", "observed" if controller else "not_observed",
                              "; ".join(controller) or "未观测到；与 KVM 层分开判读"))
        else:
            for name in _FEATURES:
                items.append(Item("KVM " + name, "unavailable", "dmesg 不可读；不能判定状态"))
        return items
