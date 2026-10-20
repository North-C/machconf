#!/usr/bin/env python3
"""CubeMaster / Cubelet / CubeAPI 的 Go、CPU/NUMA、内存及并发资源检查。"""
import argparse
import json
import sys

from machconf import RENDERERS, display_host
from modules import LOCAL, make_runner, SshError
from modules.cube_runtime import CubeRuntimeModule, collect_runtime, report_items


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("-H", "--hosts", help="SSH 主机，逗号分隔")
    target.add_argument("--local", action="store_true", help="本机检测")
    parser.add_argument("--ask-pass", action="store_true", help="复用 machconf 的 SSH 交互认证")
    parser.add_argument("--cube-root", help="Cube 安装目录，用于进程身份核验")
    parser.add_argument("-f", "--format", choices=list(RENDERERS), default="table")
    args = parser.parse_args(argv)
    if args.local and args.ask_pass:
        parser.error("--ask-pass 仅用于远程模式")
    hosts = [LOCAL] if args.local else [h.strip() for h in args.hosts.split(",") if h.strip()]
    if not hosts:
        parser.error("至少需要一个主机")
    reports, failed = {}, False
    for host in hosts:
        try:
            data = collect_runtime(make_runner(host, ask_pass=args.ask_pass, command_timeout=120), args.cube_root)
        except SshError as exc:
            print("%s: %s" % (display_host(host), exc), file=sys.stderr)
            failed = True
            continue
        reports[display_host(host)] = data
        failed = failed or bool(data.get("errors"))
    if args.format == "json":
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    else:
        mod = CubeRuntimeModule()
        print(RENDERERS[args.format]([(host, [(mod, report_items(data))]) for host, data in reports.items()]))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
