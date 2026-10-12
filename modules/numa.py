"""NUMA Balancing 配置检测 (查询逻辑与 numa_balancing.sh 保持一致)."""

from .base import Item, Module, first_group


class NumaModule(Module):
    name = "numa"
    title = "NUMA Balancing"
    group = "OS 配置"

    def probes(self):
        return [
            ("numa_value",
             "cat /proc/sys/kernel/numa_balancing 2>/dev/null || true"),
            ("numa_cmdline",
             "grep -o 'numa_balancing=[a-z]*' /proc/cmdline 2>/dev/null || true"),
            ("numa_persist",
             "grep -h '^[[:space:]]*kernel.numa_balancing'"
             " /etc/sysctl.conf /etc/sysctl.d/*.conf 2>/dev/null || true"),
        ]

    def parse(self, sections):
        items = []
        raw = first_group(r"^\s*(\d+)\s*$", sections.get("numa_value", ""))
        if raw:
            desc = {
                "1": "内核自动在 NUMA 节点间迁移内存页",
                "0": "内核不做自动 NUMA 迁移",
            }.get(raw, "未知值")
            items.append(Item("numa_balancing 开关", "%s (%s)" % (raw, desc)))
        else:
            items.append(Item("numa_balancing 开关", "N.A.",
                              "内核不支持或未启用 NUMA (/proc/sys/kernel/numa_balancing 不可读)"))

        cmdline = sections.get("numa_cmdline", "").strip()
        if cmdline:
            items.append(Item("内核启动参数", cmdline,
                              "优先级高于 sysctl, 重启后以其为准"))
        persist = sections.get("numa_persist", "").strip().splitlines()
        if persist:
            items.append(Item("持久化配置", "; ".join(line.strip() for line in persist),
                              "位于 sysctl.conf / sysctl.d, 重启后生效"))
        return items
