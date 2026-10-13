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
from .hardware import HardwareModule
from .numa import NumaModule
from .osconf import OsModule

# 本机模式的 host 标识
LOCAL = "local"

# 汇总表的展示顺序
_ORDER = ["bios", "os", "numa", "hardware"]


def _build_registry():
    modules = [BiosModule(), OsModule(), NumaModule(), HardwareModule()]
    by_name = {m.name: m for m in modules}
    return by_name


MODULES = _build_registry()
MODULE_ORDER = [name for name in _ORDER if name in MODULES] \
    + [name for name in MODULES if name not in _ORDER]


def make_runner(host, **kwargs):
    """按 host 选择执行器: 'local' 走本机 bash, 其余走 SSH."""
    if host == LOCAL:
        return LocalRunner(**kwargs)
    return SshRunner(host, **kwargs)


def collect(host, module_names, runner=None):
    """在 host ('local' 或远程地址) 上执行指定模块的探测并解析,
    返回 [(module, [Item])], 顺序按 MODULE_ORDER."""
    runner = runner or make_runner(host)
    mods = [MODULES[name] for name in MODULE_ORDER if name in module_names]
    probes = []
    for mod in mods:
        probes.extend(mod.probes())
    stdout = runner.run_script(build_script(probes))
    sections = split_sections(stdout)
    return [(mod, mod.parse(sections)) for mod in mods]
