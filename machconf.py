#!/usr/bin/env python3
"""machconf — 机器配置检测工具 (跨机型: x86 / ARM 均可, 不可用项优雅降级).

用法:
  python3 machconf.py -H 192.168.25.65                    # 远程全模块检测, 表格输出
  python3 machconf.py --local                             # 本机检测, 无需 SSH (root 可得全量)
  python3 machconf.py -H 192.168.25.65 -m numa            # 单模块
  python3 machconf.py -H 192.168.25.65,192.168.25.61      # 多台汇总
  python3 machconf.py -H 192.168.25.65 -f json            # JSON (供程序消费)
  python3 machconf.py -H 192.168.25.65 -f markdown        # Markdown 报告
  python3 machconf.py -H 192.168.25.65 -f csv             # CSV 长表 (Excel/pandas)
  python3 machconf.py -H 192.168.25.65,192.168.25.61 -f csv-wide   # CSV 宽表 (多机对比)
  python3 machconf.py -H 192.168.25.65 -m cube            # CubeSandbox 组件对比社区 v0.7.1
  python3 machconf.py -H 192.168.25.65 --set-numa 0       # 修改 numa_balancing (需root)
  python3 machconf.py --list-modules                      # 列出可用模块

模块: bios (BIOS配置/拓扑/功耗/超频), os (内核/hugepage), numa (NUMA balancing),
      hardware (内存/磁盘), cube (CubeSandbox 组件是否与社区 v0.7.1 一致).
      cube-runtime (Cube 进程与 systemd 配置), 独立入口 cube_runtime.py.
      numa 的本机修改可直接用同目录 numa_balancing.sh.
"""

import argparse
import csv
import io
import json
from pathlib import Path
import shlex
import socket
import sys

from modules import LOCAL, MODULES, MODULE_ORDER, SshError, make_runner, collect
from modules.base import disp_width, pad
from modules.memory_controls import (
    THP_MODES, clear_hugepages_command, parse_change_output, set_thp_command,
)
from modules.kvm_arm_boot import BootError, normalize_settings

SET_NUMA_CMD = "echo %s > /proc/sys/kernel/numa_balancing && cat /proc/sys/kernel/numa_balancing"


def display_host(host):
    """输出中展示的主机名: 本机模式用真实主机名."""
    return socket.gethostname() if host == LOCAL else host


def render_table(host_results):
    """按 分组标题 + 对齐表格 渲染; host_results: [(host, [(module, items)])]."""
    lines = []
    for host, results in host_results:
        lines.append("=" * 8 + " %s " % host + "=" * 24)
        current_group = None
        # 对齐宽度按单台机器内计算
        all_items = [item for _, items in results for item in items]
        key_width = max([14] + [disp_width(item.key) for item in all_items])
        for module, items in results:
            if not items:
                continue
            if module.group != current_group:
                current_group = module.group
                lines.append("── %s ─────" % current_group)
            for item in items:
                line = "  %s  %s" % (pad(item.key, key_width), item.value)
                if item.note:
                    line += "    # %s" % item.note
                lines.append(line)
        lines.append("")
    return "\n".join(lines)


def render_json(host_results):
    data = {}
    for host, results in host_results:
        data[host] = {
            module.name: [
                {"key": item.key, "value": item.value, "note": item.note}
                for item in items
            ]
            for module, items in results
        }
    return json.dumps(data, ensure_ascii=False, indent=2)


def render_markdown(host_results):
    lines = ["# 机器配置检测报告", ""]
    for host, results in host_results:
        lines += ["## %s" % host, ""]
        current_group = None
        for module, items in results:
            if not items:
                continue
            if module.group != current_group:
                current_group = module.group
                lines += ["", "### %s" % current_group, "",
                          "| 配置项 | 值 | 说明 |", "| --- | --- | --- |"]
            for item in items:
                note = item.note.replace("|", "\\|")
                value = item.value.replace("|", "\\|")
                lines.append("| %s | %s | %s |" % (item.key, value, note))
        lines.append("")
    return "\n".join(lines)


def render_csv(host_results):
    """长表 CSV: 每行一个配置项 (主机,模块,配置项,值,说明), 适合表格软件/pandas.

    首字符写入 UTF-8 BOM, 保证 Excel 直接打开中文不乱码.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["主机", "模块", "配置项", "值", "说明"])
    for host, results in host_results:
        for module, items in results:
            for item in items:
                writer.writerow([host, module.title, item.key, item.value, item.note])
    return "\ufeff" + buf.getvalue()


def render_csv_wide(host_results):
    """宽表 CSV: 一行一个配置项, 每台主机一列, 便于多机对比; 配置项按首次出现排序."""
    by_key = {}  # key -> {host: value}, dict 保序
    for host, results in host_results:
        for module, items in results:
            for item in items:
                by_key.setdefault(item.key, {})[host] = item.value
    hosts = [host for host, _ in host_results]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["配置项"] + hosts)
    for key, values in by_key.items():
        writer.writerow([key] + [values.get(host, "") for host in hosts])
    return "\ufeff" + buf.getvalue()


RENDERERS = {"table": render_table, "json": render_json, "markdown": render_markdown,
             "csv": render_csv, "csv-wide": render_csv_wide}


def set_numa(hosts, value, ask_pass=False):
    """修改 numa_balancing (远端 SSH 或本机) 并回显新值."""
    ok = True
    for host in hosts:
        try:
            new = make_runner(host, ask_pass=ask_pass).run_command(
                SET_NUMA_CMD % value).strip()
            print("%s: numa_balancing -> %s" % (display_host(host), new or "(无回显)"))
        except SshError as exc:
            print("%s: 修改失败: %s" % (display_host(host), exc), file=sys.stderr)
            ok = False
    return 0 if ok else 1


def change_memory(hosts, clear_hugepages=False, thp=None, ask_pass=False):
    """逐主机执行显式请求的运行时修改，逐项输出并汇总失败。"""
    actions = []
    if thp is not None:
        actions.append(("THP", set_thp_command(thp)))
    if clear_hugepages:
        actions.append(("HugeTLB 清除", clear_hugepages_command()))
    ok = True
    for host in hosts:
        runner = make_runner(host, ask_pass=ask_pass)
        for label, command in actions:
            print("%s: %s (仅运行时生效)" % (display_host(host), label))
            try:
                success, lines = parse_change_output(runner.run_command(command))
                for line in lines:
                    print("  " + line)
                ok = success and ok
            except SshError as exc:
                print("%s: %s 失败: %s；状态未知，可能已部分生效，请重新查询"
                      % (display_host(host), label, exc), file=sys.stderr)
                ok = False
    return 0 if ok else 1


def kvm_boot_script(settings, dry_run=False):
    source = Path(__file__).with_name("modules").joinpath("kvm_arm_boot.py").read_text()
    return ("python3 - --settings %s %s <<'MACHCONF_KVM_BOOT_PY'\n%s\nMACHCONF_KVM_BOOT_PY\n"
            % (shlex.quote(json.dumps(settings)), "--dry-run" if dry_run else "", source))


def change_kvm_arm(hosts, settings, dry_run=False, ask_pass=False):
    ok = True
    for host in hosts:
        print("%s: ARM KVM 默认启动项%s" % (display_host(host), "预览" if dry_run else "修改"))
        try:
            output = make_runner(host, ask_pass=ask_pass, command_timeout=180).run_script(
                kvm_boot_script(settings, dry_run))
            result = json.loads(output)
            if not isinstance(result, dict) or type(result.get("ok")) is not bool:
                raise ValueError("无有效修改结果")
            print("  " + result.get("status", "状态未知"))
            for key in ("config", "kernel", "index", "backup", "note", "error", "rollback_error"):
                if key in result:
                    print("  %s: %s" % (key, result[key]))
            for key, values in result.get("changes", {}).items():
                print("  %s: %s -> %s" % (key, ",".join(values["before"]) or "未设置", values["after"]))
            ok = result["ok"] and ok
        except (SshError, ValueError, KeyError, TypeError) as exc:
            print("%s: 操作失败: %s；若已请求写入，状态未知，请检查备份和启动项"
                  % (display_host(host), exc), file=sys.stderr)
            ok = False
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="机器配置检测工具 (BIOS/OS/NUMA/硬件/CubeSandbox 组件), "
                    "详见 python3 machconf.py -h")
    parser.add_argument("-H", "--hosts", help="远程目标机, 逗号分隔 (ssh 免密)")
    parser.add_argument("--ask-pass", action="store_true",
                        help="远程模式交互输入 SSH 密码 (需终端前台运行; "
                             "同主机 1 分钟内复用连接, 只输一次)")
    parser.add_argument("--local", action="store_true",
                        help="检测本机 (无需 SSH; BIOS/内存等完整信息需 root)")
    parser.add_argument("-m", "--modules", default="all",
                        help="模块名逗号分隔 (默认 all): %s" % ",".join(MODULE_ORDER))
    parser.add_argument("-f", "--format", default="table", choices=list(RENDERERS),
                        help="输出格式 (默认 table)")
    parser.add_argument("--cube-root", metavar="DIR",
                        help="cube / cube-runtime 模块: CubeSandbox 安装目录 "
                             "(默认 /usr/local/services/cubetoolbox)")
    parser.add_argument("--set-numa", metavar="0|1", choices=["0", "1"],
                        help="修改 kernel.numa_balancing (root)")
    parser.add_argument("--clear-hugepages", action="store_true",
                        help="清除所有尺寸的 HugeTLB 静态大页池并回读 (root, 仅运行时)")
    parser.add_argument("--set-thp", choices=THP_MODES,
                        help="设置顶层及各尺寸匿名 THP 模式 (root, 仅运行时)")
    parser.add_argument("--set-kvm-arm", action="append", metavar="KEY=0|1",
                        help="修改默认内核启动项的 ARM KVM 白名单参数，可重复传入；需重启后核验")
    parser.add_argument("--dry-run", action="store_true",
                        help="仅预览 --set-kvm-arm 的修改，不写文件")
    parser.add_argument("--list-modules", action="store_true", help="列出可用模块")
    args = parser.parse_args(argv)

    if args.dry_run and not args.set_kvm_arm:
        parser.error("--dry-run 仅用于 --set-kvm-arm")
    if args.set_kvm_arm and (args.set_numa or args.set_thp or args.clear_hugepages or args.list_modules):
        parser.error("--set-kvm-arm 不能与其它修改或 --list-modules 组合")
    try:
        boot_settings = normalize_settings(args.set_kvm_arm) if args.set_kvm_arm else None
    except BootError as exc:
        parser.error(str(exc))
    if args.list_modules:
        for name in MODULE_ORDER:
            mod = MODULES[name]
            print("%-10s %s (%s)" % (name, mod.title, mod.group))
        return 0
    if args.local and args.hosts:
        parser.error("--local 与 -H/--hosts 互斥")
    if args.ask_pass and args.local:
        parser.error("--ask-pass 仅用于远程模式 (-H)")
    if not args.local and not args.hosts:
        parser.error("需要 --local 或 -H/--hosts (或 --list-modules)")

    MODULES["cube"].root = args.cube_root
    MODULES["cube-runtime"].root = args.cube_root
    hosts = [LOCAL] if args.local else \
        [h.strip() for h in args.hosts.split(",") if h.strip()]
    if not hosts:
        parser.error("目标主机列表不能为空")
    if boot_settings:
        return change_kvm_arm(hosts, boot_settings, args.dry_run, args.ask_pass)
    if args.set_numa and (args.clear_hugepages or args.set_thp):
        parser.error("--set-numa 请单独执行，不能与大页修改选项组合")
    if args.clear_hugepages or args.set_thp:
        return change_memory(hosts, args.clear_hugepages, args.set_thp, args.ask_pass)
    if args.set_numa:
        return set_numa(hosts, args.set_numa, ask_pass=args.ask_pass)

    if args.modules == "all":
        selected = set(MODULE_ORDER)
    else:
        selected = {m.strip() for m in args.modules.split(",") if m.strip()}
        unknown = selected - set(MODULES)
        if unknown:
            parser.error("未知模块: %s (可选: %s)" % (",".join(sorted(unknown)),
                                                     ",".join(MODULE_ORDER)))

    host_results, failed = [], []
    for host in hosts:
        try:
            host_results.append((display_host(host), collect(host, selected,
                                                            ask_pass=args.ask_pass)))
        except SshError as exc:
            print("错误: %s" % exc, file=sys.stderr)
            failed.append(display_host(host))
    if not host_results:
        return 1
    print(RENDERERS[args.format](host_results))
    if failed:
        print("失败主机: %s" % ",".join(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
