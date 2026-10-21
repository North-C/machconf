"""Disk identity, explicit product mappings, and endpoint PCIe observations."""

import json
import re
from pathlib import Path

from .base import Item

MODEL_CATALOG = Path(__file__).resolve().parent.parent / "refs" / "disk-models.json"
NVME_NAME = re.compile(r"nvme[0-9]+(?:c[0-9]+)?n[0-9]+")

# Resolve the namespace's actual controller, never infer it from nvmeN numbering.
# Virtual multipath heads without a unique device link remain unknown.
DISK_SYSFS_PROBE = r'''
disk_attr() {
    disk_value=$(cat "$2" 2>/dev/null) || return 0
    printf '%s|%s|' "$disk_name" "$1"
    printf '%s' "$disk_value" | tr '\n\r' '  '
    printf '\n'
}
for disk_path in /sys/block/nvme*n*; do
    [ -e "$disk_path" ] || continue
    disk_name=${disk_path##*/}
    [[ "$disk_name" =~ ^nvme[0-9]+(c[0-9]+)?n[0-9]+$ ]] || continue
    disk_ctrl=$(readlink -f "$disk_path/device" 2>/dev/null)
    disk_ctrl_name=${disk_ctrl##*/}
    if ! [[ "$disk_ctrl_name" =~ ^nvme[0-9]+$ ]] ||
       [ "$(readlink -f "$disk_ctrl/subsystem" 2>/dev/null)" != /sys/class/nvme ]; then
        printf '%s|state|controller_unresolved\n' "$disk_name"
        continue
    fi
    printf '%s|controller|%s\n' "$disk_name" "$disk_ctrl_name"
    for disk_attr_name in model firmware_rev transport; do
        disk_attr "$disk_attr_name" "$disk_ctrl/$disk_attr_name"
    done
    disk_transport=$(cat "$disk_ctrl/transport" 2>/dev/null)
    if [ "$disk_transport" != pcie ]; then
        printf '%s|state|non_pcie_or_unknown\n' "$disk_name"
        continue
    fi
    disk_pci=$(readlink -f "$disk_ctrl/device" 2>/dev/null)
    if [ "$(readlink -f "$disk_pci/subsystem" 2>/dev/null)" != /sys/bus/pci ]; then
        printf '%s|state|pci_endpoint_unresolved\n' "$disk_name"
        continue
    fi
    printf '%s|pci_address|%s\n' "$disk_name" "${disk_pci##*/}"
    for disk_attr_name in max_link_speed max_link_width current_link_speed current_link_width; do
        disk_attr "$disk_attr_name" "$disk_pci/$disk_attr_name"
    done
done
'''


def read_disks(text):
    """Use lsblk JSON to preserve empty columns and multiword models; accept old fixtures."""
    if text.lstrip().startswith("{"):
        try:
            rows = json.loads(text).get("blockdevices", [])
        except (ValueError, AttributeError):
            return []
        if not isinstance(rows, list):
            return []
    else:
        rows = []
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 6:
                rows.append(dict(zip(("name", "size", "rota", "tran", "model", "type"),
                                     parts[:4] + [" ".join(parts[4:-1]), parts[-1]])))
    disks = []
    for row in rows:
        if not isinstance(row, dict) or row.get("type") != "disk":
            continue
        name = row.get("name", "")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.!+-]+", name):
            continue
        if name.startswith(("nbd", "loop", "ram", "zram", "sr")):
            continue
        disk = {key: str(row[key]).strip() if row.get(key) is not None else ""
                for key in ("name", "size", "rota", "tran", "model", "rev")}
        if type(row.get("rota")) is bool:
            disk["rota"] = "1" if row["rota"] else "0"
        disks.append(disk)
    return disks


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key: " + key)
        result[key] = value
    return result


def load_model_catalog(path):
    """Only exact, sourced model mappings are accepted; no prefix guesses."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique)
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise ValueError("schema_version must be 1")
        models = data.get("models")
        if not isinstance(models, dict):
            raise ValueError("models must be an object")
        for model, entry in models.items():
            if not model.strip() or model != model.strip() or not isinstance(entry, dict):
                raise ValueError("invalid model entry")
            if any(not isinstance(entry.get(k), str) or not entry[k].strip()
                   for k in ("series", "generation", "source")):
                raise ValueError("each model requires series, generation and source")
        return models, ""
    except (OSError, ValueError) as exc:
        return {}, "型号映射不可用: %s" % exc


def _pcie_value(speed, width):
    match = re.fullmatch(r"(\d+(?:\.\d+)?) GT/s(?: PCIe)?", speed)
    if not match or float(match[1]) <= 0 or not width.isdigit() or not 0 < int(width) <= 32:
        return "N.A."
    rate = float(match[1])
    generation = {2.5: "1.0", 5: "2.0", 8: "3.0", 16: "4.0", 32: "5.0", 64: "6.0"}.get(rate)
    if generation:
        return "PCIe %s ×%s (%g GT/s)" % (generation, width, rate)
    return "PCIe ×%s (%g GT/s，代际未识别)" % (width, rate)


def disk_inventory(disks, sysfs_text, identify_text="", catalog_path=MODEL_CATALOG):
    observed = {}
    for line in sysfs_text.splitlines():
        fields = line.split("|", 2)
        if len(fields) == 3 and NVME_NAME.fullmatch(fields[0]):
            observed.setdefault(fields[0], {})[fields[1]] = fields[2].strip()
    identify = {}
    for line in identify_text.splitlines():
        fields = line.split("|", 2)
        if len(fields) != 3 or not NVME_NAME.fullmatch(fields[0]):
            continue
        observed.setdefault(fields[0], {})
        if fields[1] == "ok":
            try:
                data = json.loads(fields[2])
                if isinstance(data, dict):
                    identify[fields[0]] = data
            except ValueError:
                pass
    by_name = {disk["name"]: disk for disk in disks}
    for name in observed:
        by_name.setdefault(name, {"name": name})
    catalog, catalog_error = load_model_catalog(catalog_path)
    items = []
    for name, disk in sorted(by_name.items()):
        attrs = observed.get(name, {})
        ctrl = identify.get(name, {})
        prefix = "磁盘 %s " % name
        is_nvme = bool(NVME_NAME.fullmatch(name))
        identity = {}
        for label, json_key, sysfs_key, lsblk_key in (
                ("型号", "mn", "model", "model"), ("固件版本", "fr", "firmware_rev", "rev")):
            value, source = "N.A.", "未观测到该字段"
            for candidate, origin in (
                    (ctrl.get(json_key), "nvme id-ctrl /dev/%s: %s" % (name, json_key)),
                    (attrs.get(sysfs_key), "sysfs %s/%s" % (attrs.get("controller", "?"), sysfs_key)),
                    (disk.get(lsblk_key), "lsblk %s" % lsblk_key.upper())):
                if isinstance(candidate, str) and candidate.strip():
                    value, source = candidate.strip(), origin
                    break
            identity[label] = value
            items.append(Item(prefix + label, value, source))
        model = identity["型号"]
        entry = catalog.get(model)
        for label, key in (("产品系列", "series"), ("产品代际", "generation")):
            if entry:
                items.append(Item(prefix + label, entry[key],
                                  "完整型号 %s 精确映射（非设备直接报告）; source=%s"
                                  % (model, entry["source"])))
            else:
                items.append(Item(prefix + label, "N.A.", catalog_error or
                                  "型号 %s 无已确认映射；请在 refs/disk-models.json 补充型号及证据" % model))
        for label, kind in (("PCIe 最大能力", "max"), ("PCIe 当前链路", "current")):
            speed, width = attrs.get(kind + "_link_speed", ""), attrs.get(kind + "_link_width", "")
            transport = attrs.get("transport", "")
            endpoint = attrs.get("pci_address", "")
            if not is_nvme:
                value, note = "N.A.", "未直接观测到磁盘的 PCIe 端点；不使用上游 HBA/桥的能力代替"
            elif transport in ("tcp", "rdma", "fc", "loop"):
                value, note = "not_applicable", "NVMe transport=%s，无本地磁盘 PCIe 链路" % transport
            elif transport == "pcie" and re.fullmatch(r"[0-9a-fA-F]{4,}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]", endpoint):
                value = _pcie_value(speed, width)
                note = "/sys/bus/pci/devices/%s/%s_link_{speed,width}; speed=%s; width=%s" % (
                    endpoint, kind, speed or "missing", width or "missing")
                note += "；设备端点能力" if kind == "max" else "；采集时瞬时链路状态"
            else:
                value, note = "N.A.", "未确认唯一 PCIe 控制器/端点；state=%s" % attrs.get("state", "not_observed")
            items.append(Item(prefix + label, value, note))
    return items
