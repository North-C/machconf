"""machconf 基础框架: SSH 执行器、探测脚本组装、结果模型、模块基类."""

import re
import subprocess
import unicodedata


class SshError(RuntimeError):
    """SSH 连接或执行失败."""


class SshRunner:
    """通过 `ssh user@host bash -s` 在远端执行探测脚本, 复用免密登录配置."""

    def __init__(self, host, user="root", connect_timeout=5, command_timeout=30):
        self.host = host
        self.user = user
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout

    def run_script(self, script):
        argv = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=%d" % self.connect_timeout,
            "%s@%s" % (self.user, self.host),
            "bash -s",
        ]
        try:
            proc = subprocess.run(
                argv, input=script, capture_output=True, text=True,
                timeout=self.connect_timeout + self.command_timeout,
            )
        except FileNotFoundError:
            raise SshError("未找到 ssh 命令")
        except subprocess.TimeoutExpired:
            raise SshError("%s: 命令执行超时 (> %ds)" % (self.host, self.command_timeout))
        if proc.returncode != 0:
            raise SshError("%s: ssh 失败 (rc=%d): %s"
                           % (self.host, proc.returncode, proc.stderr.strip()[:200]))
        return proc.stdout

    def run_command(self, command):
        """执行单条命令, 返回 stdout; 用于写操作(如修改配置)."""
        script = "set -e\n%s\n" % command
        return self.run_script(script)


# ---------- 探测脚本组装与解析 ----------

_MARK_RE = re.compile(r"^@@MCTAG:(\S+)@@$")


def build_script(probes):
    """将 [(tag, command), ...] 组装为一个带分段标记的 bash 脚本.

    每个 command 应自带容错 (如 `... 2>/dev/null || true`), 框架不在远端使用 set -e,
    保证单个探测失败不影响其余探测.
    """
    lines = []
    for tag, command in probes:
        lines.append('echo "@@MCTAG:%s@@"' % tag)
        lines.append(command)
    # 探测命令自带容错, 末尾显式 exit 0 使 ssh 返回码只反映连接/登录失败
    lines.append("exit 0")
    return "\n".join(lines) + "\n"


def split_sections(stdout):
    """按 @@MCTAG:<tag>@@ 标记把远端输出切分为 {tag: text}."""
    sections, current, buf = {}, None, []
    for line in stdout.splitlines():
        match = _MARK_RE.match(line)
        if match:
            if current is not None:
                sections[current] = "\n".join(buf)
            current, buf = match.group(1), []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf)
    return sections


# ---------- 结果模型 ----------

class Item:
    """一条配置项: key=项目名, value=值, note=补充说明或检测不到的原因."""

    def __init__(self, key, value, note=""):
        self.key = key
        self.value = value
        self.note = note


class Module:
    """检测模块基类. 子类定义 name/title/group, 并实现 probes() 和 parse()."""

    name = ""    # CLI 选择的短名 (-m)
    title = ""   # 展示标题
    group = ""   # 汇总表中的分组标题 (如 "OS 配置")

    def probes(self):
        """返回 [(tag, command), ...]; command 在远端执行, 输出以 tag 标记."""
        raise NotImplementedError

    def parse(self, sections):
        """把 {tag: text} 解析为 [Item]; 无法获取的项给出 note 说明."""
        raise NotImplementedError


# ---------- 展示宽度 (中文对齐) ----------

def disp_width(text):
    """终端显示宽度: 东亚宽字符按 2 列计."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def pad(text, width):
    """按显示宽度右补空格."""
    return text + " " * max(0, width - disp_width(text))


def first_group(pattern, text, flags=0):
    """提取第一个正则捕获组, 无匹配返回空串 (探测结果缺失时的统一容错)."""
    match = re.search(pattern, text, flags)
    return match.group(1).strip() if match else ""


def onoff_text(value):
    """把 0/1/on/off 转为 开启/关闭 文案, 其余原样返回."""
    if value in ("1", "on", "enable", True, 1):
        return "开启"
    if value in ("0", "off", "disable", False, 0):
        return "关闭"
    return value
