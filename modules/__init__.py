"""machconf 检测模块注册表. 新增模块: 实现 Module 子类后加入 _ORDER 与 _build_registry."""

from .base import Item, Module, SshError, SshRunner, build_script, split_sections
from .numa import NumaModule

# 汇总表的展示顺序
_ORDER = ["numa"]


def _build_registry():
    modules = [NumaModule()]
    by_name = {m.name: m for m in modules}
    return by_name


MODULES = _build_registry()
MODULE_ORDER = [name for name in _ORDER if name in MODULES] \
    + [name for name in MODULES if name not in _ORDER]


def collect(host, module_names, runner=None):
    """在 host 上执行指定模块的探测并解析, 返回 [(module, [Item])], 顺序按 MODULE_ORDER."""
    runner = runner or SshRunner(host)
    mods = [MODULES[name] for name in MODULE_ORDER if name in module_names]
    probes = []
    for mod in mods:
        probes.extend(mod.probes())
    stdout = runner.run_script(build_script(probes))
    sections = split_sections(stdout)
    return [(mod, mod.parse(sections)) for mod in mods]
