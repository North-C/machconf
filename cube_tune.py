#!/usr/bin/env python3
"""按 JSON 配置管理 CubeSandbox Go 参数、CPU/NUMA、Docker cpuset 和宿主 RCU。"""

import argparse
import json
from pathlib import Path
import re
import sys

from modules import LOCAL, make_runner
from modules.cube_tune_config import validate_config


def read_config(path):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("JSON 重复字段: " + key)
            result[key] = value
        return result
    return validate_config(json.loads(Path(path).read_text(), object_pairs_hook=unique_pairs))


def build_script(payload):
    """Ship isolated modules via stdin; no remote install or temporary source files."""
    sources = {}
    for name in ("cube_tune_config", "cube_tune_verify", "cube_tune_backend"):
        sources[name] = (Path(__file__).parent / "modules" / (name + ".py")).read_text()
    code = "sources = " + repr(sources) + "\npayload = " + repr(payload) + "\n" + '''import json, sys, types
try:
    if sys.version_info < (3, 8):
        raise RuntimeError("需要 Python 3.8 或更新版本")
    package = types.ModuleType("machconf_tune")
    package.__path__ = []
    sys.modules[package.__name__] = package
    for name, source in sources.items():
        fullname = "machconf_tune." + name
        module = types.ModuleType(fullname)
        module.__package__ = "machconf_tune"
        sys.modules[fullname] = module
        exec(compile(source, "<" + fullname + ">", "exec"), module.__dict__)
    result = sys.modules["machconf_tune.cube_tune_backend"].execute(payload)
except Exception as exc:
    result = {"ok": False, "error": str(exc), "error_type": type(exc).__name__}
print(json.dumps(result, ensure_ascii=False))
'''
    return "python3 - <<'MACHCONF_CUBE_TUNE_PY'\n" + code + "\nMACHCONF_CUBE_TUNE_PY\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "apply", "verify", "restore"), nargs="?", default="plan")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("-H", "--host", help="单个 SSH 主机，如 root@192.168.25.90")
    target.add_argument("--local", action="store_true", help="操作本机（Linux）")
    parser.add_argument("--config", help="本地 JSON 配置文件")
    parser.add_argument("--state-dir", help="目标机上的绝对快照目录；apply 时必须尚不存在")
    parser.add_argument("--ask-pass", action="store_true", help="SSH 交互认证")
    args = parser.parse_args(argv)
    if args.local and args.ask_pass:
        parser.error("--ask-pass 仅用于 SSH")
    if args.action in ("plan", "apply") and not args.config:
        parser.error("plan/apply 需要 --config")
    if args.action == "plan" and args.state_dir:
        parser.error("plan 不使用 --state-dir")
    if args.action == "restore" and (not args.state_dir or args.config):
        parser.error("restore 只使用 --state-dir，不接受 --config")
    if args.action == "verify" and bool(args.config) == bool(args.state_dir):
        parser.error("verify 需要 --config 或 --state-dir，二选一")
    if args.host and ("," in args.host or args.host == LOCAL):
        parser.error("--host 只接受单个 SSH 主机；本机请用 --local")
    if args.state_dir and (not Path(args.state_dir).is_absolute() or ".." in Path(args.state_dir).parts):
        parser.error("--state-dir 需要不含 .. 的绝对路径")
    try:
        payload = {"action": args.action}
        if args.config:
            payload["config"] = read_config(args.config)
        if args.state_dir:
            payload["state_dir"] = args.state_dir
        runner_options = {"ask_pass": args.ask_pass, "command_timeout": 1800}
        hostname = LOCAL if args.local else args.host
        if not args.local:
            user, sep, host = hostname.rpartition("@")
            if sep:
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", user):
                    raise ValueError("无效 SSH 用户名")
                runner_options["user"] = user
                hostname = host
            if not hostname or not re.fullmatch(r"[A-Za-z0-9_.:\[\]-]+", hostname):
                raise ValueError("无效 SSH 主机")
        runner = make_runner(hostname, **runner_options)
        result = json.loads(runner.run_script(build_script(payload)))
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise ValueError("目标机返回无效的结果对象")
    except Exception as exc:
        result = {"ok": False, "error": str(exc), "error_type": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
