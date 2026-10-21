"""NVMe Identify Controller parsing and read-only probe failure handling."""

import json
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

from modules.hardware import HardwareModule, NVME_VERSION_PROBE, parse_nvme_versions
from modules.compare import compare_reports


class NvmeVersionTests(unittest.TestCase):
    def test_ver_decoding(self):
        for raw, expected in ((0x10400, "1.4"), (0x20000, "2.0"),
                              (0x10401, "1.4.1"), ("0x10300", "1.3"),
                              ("66560", "1.4")):
            with self.subTest(raw=raw):
                item = parse_nvme_versions('nvme0n1|ok|' + json.dumps({"ver": raw}))[0]
                self.assertEqual(item.value, expected)
                self.assertIn("Identify Controller VER=0x", item.note)

    def test_invalid_missing_and_zero_are_unavailable(self):
        for payload in ('{}', '[]', 'null', '{bad', '{"fr":"1.4"}',
                        *[json.dumps({"ver": v}) for v in
                          (0, -1, True, 1.4, "1.4", None, 0x100000000, 0xffffffff, 255)]):
            with self.subTest(payload=payload):
                self.assertEqual(parse_nvme_versions('nvme0n1|ok|' + payload)[0].value, "N.A.")

    def test_missing_tools_read_failure_timeout_reasons(self):
        for status, reason in (("missing_nvme", "nvme-cli"), ("missing_timeout", "timeout"),
                               ("timeout", "超时"), ("budget_exhausted", "预算耗尽"),
                               ("read_error", "exit=13")):
            item = parse_nvme_versions('nvme0n1|%s|13' % status)[0]
            self.assertEqual(item.value, "N.A.")
            self.assertIn(reason, item.note)

    def test_module_integration_and_missing_historical_probe(self):
        sections = {"hw_lsblk": "nvme0n1 1T 0 nvme SSD disk\nsda 1T 0 sata SSD disk",
                    "hw_disk_mounts": "nvme0n1=/data\nsda=/"}
        items = {item.key: item for item in HardwareModule().parse(sections)}
        key = "磁盘 nvme0n1 NVMe 协议版本"
        self.assertEqual(items[key].value, "N.A.")
        self.assertNotIn("磁盘 sda NVMe 协议版本", items)
        self.assertEqual(items["数据盘汇总"].value, "1 块")
        sections["hw_nvme_versions"] = 'nvme0n1|ok|{"ver":66560}'
        self.assertEqual({i.key: i.value for i in HardwareModule().parse(sections)}[key], "1.4")

    def test_multiple_namespaces_multipath_and_no_lsblk(self):
        items = parse_nvme_versions('nvme0n1|ok|{"ver":66560}\n'
                                    'nvme0n2|ok|{"ver":66560}\n'
                                    'nvme1c2n1|ok|{"ver":131072}\n'
                                    'nvme0n1p1|ok|{"ver":66560}\n'
                                    'sda|ok|{"ver":66560}')
        self.assertEqual([i.value for i in items], ["1.4", "1.4", "2.0"])
        self.assertEqual(parse_nvme_versions(""), [])

    def test_comparison_distinguishes_version_and_unavailable(self):
        def snapshot(version):
            return {"hardware": [vars(i) for i in parse_nvme_versions(
                'nvme0n1|ok|' + json.dumps({"ver": version}))]}
        self.assertEqual(compare_reports('a', snapshot(66560), 'b', snapshot(131072))
                         ['rows'][0]['status'], 'different')
        self.assertEqual(compare_reports('a', snapshot(0), 'b', snapshot(0))
                         ['rows'][0]['status'], 'unavailable')

    def run_probe(self, stub, disks=("nvme0n1",), exhausted=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for disk in disks:
                (root/disk).mkdir()
            script = NVME_VERSION_PROBE.replace('/sys/block', shlex.quote(str(root)))
            if exhausted:
                script = script.replace('nvme_probe_start=$SECONDS',
                                        'nvme_probe_start=$((SECONDS - 10))')
            proc = subprocess.run(['bash', '-s'], input=stub + '\n' + script,
                                  capture_output=True, text=True, timeout=5)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return parse_nvme_versions(proc.stdout)

    def test_probe_passes_namespace_and_parses_multiline_json(self):
        stub = '''
timeout() { shift 3; "$@"; }
nvme() {
    [ "$1" = id-ctrl ] && [ "$2" = /dev/nvme0n1 ] &&
    [ "$3" = -o ] && [ "$4" = json ] || return 90
    printf '{\n"ver":66560,\n"fr":"firmware-not-protocol"\n}\n'
}
'''
        self.assertEqual(self.run_probe(stub)[0].value, '1.4')
        self.assertEqual(self.run_probe(stub, disks=()), [])

    def test_probe_missing_tool_errors_and_budget(self):
        stub = 'nvme() { :; }; timeout() { return 124; }'
        self.assertIn('超时', self.run_probe(stub)[0].note)
        self.assertIn('预算耗尽', self.run_probe(stub, exhausted=True)[0].note)
        self.assertIn('exit=13', self.run_probe('nvme() { :; }; timeout() { return 13; }')[0].note)
        for tool in ('nvme', 'timeout'):
            stub = '''command() {
                if [ "$2" = %s ]; then return 1; fi
                return 0
            }''' % tool
            self.assertIn('不可用', self.run_probe(stub)[0].note)


if __name__ == '__main__':
    unittest.main()
