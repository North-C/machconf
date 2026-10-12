"""BIOS 配置检测: 固件信息、CPU 拓扑(NUMA/核心分布, 反映 BIOS 相关设置)、
功耗 (governor/C-state/EPB)、超频/睿频 (x86: intel_pstate; ARM: 最大频率推断).

BIOS 菜单项无法从 OS 内直接读取 (需厂商带外工具), 本模块检测的是 BIOS 设置
在 OS 侧的可见结果; 不同机型/架构可用项不同, 不可用项以 note 说明.
"""

import re

from .base import Item, Module, first_group

# dmidecode 中常见的占位垃圾串
_JUNK_STRINGS = ("Not Specified", "To Be Filled By O.E.M.",
                 "To be filled by O.E.M.", "Default string", "System Product Name")


def _clean(text):
    """去掉 dmidecode 占位串并压缩空白."""
    for junk in _JUNK_STRINGS:
        text = text.replace(junk, " ")
    return " ".join(text.split())


def parse_bios_firmware(dmi_bios, dmi_system):
    """BIOS 固件与整机信息."""
    items = []
    vendor = first_group(r"^\s*Vendor:\s+(.+)$", dmi_bios, re.M)
    version = first_group(r"^\s*Version:\s+(.+)$", dmi_bios, re.M)
    date = first_group(r"^\s*Release Date:\s+(.+)$", dmi_bios, re.M)
    bios_rev = first_group(r"^\s*BIOS Revision:\s+(.+)$", dmi_bios, re.M)
    if vendor or version:
        firmware = " ".join(filter(None, [vendor, version]))
        if bios_rev and bios_rev not in firmware:
            firmware += " (Rev %s)" % bios_rev
        items.append(Item("BIOS 固件", firmware, "发布日期 %s" % date if date else ""))
    else:
        items.append(Item("BIOS 固件", "N.A.", "dmidecode 不可用 (需 root) 或平台未提供"))

    manufacturer = _clean(first_group(r"^\s*Manufacturer:\s+(.+)$", dmi_system, re.M))
    product = _clean(first_group(r"^\s*Product Name:\s+(.+)$", dmi_system, re.M))
    if manufacturer or product:
        items.append(Item("整机型号", " ".join(filter(None, [manufacturer, product]))))
    return items


def parse_topology(lscpu_text, smt_text):
    """CPU 拓扑: 插槽/每颗 CPU 核数/超线程/NUMA 节点及各节点 CPU 分布.

    这些是 BIOS 中 NUMA 配置与核心使能设置在 OS 侧的直接体现.
    """
    items = []
    # ARM 平台 lscpu 的 "Model name" 可能为 "-", 回退用 "BIOS Model name"
    model = first_group(r"^Model name:\s+(.+)$", lscpu_text, re.M)
    if model in ("", "-", "N.A."):
        model = first_group(r"^BIOS Model name:\s+(.+)$", lscpu_text, re.M)
    model = _clean(model)
    sockets = first_group(r"^Socket\(s\):\s+(\d+)", lscpu_text, re.M)
    cores = first_group(r"^Core\(s\) per socket:\s+(\d+)", lscpu_text, re.M)
    threads = first_group(r"^Thread\(s\) per core:\s+(\d+)", lscpu_text, re.M)
    cpus = first_group(r"^CPU\(s\):\s+(\d+)", lscpu_text, re.M)
    nodes = first_group(r"^NUMA node\(s\):\s+(\d+)", lscpu_text, re.M)

    if model:
        items.append(Item("CPU 型号", model))
    if all([sockets, cores, threads, cpus]):
        smt_note = ""
        if smt_text:
            smt_note = "SMT %s (sysfs)" % (
                "开启" if smt_text == "on" else smt_text)
        items.append(Item("CPU 核心", "%s 路 × %s 核 × %s 线程 = %s 逻辑 CPU"
                          % (sockets, cores, threads, cpus), smt_note))
    elif cpus:
        items.append(Item("CPU 核心", "%s 逻辑 CPU" % cpus, "拓扑详情不全"))

    node_cpus = re.findall(r"^NUMA node(\d+) CPU\(s\):\s+([\d,\-]+)", lscpu_text, re.M)
    if nodes:
        dist = "; ".join("node%s: CPU %s" % (n, c) for n, c in node_cpus)
        items.append(Item("NUMA 节点分布", "%s 个 NUMA 节点" % nodes,
                          dist if dist else "各节点 CPU 范围未导出"))
    else:
        items.append(Item("NUMA 节点分布", "N.A.",
                          "内核未启用 NUMA 或单节点 (numa=off/单socket)"))
    return items


def parse_power(governor, driver, min_freq, max_freq, cstates, epb):
    """功耗相关: 调频策略、C-state、能效偏好."""
    items = []
    if governor:
        items.append(Item("CPU 调频策略 (governor)", governor,
                          "驱动: %s" % driver if driver else ""))
    else:
        items.append(Item("CPU 调频策略 (governor)", "N.A.", "cpufreq 未启用 (BIOS 关闭调频或平台不支持)"))
    freq_note = ""
    if min_freq and max_freq:
        freq_note = "频率范围 %.0f ~ %.0f MHz" % (
            int(min_freq) / 1000, int(max_freq) / 1000)
    if freq_note:
        items[-1].note = (items[-1].note + "; " if items[-1].note else "") + freq_note

    state_names = [s for s in cstates.split() if s]
    if state_names:
        items.append(Item("C-state (空闲态)", " ".join(state_names)))
    else:
        items.append(Item("C-state (空闲态)", "N.A.",
                          "cpuidle 未导出 (ARM 平台常见, 由固件管理)"))
    if epb:
        epb_map = {"0": "performance (0)", "6": "normal (6)", "15": "powersave (15)"}
        items.append(Item("能效偏好 (EPB)", epb_map.get(epb.strip(), epb.strip())))
    return items


def parse_turbo(model_text, no_turbo, max_freq, lscpu_text):
    """超频/睿频检测: x86 走 intel_pstate; ARM 按最大频率与基础频率对比推断."""
    items = []
    no_turbo = no_turbo.strip()
    if no_turbo:
        turbo_on = no_turbo == "0"
        items.append(Item("睿频/超频 (Turbo)", "开启" if turbo_on else "关闭",
                          "intel_pstate/no_turbo=%s" % no_turbo))
        return items
    # ARM/其他平台: 用最大频率对比型号中的基础频率推断 (GHz -> MHz)
    base_ghz = first_group(r"@\s*([\d.]+)\s*GHz", model_text or "")
    if max_freq and base_ghz:
        max_mhz = int(max_freq) / 1000.0
        base_mhz = float(base_ghz) * 1000
        if max_mhz > base_mhz * 1.02:
            items.append(Item("睿频/超频 (Turbo)", "开启 (推断)",
                              "最大频率 %.0f MHz > 基础频率 %.0f MHz"
                              % (max_mhz, base_mhz)))
        else:
            items.append(Item("睿频/超频 (Turbo)", "未检测到睿频 (推断)",
                              "最大频率 %.0f MHz ≈ 基础频率 %.0f MHz"
                              % (max_mhz, base_mhz)))
    else:
        items.append(Item("睿频/超频 (Turbo)", "N.A.",
                          "平台未提供 turbo 状态接口 (ARM/无 intel_pstate)"))
    return items


class BiosModule(Module):
    name = "bios"
    title = "BIOS 配置"
    group = "BIOS 配置"

    def probes(self):
        cstates = ("for f in /sys/devices/system/cpu/cpu0/cpuidle/state*/name; do "
                   "cat $f 2>/dev/null; done")
        return [
            ("bios_dmi_bios",
             "dmidecode -t bios 2>/dev/null | grep -E '^[[:space:]]+(Vendor|Version|Release Date|BIOS Revision):' || true"),
            ("bios_dmi_system",
             "dmidecode -t system 2>/dev/null | grep -E '^[[:space:]]+(Manufacturer|Product Name):' || true"),
            ("bios_lscpu", "lscpu 2>/dev/null || true"),
            ("bios_smt", "cat /sys/devices/system/cpu/smt/control 2>/dev/null || true"),
            ("bios_governor",
             "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || true"),
            ("bios_freq_driver",
             "cat /sys/devices/system/cpu/cpufreq/policy0/scaling_driver 2>/dev/null || true"),
            ("bios_min_freq",
             "cat /sys/devices/system/cpu/cpufreq/policy0/scaling_min_freq 2>/dev/null || true"),
            ("bios_max_freq",
             "cat /sys/devices/system/cpu/cpufreq/policy0/scaling_max_freq 2>/dev/null || true"),
            ("bios_no_turbo",
             "cat /sys/devices/system/cpu/intel_pstate/no_turbo 2>/dev/null || true"),
            ("bios_epb",
             "cat /sys/devices/system/cpu/cpu0/power/energy_perf_bias 2>/dev/null || true"),
            ("bios_cstates", cstates),
        ]

    def parse(self, sections):
        lscpu = sections.get("bios_lscpu", "")
        model = first_group(r"^Model name:\s+(.+)$", lscpu, re.M)
        if model in ("", "-"):
            model = first_group(r"^BIOS Model name:\s+(.+)$", lscpu, re.M)
        return (
            parse_bios_firmware(sections.get("bios_dmi_bios", ""),
                                sections.get("bios_dmi_system", ""))
            + parse_topology(lscpu, sections.get("bios_smt", "").strip())
            + parse_power(sections.get("bios_governor", "").strip(),
                          sections.get("bios_freq_driver", "").strip(),
                          sections.get("bios_min_freq", "").strip(),
                          sections.get("bios_max_freq", "").strip(),
                          sections.get("bios_cstates", ""),
                          sections.get("bios_epb", ""))
            + parse_turbo(model,
                          sections.get("bios_no_turbo", ""),
                          sections.get("bios_max_freq", "").strip(),
                          lscpu)
        )
