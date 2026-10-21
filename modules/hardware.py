"""硬件配置检测: 内存 (容量/通道/类型/频率)、磁盘 (大小/类型/角色: 系统盘/数据盘)."""

import json
import re

from .base import Item, Module, first_group
from .disk_inventory import DISK_SYSFS_PROBE, disk_inventory, read_disks


# Read Identify Controller via each namespace (also works for multipath names).
# Bound each command and the total budget so a stalled disk cannot block all probes.
NVME_VERSION_PROBE = r'''
nvme_probe_start=$SECONDS
for nvme_path in /sys/block/nvme*n*; do
    [ -e "$nvme_path" ] || continue
    nvme_disk=${nvme_path##*/}
    [[ "$nvme_disk" =~ ^nvme[0-9]+(c[0-9]+)?n[0-9]+$ ]] || continue
    if ! command -v nvme >/dev/null 2>&1; then
        printf '%s|missing_nvme|\n' "$nvme_disk"
        continue
    fi
    if ! command -v timeout >/dev/null 2>&1; then
        printf '%s|missing_timeout|\n' "$nvme_disk"
        continue
    fi
    nvme_remaining=$((10 - SECONDS + nvme_probe_start))
    if [ "$nvme_remaining" -le 0 ]; then
        printf '%s|budget_exhausted|\n' "$nvme_disk"
        continue
    fi
    [ "$nvme_remaining" -le 3 ] || nvme_remaining=3
    nvme_output=$(LC_ALL=C timeout -k 1s "${nvme_remaining}s" nvme id-ctrl "/dev/$nvme_disk" -o json 2>/dev/null)
    nvme_rc=$?
    if [ "$nvme_rc" -eq 0 ]; then
        printf '%s|ok|' "$nvme_disk"
        printf '%s' "$nvme_output" | tr '\n\r' '  '
        printf '\n'
    elif [ "$nvme_rc" -eq 124 ] || [ "$nvme_rc" -eq 137 ]; then
        printf '%s|timeout|\n' "$nvme_disk"
    else
        printf '%s|read_error|%s\n' "$nvme_disk" "$nvme_rc"
    fi
done
'''


def parse_nvme_versions(text, disk_items=()):
    """Decode the controller's 32-bit VER; never infer it from firmware or PCIe."""
    name_pattern = r"nvme[0-9]+(?:c[0-9]+)?n[0-9]+"
    records = {}
    for line in text.splitlines():
        fields = line.split("|", 2)
        if len(fields) == 3 and re.fullmatch(name_pattern, fields[0]):
            records[fields[0]] = fields[1:]
    for item in disk_items:
        match = re.fullmatch(r"磁盘 (" + name_pattern + r")", item.key)
        if match:
            records.setdefault(match[1], ("not_observed", ""))
    reasons = {
        "missing_nvme": "未安装 nvme-cli（nvme 命令不可用）",
        "missing_timeout": "timeout 命令不可用，跳过未受超时保护的查询",
        "timeout": "Identify Controller 查询超时",
        "budget_exhausted": "NVMe 查询总时间预算耗尽，未查询此设备",
        "read_error": "Identify Controller 读取失败（权限、设备状态或命令支持问题）",
        "not_observed": "未采集到该盘的 NVMe 版本探测结果",
    }
    items = []
    for name, (status, payload) in sorted(records.items()):
        value = "N.A."
        source = "nvme id-ctrl /dev/%s -o json; Identify Controller VER" % name
        if status == "ok":
            try:
                data = json.loads(payload)
                version = data.get("ver") if isinstance(data, dict) else None
                if isinstance(version, str) and re.fullmatch(r"0[xX][0-9a-fA-F]+|[0-9]+", version):
                    version = int(version, 16 if version.lower().startswith("0x") else 10)
                if type(version) is not int or not 0x10000 <= version < 0xffffffff:
                    raise ValueError("invalid VER")
                value = "%d.%d" % (version >> 16, (version >> 8) & 0xff)
                if version & 0xff:
                    value += ".%d" % (version & 0xff)
                note = "%s=0x%08x（控制器报告的协议版本）" % (source, version)
            except (ValueError, TypeError):
                note = source + "; JSON/VER 缺失、为 0 或无效，无法判定协议版本"
        else:
            note = source + "; " + reasons.get(status, "未知探测状态")
            if status == "read_error" and payload.isdigit():
                note += "; exit=" + payload
        items.append(Item("磁盘 %s NVMe 协议版本" % name, value, note))
    return items


def _fmt_size(size_str):
    """lsblk 的 SIZE (如 3.5T/1.7T/492G) 统一为人类可读."""
    return size_str.strip()


def _classify_disk(rota, tran):
    """按 ROTA/TRAN 判别磁盘类型."""
    if "nvme" in tran:
        return "NVMe SSD"
    if rota == "1":
        return "HDD (机械盘)"
    if rota == "0":
        if tran in ("sata", "sas", "ata"):
            return "SATA/SAS SSD"
        return "非旋转设备 (%s)" % (tran or "接口未知")
    return tran or "未知"


def _summarize_mounts(paths, limit=4):
    """挂载点去重 + 截断展示, 避免 kubelet 等大量子路径挂载刷屏."""
    seen = []
    for p in paths:
        if p not in seen:
            seen.append(p)
    if len(seen) <= limit:
        return ", ".join(seen)
    return "%s 等 %d 个挂载点" % (", ".join(seen[:limit]), len(seen))


def _disk_role(paths):
    """按挂载点判别磁盘角色: 系统盘/数据盘/未挂载.

    paths 为该盘 (含 LVM 子设备) 的所有挂载点; lsblk 会把 [SWAP] 表示为 '[SWAP]'.
    """
    if not paths:
        return "未挂载", ""
    if any(p == "/" or p.startswith("/boot") or p == "[SWAP]" for p in paths):
        return "系统盘", _summarize_mounts(paths)
    return "数据盘", _summarize_mounts(paths)


def parse_memory(dmi_text, meminfo_text):
    """解析 dmidecode -t memory 与 /proc/meminfo, 生成内存条目."""
    items = []
    dimms = re.findall(r"^\s*Size:\s+(\d+)\s+(GB|MB|TB)\s*$", dmi_text, re.M)
    if dimms:
        specs = [(int(n), u) for n, u in dimms]
        unit_gb = {"MB": 1 / 1024, "GB": 1, "TB": 1024}
        total_gb = sum(n * unit_gb[u] for n, u in specs)
        sizes = sorted({"%d %s" % (n, u) for n, u in specs})
        items.append(Item("内存容量", "%d × %s = %d GB (%.0f GiB)"
                          % (len(specs), " + ".join(sizes),
                             round(total_gb), total_gb / 1.073741824)))
    else:
        mem_total = first_group(r"MemTotal:\s+(\d+) kB", meminfo_text)
        if mem_total:
            items.append(Item("内存容量", "%s (dmidecode 不可用, 取自 /proc/meminfo)"
                              % ("%.0f GB" % (int(mem_total) / 1024 / 1024 * 1.073741824))))
        else:
            items.append(Item("内存容量", "N.A."))
    dimm_type = first_group(r"^\s*Type:\s+(\S+)", dmi_text, re.M)
    if dimm_type:
        items.append(Item("内存类型", dimm_type))
    conf_speed = first_group(r"^\s*Configured Memory Speed:\s+(\S+ \S+)", dmi_text, re.M)
    rated_speed = first_group(r"^\s*Speed:\s+(\S+ \S+)", dmi_text, re.M)
    speed = conf_speed or rated_speed
    if speed:
        note = "" if conf_speed else "仅读取到标称频率 (未配置或 dmidecode 信息不全)"
        if conf_speed and rated_speed and conf_speed != rated_speed:
            note = "标称 %s" % rated_speed
        items.append(Item("内存频率", speed, note))
    return items


def parse_mounts(mounts_text):
    """解析逐盘挂载探测输出 'nvme0n1=/ /boot' -> {'nvme0n1': ['/', '/boot']}."""
    mounts = {}
    for line in mounts_text.splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        name, _, paths = line.partition("=")
        mounts[name] = paths.split()
    return mounts


def parse_disks(lsblk_text, mounts_text):
    """解析 lsblk -d 与逐盘挂载输出, 生成磁盘条目."""
    disk_mounts = parse_mounts(mounts_text)

    items = []
    disks = read_disks(lsblk_text)

    if not disks:
        return [Item("磁盘", "N.A.", "lsblk 未返回任何物理盘")]

    for disk in disks:
        name, size, rota, tran, model = (disk[k] for k in ("name", "size", "rota", "tran", "model"))
        role, mount_note = _disk_role(disk_mounts.get(name, []))
        disk_type = _classify_disk(rota, tran)
        items.append(Item(
            "磁盘 %s" % name,
            "%s | %s | %s%s" % (_fmt_size(size), disk_type,
                                role, (" | " + model) if model else ""),
            mount_note,
        ))
        items.append(Item(
            "磁盘 %s Disk Type" % name, disk_type,
            "依据 lsblk ROTA=%s, TRAN=%s 判别；RAID/虚拟设备可能隐藏底层介质类型"
            % (rota, tran),
        ))

    data = [it for it in items if "| 数据盘" in it.value]
    unmounted = [it for it in items if "| 未挂载" in it.value]
    summary_note = "; ".join(re.sub(r"^.*?\| ", "", it.value) for it in data)
    if unmounted:
        spare = "另有 %d 块未挂载: %s" % (
            len(unmounted), ", ".join(it.key.replace("磁盘 ", "") for it in unmounted))
        summary_note = (summary_note + "; " if summary_note else "") + spare
    if data:
        items.append(Item("数据盘汇总", "%d 块" % len(data), summary_note))
    else:
        items.append(Item("数据盘汇总", "未检出", summary_note or "未发现挂载于非系统路径的磁盘"))
    return items


class HardwareModule(Module):
    name = "hardware"
    title = "硬件配置"
    group = "硬件配置"

    def probes(self):
        disk_mounts = (
            "for d in $(lsblk -dn -o NAME,TYPE 2>/dev/null"
            " | awk '$2==\"disk\" && $1 !~ /^(nbd|loop|ram|zram|sr)/ {print $1}'); do"
            " echo \"$d=$(lsblk /dev/$d -rno MOUNTPOINTS 2>/dev/null"
            " | grep -v '^[[:space:]]*$' | sed 's/\\\\x0a/ /g' | tr '\\n' ' ')\"; done"
        )
        return [
            ("hw_meminfo", "grep '^MemTotal' /proc/meminfo || true"),
            ("hw_dmi_mem",
             "dmidecode -t memory 2>/dev/null"
             " | grep -E '^[[:space:]]+(Size|Type|Speed|Configured Memory Speed):' || true"),
            ("hw_lsblk",
             "lsblk -d -J -o NAME,SIZE,ROTA,TRAN,MODEL,TYPE,REV 2>/dev/null || "
             "lsblk -d -n -o NAME,SIZE,ROTA,TRAN,MODEL,TYPE 2>/dev/null || true"),
            ("hw_disk_mounts", disk_mounts),
            ("hw_nvme_versions", NVME_VERSION_PROBE),
            ("hw_disk_sysfs", DISK_SYSFS_PROBE),
        ]

    def parse(self, sections):
        disks = parse_disks(sections.get("hw_lsblk", ""),
                            sections.get("hw_disk_mounts", ""))
        return (parse_memory(sections.get("hw_dmi_mem", ""),
                             sections.get("hw_meminfo", ""))
                + disks + parse_nvme_versions(sections.get("hw_nvme_versions", ""), disks)
                + disk_inventory(read_disks(sections.get("hw_lsblk", "")),
                                 sections.get("hw_disk_sysfs", ""),
                                 sections.get("hw_nvme_versions", "")))
