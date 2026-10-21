"""Disk inventory preserves identity and distinguishes PCIe capability from state."""

import json
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

from modules.disk_inventory import (
    DISK_SYSFS_PROBE, disk_inventory, load_model_catalog, read_disks,
)
from modules.hardware import HardwareModule
from modules.compare import compare_reports
from machconf import render_json, render_markdown


def attrs(name="nvme0n1", **overrides):
    values = dict(controller="nvme0", model="ES3600P V7", firmware_rev="1049",
                  transport="pcie", pci_address="0000:28:00.0",
                  max_link_speed="32.0 GT/s PCIe", max_link_width="4",
                  current_link_speed="16.0 GT/s PCIe", current_link_width="2")
    values.update(overrides)
    return "\n".join("%s|%s|%s" % (name, key, value) for key, value in values.items())


def by_label(items):
    return {item.key.split(" ", 2)[2]: item for item in items}


class DiskInventoryTests(unittest.TestCase):
    def test_json_empty_columns_do_not_shift_model_or_firmware(self):
        disks = read_disks(json.dumps({"blockdevices": [dict(
            name="sda", type="disk", size="1T", rota=False, tran=None,
            model="Multi Word Device", rev="A123")]}))
        self.assertEqual(disks[0]["model"], "Multi Word Device")
        self.assertEqual(disks[0]["rev"], "A123")
        self.assertEqual(disks[0]["rota"], "0")
        items = by_label(disk_inventory(disks, ""))
        self.assertEqual(items["型号"].value, "Multi Word Device")
        self.assertEqual(items["固件版本"].value, "A123")
        self.assertEqual(items["PCIe 当前链路"].value, "N.A.")

    def test_capability_state_identity_and_mapped_generation_are_separate(self):
        items = by_label(disk_inventory([], attrs()))
        self.assertEqual(items["型号"].value, "ES3600P V7")
        self.assertEqual(items["产品系列"].value, "ES3600P")
        self.assertEqual(items["产品代际"].value, "V7")
        self.assertIn("非设备直接报告", items["产品代际"].note)
        self.assertEqual(items["固件版本"].value, "1049")
        self.assertEqual(items["PCIe 最大能力"].value, "PCIe 5.0 ×4 (32 GT/s)")
        self.assertEqual(items["PCIe 当前链路"].value, "PCIe 4.0 ×2 (16 GT/s)")
        self.assertIn("0000:28:00.0", items["PCIe 当前链路"].note)

    def test_coded_model_cannot_imply_generation(self):
        for model in ("HWE72P456T4M000N", "ES3600P V7-extra", "es3600p v7"):
            items = by_label(disk_inventory([], attrs(model=model)))
            self.assertEqual(items["产品代际"].value, "N.A.")
            self.assertEqual(items["产品系列"].value, "N.A.")

    def test_identify_identity_overrides_sysfs_and_is_trimmed(self):
        items = by_label(disk_inventory([], attrs(),
                         'nvme0n1|ok|{"mn":"  ES3500P V6  ","fr":"  8318  ","ver":66560}'))
        self.assertEqual(items["型号"].value, "ES3500P V6")
        self.assertEqual(items["固件版本"].value, "8318")
        self.assertEqual(items["产品代际"].value, "V6")
        self.assertIn("nvme id-ctrl", items["固件版本"].note)
        fallback = by_label(disk_inventory([], attrs(), 'nvme0n1|missing_nvme|'))
        self.assertEqual(fallback["固件版本"].value, "1049")

    def test_missing_identity_does_not_break_other_items(self):
        items = by_label(disk_inventory([], 'nvme0n1|state|controller_unresolved',
                                       'nvme0n1|ok|{"mn":null,"fr":42}'))
        for field in ("型号", "固件版本", "产品代际", "PCIe 当前链路"):
            self.assertEqual(items[field].value, "N.A.")

    def test_non_pcie_and_ambiguous_controller_are_not_pcie_results(self):
        for transport in ("tcp", "rdma", "fc", "loop"):
            items = by_label(disk_inventory([], attrs(transport=transport)))
            self.assertEqual(items["PCIe 当前链路"].value, "not_applicable")
        for extra in (dict(pci_address=""), dict(transport=""), dict(pci_address="nvme0")):
            items = by_label(disk_inventory([], attrs(**extra)))
            self.assertEqual(items["PCIe 最大能力"].value, "N.A.")

    def test_bad_link_values_are_unavailable_and_new_rates_preserved(self):
        for speed, width in (("Unknown", "4"), ("32.0 GT/s PCIe", "0"),
                             ("32.0 GT/s PCIe", "255"), ("", "4")):
            items = by_label(disk_inventory([], attrs(current_link_speed=speed, current_link_width=width)))
            self.assertEqual(items["PCIe 当前链路"].value, "N.A.")
        items = by_label(disk_inventory([], attrs(current_link_speed="128.0 GT/s PCIe")))
        self.assertIn("128 GT/s", items["PCIe 当前链路"].value)
        self.assertIn("代际未识别", items["PCIe 当前链路"].value)

    def test_local_exact_model_catalog_and_invalid_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"models.json"
            path.write_text(json.dumps({"schema_version": 1, "models": {"OEM-model": {
                "series": "verified-series", "generation": "verified-generation", "source": "asset-record-42"}}}))
            items = by_label(disk_inventory([], attrs(model="OEM-model"), catalog_path=path))
            self.assertEqual(items["产品代际"].value, "verified-generation")
            self.assertIn("asset-record-42", items["产品代际"].note)
            for bad in ('{bad', '{"schema_version":1,"models":{"x":{},"x":{}}}',
                        '{"schema_version":1,"models":{"x":{"series":"s","generation":"V7"}}}'):
                path.write_text(bad)
                self.assertTrue(load_model_catalog(path)[1])
                items = by_label(disk_inventory([], attrs(), catalog_path=path))
                self.assertEqual(items["产品代际"].value, "N.A.")
                self.assertIn("映射不可用", items["产品代际"].note)

    def test_module_render_and_comparison_receive_all_fields(self):
        module = HardwareModule()
        sections = {"hw_lsblk": 'nvme0n1 1T 0 nvme SSD disk',
                    "hw_disk_sysfs": attrs(),
                    "hw_nvme_versions": 'nvme0n1|ok|{"ver":131072}'}
        items = module.parse(sections)
        for render in (render_json, render_markdown):
            output = render([('host', [(module, items)])])
            for field in ('Disk Type', '型号', '产品系列', '产品代际', '固件版本',
                          'NVMe 协议版本', 'PCIe 最大能力', 'PCIe 当前链路'):
                self.assertIn(field, output)
        left = {"hardware": [vars(i) for i in items]}
        sections['hw_disk_sysfs'] = attrs(current_link_speed="32.0 GT/s PCIe", firmware_rev="9999")
        right = {"hardware": [vars(i) for i in module.parse(sections)]}
        rows = compare_reports('left', left, 'right', right)['rows']
        changed = {r['key'] for r in rows if r['status'] == 'different'}
        self.assertIn('磁盘 nvme0n1 固件版本', changed)
        self.assertIn('磁盘 nvme0n1 PCIe 当前链路', changed)
        self.assertNotIn('磁盘 nvme0n1 PCIe 最大能力', changed)

    def test_sysfs_probe_resolves_device_link_not_namespace_number(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()/'sys'
            for name in ('block', 'class/nvme', 'bus/pci'):
                (root/name).mkdir(parents=True)
            endpoint = root/'devices/0000:28:00.0'
            controller = endpoint/'nvme/nvme2'
            namespace = controller/'nvme7n1'
            namespace.mkdir(parents=True)
            (root/'block/nvme7n1').symlink_to(namespace)
            (namespace/'device').symlink_to(controller)
            (controller/'subsystem').symlink_to(root/'class/nvme')
            (controller/'device').symlink_to(endpoint)
            (endpoint/'subsystem').symlink_to(root/'bus/pci')
            for key, value in (('model', 'ES3600P V7'), ('firmware_rev', '1049'), ('transport', 'pcie')):
                (controller/key).write_text(value+'\n')
            for key, value in (('max_link_speed', '32.0 GT/s PCIe'), ('max_link_width', '4'),
                               ('current_link_speed', '16.0 GT/s PCIe'), ('current_link_width', '2')):
                (endpoint/key).write_text(value+'\n')
            script = DISK_SYSFS_PROBE.replace('/sys', shlex.quote(str(root)))
            result = subprocess.run(['bash', '-s'], input=script, text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            items = by_label(disk_inventory([], result.stdout))
            self.assertIn('PCIe 4.0 ×2', items['PCIe 当前链路'].value)
            self.assertIn('nvme7n1|controller|nvme2', result.stdout)
            (namespace/'device').unlink()
            result = subprocess.run(['bash', '-s'], input=script, text=True, capture_output=True, timeout=5)
            items = by_label(disk_inventory([], result.stdout))
            self.assertEqual(items['PCIe 当前链路'].value, 'N.A.')


if __name__ == '__main__':
    unittest.main()
