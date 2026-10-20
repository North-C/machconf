"""machconf 检测模块注册表. 新增模块: 实现 Module 子类后加入 _ORDER 与 _build_registry."""

from .base import (
    Item,
    LocalRunner,
    Module,
    RunnerError,
    SshError,
    SshRunner,
    build_script,
    split_sections,
)
from .bios import BiosModule
from .cubesandbox import CubeSandboxModule
from .cube_runtime import CubeRuntimeModule
from .hardware import HardwareModule
from .numa import NumaModule
from .osconf import OsModule
from .kvm_arm import KvmArmModule
from .rcu import RcuModule

# 本机模式的 host 标识
LOCAL = "local"

# 汇总表的展示顺序
_ORDER = ["bios", "os", "numa", "hardware", "cube", "cube-runtime", "kvm-arm", "rcu"]


def _build_registry():
    modules = [BiosModule(), OsModule(), NumaModule(), HardwareModule(),
               CubeSandboxModule(), CubeRuntimeModule(), KvmArmModule(), RcuModule()]
    by_name = {m.name: m for m in modules}
    return by_name


MODULES = _build_registry()
MODULE_ORDER = [name for name in _ORDER if name in MODULES] \
    + [name for name in MODULES if name not in _ORDER]


def make_runner(host, ask_pass=False, **kwargs):
    """按 host 选择执行器: 'local' 走本机 bash, 其余走 SSH (ask_pass 见 SshRunner)."""
    if host == LOCAL:
        return LocalRunner(**kwargs)
    return SshRunner(host, ask_pass=ask_pass, **kwargs)


def collect(host, module_names, runner=None, ask_pass=False):
    """在 host ('local' 或远程地址) 上执行指定模块的探测并解析,
    返回 [(module, [Item])], 顺序按 MODULE_ORDER."""
    mods = [MODULES[name] for name in MODULE_ORDER if name in module_names]
    timeout = max([Module.timeout] + [mod.timeout for mod in mods])
    runner = runner or make_runner(host, ask_pass=ask_pass, command_timeout=timeout)
    probes = []
    for mod in mods:
        probes.extend(mod.probes())
    stdout = runner.run_script(build_script(probes))
    sections = split_sections(stdout)
    return [(mod, mod.parse(sections)) for mod in mods]
