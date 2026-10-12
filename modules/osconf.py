"""OS 配置检测: 内核版本/发行版、静态大页(含各 NUMA 节点分布)、透明大页 THP."""

from .base import Item, Module, first_group


def _fmt_kb(kib):
    """KiB 数值转人类可读 (kB/MB/GB/TB)."""
    if kib < 1024:
        return "%d kB" % kib
    mb = kib / 1024
    if mb < 1024:
        return "%.0f MB" % mb
    gb = mb / 1024
    if gb < 1024:
        return "%.0f GB" % gb
    return "%.1f TB" % (gb / 1024)


class OsModule(Module):
    name = "os"
    title = "OS 配置"
    group = "OS 配置"

    def probes(self):
        node_hp = (
            "for d in /sys/devices/system/node/node[0-9]*/hugepages/hugepages-*kB; do "
            "echo \"$(basename $(dirname $(dirname $d)))/$(basename $d)=$(cat $d/nr_hugepages 2>/dev/null)\"; done"
        )
        return [
            ("os_kernel", "uname -r 2>/dev/null || true"),
            ("os_release",
             "grep -m1 '^PRETTY_NAME' /etc/os-release 2>/dev/null | cut -d'\"' -f2 || true"),
            ("os_hp_meminfo",
             "grep -i '^HugePages_Total\\|^HugePages_Free\\|^Hugepagesize' /proc/meminfo || true"),
            ("os_hp_per_node", node_hp),
            ("os_thp",
             "cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null || true"),
        ]

    def parse(self, sections):
        items = []
        release = sections.get("os_release", "").strip()
        kernel = sections.get("os_kernel", "").strip()
        items.append(Item("发行版", release or "N.A."))
        items.append(Item("内核版本", kernel or "N.A."))

        meminfo = sections.get("os_hp_meminfo", "")
        total = first_group(r"HugePages_Total:\s+(\d+)", meminfo)
        free = first_group(r"HugePages_Free:\s+(\d+)", meminfo)
        size_kb = first_group(r"Hugepagesize:\s+(\d+)", meminfo)
        if total and size_kb and int(total) > 0:
            total_kb = int(total) * int(size_kb)
            free_kb = int(free) * int(size_kb) if free else 0
            items.append(Item(
                "静态大页 (HugeTLB)",
                "%d × %s = %s (空闲 %s)"
                % (int(total), _fmt_kb(int(size_kb)), _fmt_kb(total_kb), _fmt_kb(free_kb)),
            ))
        else:
            items.append(Item("静态大页 (HugeTLB)", "未配置"))

        per_node = [line.strip() for line in
                    sections.get("os_hp_per_node", "").splitlines()
                    if line.strip() and not line.strip().endswith("=0")]
        if per_node:
            items.append(Item("大页 NUMA 分布", "; ".join(per_node)))
        elif total and int(total) > 0:
            items.append(Item("大页 NUMA 分布", "N.A.", "sysfs 未导出各节点大页信息"))

        thp = first_group(r"\[(\w+)\]", sections.get("os_thp", ""))
        items.append(Item("透明大页 (THP)", thp or "N.A.",
                          "" if thp else "内核不支持 THP 或 sysfs 路径不可读"))
        return items
