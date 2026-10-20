"""Read-only RCU policy evidence; boot arguments are not runtime state."""

import re
import shlex

from .base import Item, Module


RCU_BOOT_KEYS = (
    "rcupdate.rcu_expedited",
    "rcupdate.rcu_normal",
    "rcupdate.rcu_normal_after_boot",
)
RCU_CONFIG_KEYS = ("CONFIG_TREE_RCU", "CONFIG_TINY_RCU", "CONFIG_PREEMPT_RT")
_PREFIX = "MACHCONF_STATUS="
_RUNTIME_NOTE = (
    "sysfs 读取快照；normal 优先于 expedited；不证明具体调用、耗时或性能收益"
)


def read_file_probe(path):
    """Report missing, unreadable and failed reads without defaulting to zero."""
    quoted = shlex.quote(path)
    return """if [ ! -e {path} ]; then
  printf '%s\\n' 'MACHCONF_STATUS=missing'
elif [ ! -r {path} ]; then
  printf '%s\\n' 'MACHCONF_STATUS=permission_denied'
elif machconf_rcu_out=$(cat {path} 2>&1); then
  printf '%s\\n%s\\n' 'MACHCONF_STATUS=ok' "$machconf_rcu_out"
else
  printf '%s\\n%s\\n' 'MACHCONF_STATUS=read_error' "$machconf_rcu_out"
fi""".format(path=quoted)


def _section(sections, tag):
    lines = sections.get(tag, "").splitlines()
    if not lines or not lines[0].startswith(_PREFIX):
        return "unavailable", "未收到有效探测结果"
    status = lines[0][len(_PREFIX):]
    content = "\n".join(lines[1:])
    reasons = {
        "missing": "文件不存在；当前内核可能未提供该接口",
        "permission_denied": "权限不足，无法读取",
        "read_error": "读取失败",
    }
    if status == "ok":
        return status, content
    if status in reasons:
        detail = " ".join(content.splitlines())[:400]
        return status, reasons[status] + (": " + detail if detail else "")
    return "unavailable", "未知探测状态"


def parse_flag(sections, tag):
    status, value = _section(sections, tag)
    if status != "ok":
        return status, None, value
    value = value.strip()
    if value not in ("0", "1"):
        # Do not classify empty, multiple lines or an unexpected integer as 0.
        return "invalid", None, "不可用：预期 0/1，实际为 %r" % value[:100]
    return "ok", int(value), ""


def argument_values(text):
    values = {key: [] for key in RCU_BOOT_KEYS}
    for token in shlex.split(text):
        key, separator, value = token.partition("=")
        if key in values:
            values[key].append(value if separator else "<无等号>")
    return values


def _boot_items(text):
    note = "当前 /proc/cmdline；仅启动参数证据，不代表当前 sysfs 值或内核已支持；重复项不推断生效顺序"
    try:
        values = argument_values(text)
    except ValueError:
        return [Item("RCU 当前 cmdline", "unparseable", "引号不完整；无法可靠解析")]
    items = []
    for key, observed in values.items():
        shown = [value if value else "<空值>" for value in observed]
        if not observed:
            value = "absent (未设置)"
        elif len(set(observed)) > 1:
            value = "conflict: " + ", ".join(shown)
        elif len(observed) > 1:
            value = "duplicate: " + ", ".join(shown)
        else:
            value = shown[0]
        items.append(Item("当前 cmdline " + key, value, note))
    return items


def _config_probe():
    # /proc/config.gz is the running kernel's embedded config. A matching-name
    # /boot file is weaker evidence and is labelled accordingly.
    return """if [ -e /proc/config.gz ]; then
  machconf_rcu_source=/proc/config.gz
  if [ ! -r /proc/config.gz ]; then
    printf '%s\\n' 'MACHCONF_STATUS=permission_denied'
    exit_rcu_config=1
  elif machconf_rcu_config=$(gzip -cd /proc/config.gz 2>&1); then
    exit_rcu_config=0
  else
    printf '%s\\n' 'MACHCONF_STATUS=read_error'
    exit_rcu_config=1
  fi
else
  machconf_rcu_source="/boot/config-$(uname -r)"
  if [ ! -e "$machconf_rcu_source" ]; then
    printf '%s\\n' 'MACHCONF_STATUS=missing'
    exit_rcu_config=1
  elif [ ! -r "$machconf_rcu_source" ]; then
    printf '%s\\n' 'MACHCONF_STATUS=permission_denied'
    exit_rcu_config=1
  elif machconf_rcu_config=$(cat "$machconf_rcu_source" 2>&1); then
    exit_rcu_config=0
  else
    printf '%s\\n' 'MACHCONF_STATUS=read_error'
    exit_rcu_config=1
  fi
fi
if [ "$exit_rcu_config" = 0 ]; then
  printf '%s\\n%s\\n' 'MACHCONF_STATUS=ok' "source=$machconf_rcu_source"
  printf '%s\\n' "$machconf_rcu_config" |
    grep -E '^(CONFIG_(TREE_RCU|TINY_RCU|PREEMPT_RT)=|# CONFIG_(TREE_RCU|TINY_RCU|PREEMPT_RT) is not set$)' || true
fi"""


def _config_items(sections):
    status, text = _section(sections, "rcu_kernel_config")
    if status != "ok":
        return [Item("RCU 内核配置", status, text)]
    source_lines = re.findall(r"^source=(.+)$", text, re.M)
    source = source_lines[0] if len(source_lines) == 1 else "unknown"
    note = ("运行内核内嵌配置" if source == "/proc/config.gz"
            else "按 uname -r 匹配的磁盘配置；不保证与运行内核一致")
    items = [Item("RCU 内核配置来源", source, note)]
    for key in RCU_CONFIG_KEYS:
        values = re.findall(r"^" + key + r"=(.*)$", text, re.M)
        values += ["n"] * len(re.findall(r"^# " + key + r" is not set$", text, re.M))
        if len(values) > 1:
            value = "conflict/duplicate: " + ", ".join(values)
        elif values and values[0] in ("y", "n"):
            value = values[0]
        elif values:
            value = "invalid: " + values[0]
        else:
            value = "unknown (未见配置项)"
        explanation = note
        if key == "CONFIG_TINY_RCU":
            explanation += "；TINY_RCU 下这些宽限期强制策略无效"
        if key == "CONFIG_PREEMPT_RT":
            explanation += "；RT 内核的启动后 normal 策略需结合运行内核版本与 sysfs 观察"
        items.append(Item(key, value, explanation))
    return items


class RcuModule(Module):
    name = "rcu"
    title = "RCU 宽限期策略"
    group = "OS 配置"

    def probes(self):
        return [
            ("rcu_expedited", read_file_probe("/sys/kernel/rcu_expedited")),
            ("rcu_normal", read_file_probe("/sys/kernel/rcu_normal")),
            ("rcu_normal_after_boot", read_file_probe(
                "/sys/module/rcupdate/parameters/rcu_normal_after_boot")),
            ("rcu_cmdline", read_file_probe("/proc/cmdline")),
            ("rcu_kernel_config", _config_probe()),
        ]

    def parse(self, sections):
        items = []
        flags = {}
        for key in ("rcu_expedited", "rcu_normal", "rcu_normal_after_boot"):
            status, value, reason = parse_flag(sections, key)
            flags[key] = value if status == "ok" else None
            note = _RUNTIME_NOTE if key != "rcu_normal_after_boot" else (
                "/sys/module/rcupdate/parameters；启动后 normal 策略参数，不能替代两项运行时开关")
            items.append(Item(key, str(value) if status == "ok" else status,
                              note + ("；" + reason if reason else "")))
        expedited, normal = flags["rcu_expedited"], flags["rcu_normal"]
        if expedited is None or normal is None:
            mode = "unavailable"
            detail = "两项运行时开关未完整读取；不推断组合策略"
        elif normal == 1:
            mode = "normal"
            detail = "强制 normal 宽限期；normal=1 优先于 expedited=%d" % expedited
        elif expedited == 1:
            mode = "expedited"
            detail = "强制 expedited 宽限期"
        else:
            mode = "unforced (未强制)"
            detail = "两项均为 0：未由这两个开关强制 normal/expedited；不是关闭 RCU，调用方仍可请求 expedited"
        items.append(Item("RCU 运行时组合策略", mode,
                          detail + "；" + _RUNTIME_NOTE + "；TINY_RCU 下不适用"))
        status, cmdline = _section(sections, "rcu_cmdline")
        items.extend(_boot_items(cmdline) if status == "ok"
                     else [Item("RCU 当前 cmdline", status, cmdline)])
        items.extend(_config_items(sections))
        return items
