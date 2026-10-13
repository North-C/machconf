"""machconf 解析与渲染测试: 离线运行, 基于 fixture (鲲鹏950 真实输出 + x86 合成输出).

运行: cd tools/machconf && python3 -m unittest test_machconf -v
"""

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import machconf  # noqa: E402
from modules import MODULES  # noqa: E402
from modules.base import split_sections  # noqa: E402

FIXTURES = HERE / "tests" / "fixtures"


def load(machine, tag):
    return (FIXTURES / machine / (tag + ".txt")).read_text()


def parse_module(name, machine):
    mod = MODULES[name]
    sections = {tag: load(machine, tag) for tag, _ in mod.probes()}
    return {item.key: item for item in mod.parse(sections)}


class BaseTests(unittest.TestCase):
    def test_split_sections(self):
        out = "@@MCTAG:a@@\n1\n2\n@@MCTAG:b@@\nx\n"
        self.assertEqual(split_sections(out), {"a": "1\n2", "b": "x"})

    def test_split_sections_ignores_pre_marker_output(self):
        self.assertEqual(split_sections("banner\n@@MCTAG:a@@\n1"), {"a": "1"})


class LocalRunnerTests(unittest.TestCase):
    def test_run_script_local(self):
        from modules.base import LocalRunner, build_script, split_sections
        runner = LocalRunner()
        out = runner.run_script(build_script([("a", "echo hello"), ("b", "printf x")]))
        self.assertEqual(split_sections(out), {"a": "hello", "b": "x"})

    def test_run_command_failure_raises(self):
        from modules.base import LocalRunner, RunnerError
        with self.assertRaises(RunnerError):
            LocalRunner().run_command("exit 3")

    def test_make_runner_dispatch(self):
        from modules import LOCAL, make_runner
        from modules.base import LocalRunner, SshRunner
        self.assertIsInstance(make_runner(LOCAL), LocalRunner)
        self.assertIsInstance(make_runner("192.0.2.10"), SshRunner)


class CliLocalTests(unittest.TestCase):
    def test_local_and_hosts_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            machconf.main(["--local", "-H", "192.0.2.10"])

    def test_local_json_smoke(self):
        """--local 在本机真实执行, 断言结构而非具体值 (跨平台)."""
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = machconf.main(["--local", "-m", "numa", "-f", "json"])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        (host, modules), = data.items()
        self.assertTrue(host)
        self.assertIn("numa", modules)


class NumaTests(unittest.TestCase):
    def test_kunpeng_enabled(self):
        items = parse_module("numa", "kunpeng950-openeuler2403sp3")
        self.assertIn("1 (内核自动在 NUMA 节点间迁移内存页)",
                      items["numa_balancing 开关"].value)

    def test_x86_persistent_override(self):
        items = parse_module("numa", "x86-synthetic")
        item = items["持久化配置"]
        self.assertEqual(item.value, "kernel.numa_balancing = 0")

    def test_unsupported_kernel(self):
        mod = MODULES["numa"]
        items = {i.key: i for i in mod.parse({"numa_value": "", "numa_cmdline": "",
                                              "numa_persist": ""})}
        self.assertEqual(items["numa_balancing 开关"].value, "N.A.")


class OsTests(unittest.TestCase):
    def test_kunpeng_hugepage(self):
        items = parse_module("os", "kunpeng950-openeuler2403sp3")
        self.assertIn("158320", items["静态大页 (HugeTLB)"].value)
        self.assertIn("309 GB", items["静态大页 (HugeTLB)"].value)
        self.assertIn("node2/hugepages-2048kB=49152", items["大页 NUMA 分布"].value)
        self.assertNotIn("=0", items["大页 NUMA 分布"].value)
        self.assertEqual(items["透明大页 (THP)"].value, "always")

    def test_x86_no_hugepage(self):
        items = parse_module("os", "x86-synthetic")
        self.assertEqual(items["静态大页 (HugeTLB)"].value, "未配置")
        self.assertEqual(items["透明大页 (THP)"].value, "madvise")
        self.assertNotIn("大页 NUMA 分布", items)


class HardwareTests(unittest.TestCase):
    def test_kunpeng_memory_and_disks(self):
        items = parse_module("hardware", "kunpeng950-openeuler2403sp3")
        self.assertIn("11 × 64 GB = 704 GB", items["内存容量"].value)
        self.assertEqual(items["内存类型"].value, "DDR5")
        self.assertIn("5200 MT/s", items["内存频率"].value)
        self.assertIn("标称 5600 MT/s", items["内存频率"].note)
        self.assertIn("系统盘", items["磁盘 nvme0n1"].value)
        self.assertIn("数据盘", items["磁盘 nvme2n1"].value)
        self.assertIn("/data/cubelet", items["磁盘 nvme2n1"].note)
        self.assertEqual(items["数据盘汇总"].value, "1 块")
        self.assertIn("另有 2 块未挂载", items["数据盘汇总"].note)

    def test_x86_disk_types_and_mem_fallback(self):
        items = parse_module("hardware", "x86-synthetic")
        # dmi 可用: 2 x 16GB DDR4, 实配 2933
        self.assertIn("2 × 16 GB", items["内存容量"].value)
        self.assertEqual(items["内存频率"].value, "2933 MT/s")
        self.assertIn("HDD (机械盘)", items["磁盘 sda"].value)
        self.assertIn("SATA/SAS SSD", items["磁盘 sdb"].value)
        self.assertIn("NVMe SSD", items["磁盘 nvme0n1"].value)
        self.assertIn("系统盘", items["磁盘 sda"].value)
        self.assertIn("数据盘", items["磁盘 sdb"].value)

    def test_memory_meminfo_fallback(self):
        mod = MODULES["hardware"]
        from modules.hardware import parse_memory
        items = {i.key: i for i in
                 parse_memory("", "MemTotal:       134321340 kB\n")}
        self.assertIn("GB", items["内存容量"].value)
        self.assertIn("/proc/meminfo", items["内存容量"].value)


class S920X20Tests(unittest.TestCase):
    """第二机型: 华为 S920X20 / 鲲鹏 920 (SATA RAID1 系统盘 + NVMe 数据盘)."""

    def test_bios_s920x20(self):
        items = parse_module("bios", "huawei-s920x20-kunpeng920")
        self.assertIn("32.64", items["BIOS 固件"].value)
        self.assertEqual(items["整机型号"].value, "S920X20")
        self.assertIn("Kunpeng 920 7280Z", items["CPU 型号"].value)
        self.assertIn("320 逻辑 CPU", items["CPU 核心"].value)
        self.assertIn("node3: CPU 240-319", items["NUMA 节点分布"].note)

    def test_os_hugepage_single_node(self):
        items = parse_module("os", "huawei-s920x20-kunpeng920")
        self.assertIn("40000 × 2 MB = 78 GB", items["静态大页 (HugeTLB)"].value)
        # 大页全部在 node0, 分布中不应出现其他节点
        self.assertEqual(items["大页 NUMA 分布"].value,
                         "node0/hugepages-2048kB=40000")

    def test_hardware_raid1_system_disks(self):
        items = parse_module("hardware", "huawei-s920x20-kunpeng920")
        self.assertIn("16 × 64 GB = 1024 GB", items["内存容量"].value)
        self.assertIn("4800 MT/s", items["内存频率"].value)
        # RAID1 双系统盘: sda/sdb 均挂有 /, 均为 SATA SSD
        for disk in ("sda", "sdb"):
            self.assertIn("SATA/SAS SSD", items["磁盘 %s" % disk].value)
            self.assertIn("系统盘", items["磁盘 %s" % disk].value)
        self.assertIn("数据盘", items["磁盘 nvme0n1"].value)
        self.assertEqual(items["数据盘汇总"].value, "1 块")


class BiosTests(unittest.TestCase):
    def test_kunpeng_arm(self):
        items = parse_module("bios", "kunpeng950-openeuler2403sp3")
        self.assertIn("Huawei Corp. 13.22.00.32", items["BIOS 固件"].value)
        self.assertIn("Kunpeng 950 7592C", items["CPU 型号"].value)
        self.assertNotIn("Not Specified", items["CPU 型号"].value)
        self.assertIn("384 逻辑 CPU", items["CPU 核心"].value)
        self.assertIn("4 个 NUMA 节点", items["NUMA 节点分布"].value)
        self.assertIn("node3: CPU 288-383", items["NUMA 节点分布"].note)
        self.assertEqual(items["CPU 调频策略 (governor)"].value, "performance")
        self.assertIn("cppc_cpufreq", items["CPU 调频策略 (governor)"].note)
        self.assertIn("未检测到睿频", items["睿频/超频 (Turbo)"].value)
        self.assertEqual(items["C-state (空闲态)"].value, "N.A.")

    def test_x86_turbo_and_epb(self):
        items = parse_module("bios", "x86-synthetic")
        self.assertEqual(items["睿频/超频 (Turbo)"].value, "开启")
        self.assertIn("no_turbo=0", items["睿频/超频 (Turbo)"].note)
        self.assertIn("normal (6)", items["能效偏好 (EPB)"].value)
        self.assertEqual(items["CPU 调频策略 (governor)"].value, "powersave")
        self.assertEqual(items["C-state (空闲态)"].value, "C1 C1E C6")
        self.assertIn("2 路 × 26 核", items["CPU 核心"].value)
        self.assertIn("node1: CPU 26-51,78-103", items["NUMA 节点分布"].note)


class RenderTests(unittest.TestCase):
    def _fake_results(self):
        from modules.base import Item
        item = Item("测试|项", "值|带竖线", "注|解")
        mod = MODULES["numa"]
        return [("host-a", [(mod, [item])])]

    def test_json_roundtrip(self):
        data = json.loads(machconf.render_json(self._fake_results()))
        self.assertIn("host-a", data)
        self.assertEqual(data["host-a"]["numa"][0]["key"], "测试|项")

    def test_markdown_escapes_pipe(self):
        md = machconf.render_markdown(self._fake_results())
        self.assertIn("值\\|带竖线", md)
        self.assertIn("注\\|解", md)

    def test_table_contains_host(self):
        table = machconf.render_table(self._fake_results())
        self.assertIn("host-a", table)
        self.assertIn("值|带竖线", table)


if __name__ == "__main__":
    unittest.main()
