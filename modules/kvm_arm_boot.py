"""Standalone Python 3.8+ backend: edit one default legacy GRUB entry, never reboot."""

import argparse
import datetime
import errno
import fcntl
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile


KEYS = tuple("kvm-arm." + key for key in (
    "vgic_v4_enable", "virt_msi_bypass", "dvmbm_enabled", "ipiv_enabled"))


class BootError(RuntimeError):
    pass


def sync_directory(path):
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
    finally:
        os.close(fd)


def normalize_settings(assignments):
    settings = {}
    for assignment in assignments:
        key, sep, value = assignment.partition("=")
        if not key.startswith("kvm-arm."):
            key = "kvm-arm." + key
        if not sep or key not in KEYS or value not in ("0", "1"):
            raise BootError("仅支持四个白名单参数的 KEY=0|1: %s" % assignment)
        if key in settings:
            raise BootError("同一次请求不允许重复参数: %s" % key)
        settings[key] = value
    if not settings:
        raise BootError("没有请求修改的参数")
    return settings


def run(argv):
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    if proc.returncode:
        raise BootError("%s 执行失败 rc=%s: %s" % (Path(argv[0]).name, proc.returncode,
                                               proc.stderr.strip()[:300]))
    return proc.stdout.strip()


def parse_entries(text):
    entries = []
    for line in text.splitlines():
        if line.startswith("index="):
            entries.append({"index": line[6:]})
        elif line.strip():
            if not entries:
                raise BootError("无法解析 grubby entry 输出")
            key, sep, value = line.partition("=")
            if sep:
                if key in entries[-1]:
                    raise BootError("grubby entry 出现重复字段")
                entries[-1][key] = value
            else:
                entries[-1].setdefault("extra", []).append(line)
    if not entries or len({e["index"] for e in entries}) != len(entries):
        raise BootError("grubby 没有返回唯一启动项列表")
    return entries


def arg_tokens(entry):
    raw = entry.get("args", "")
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]
    # Refuse complex GRUB quoting/expansion instead of rewriting its semantics.
    if any(ch in raw for ch in ('"', "'", "\\", "$", "`", "\n")):
        raise BootError("启动项 args 包含动态变量或复杂引用，不能安全自动修改")
    return raw.split()


def inspect_entries(grubby, config, env, invoke=run):
    base = [grubby, "--grub2", "--config-file=" + str(config), "--env=" + str(env)]
    entries = parse_entries(invoke(base + ["--info=ALL"]))
    kernel = invoke(base + ["--default-kernel"]).strip('"')
    index = invoke(base + ["--default-index"])
    selected = [e for e in entries if e.get("kernel", "").strip('"') == kernel]
    if len(selected) != 1 or selected[0]["index"] != index:
        raise BootError("默认内核未唯一对应一个启动项，拒绝批量修改")
    if not kernel.startswith("/boot/"):
        raise BootError("默认内核路径不是明确的 /boot/ 文件")
    arg_tokens(selected[0])
    return {"entries": entries, "kernel": kernel, "index": index}


def selected_entry(info):
    return next(e for e in info["entries"] if e["index"] == info["index"])


def verify_change(before, after, settings):
    if before["kernel"] != after["kernel"] or before["index"] != after["index"]:
        raise BootError("默认内核/启动索引发生变化")
    if len(before["entries"]) != len(after["entries"]):
        raise BootError("启动项数量发生变化")
    for old, new in zip(before["entries"], after["entries"]):
        if old["index"] != before["index"]:
            if old != new:
                raise BootError("非目标启动项发生变化: index=" + old["index"])
            continue
        if {k: v for k, v in old.items() if k != "args"} != {k: v for k, v in new.items() if k != "args"}:
            raise BootError("目标启动项的非 args 字段发生变化")
        old_args, new_args = arg_tokens(old), arg_tokens(new)
        unrelated = lambda args: [t for t in args if t.split("=", 1)[0] not in settings]
        if unrelated(old_args) != unrelated(new_args):
            raise BootError("无关启动参数发生变化")
        for key, value in settings.items():
            if [t for t in new_args if t.split("=", 1)[0] == key] != [key + "=" + value]:
                raise BootError("参数回读不匹配或仍重复: " + key)


def verify_config_bytes(original, candidate):
    old, new = original.splitlines(keepends=True), candidate.splitlines(keepends=True)
    if len(old) != len(new):
        raise BootError("候选配置行数变化，拒绝安装")
    changed = [(a, b) for a, b in zip(old, new) if a != b]
    if len(changed) != 1 or not all(re.match(rb"^\s*linux(?:efi|16)?\s", line)
                                    for line in changed[0]):
        raise BootError("候选配置不止修改一行 Linux 启动参数，拒绝安装")


def reset_candidate_env(candidate_env, original, before):
    """Discard grubby's equivalent index-to-title rewrite, preserving the real env."""
    def variables(data):
        values = {}
        for line in data.decode("utf-8").splitlines():
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if not sep or key in values:
                raise BootError("候选 grubenv 不可解析")
            values[key] = value
        return values
    old, new = variables(original), variables(candidate_env.read_bytes())
    allowed = {old.get("saved_entry"), before["index"], selected_entry(before).get("title")}
    if (new.get("saved_entry") not in allowed
            or {k: v for k, v in old.items() if k != "saved_entry"}
            != {k: v for k, v in new.items() if k != "saved_entry"}):
        raise BootError("候选操作修改了非等价 grubenv 状态，拒绝安装")
    candidate_env.write_bytes(original)


def discover():
    if platform.machine().lower() not in ("aarch64", "arm64"):
        raise BootError("kvm-arm 启动修改仅适用于 ARM64")
    grubby = shutil.which("grubby")
    checker = shutil.which("grub2-script-check") or shutil.which("grub-script-check")
    if not grubby or not checker:
        raise BootError("需要 grubby 和 grub2-script-check；不支持此引导后端")
    link = Path("/etc/grub2-efi.cfg" if Path("/sys/firmware/efi").exists() else "/etc/grub2.cfg")
    if not link.is_symlink():
        raise BootError("缺少明确的 GRUB 配置符号链接: " + str(link))
    config = link.resolve(strict=True)
    if not str(config).startswith("/boot/") or config.name != "grub.cfg" or not config.is_file():
        raise BootError("GRUB 配置目标不受支持")
    text = config.read_text()
    if re.search(r"^\s*(?:blscfg|configfile)\b", text, re.M) or not re.search(r"^\s*menuentry\s", text, re.M):
        raise BootError("仅支持完整的非 BLS GRUB 配置；拒绝 BLS 或跳转 stub")
    env = config.parent / "grubenv"
    if not env.is_file() or env.is_symlink():
        raise BootError("缺少与 GRUB 配置同目录的常规 grubenv")
    run([checker, str(config)])
    return grubby, checker, config, env


def change(settings, dry_run=False, paths=None, invoke=run):
    """Stage and validate a full candidate before one atomic config replacement."""
    settings = normalize_settings([key + "=" + str(value) for key, value in settings.items()])
    grubby, checker, config, env = paths or discover()
    config, env = Path(config), Path(env)
    original, original_env = config.read_bytes(), env.read_bytes()
    before = inspect_entries(grubby, config, env, invoke)
    entry = selected_entry(before)
    old_args = arg_tokens(entry)
    changes = {key: {"before": [t.partition("=")[2] if "=" in t else "(无值)"
                               for t in old_args if t.split("=", 1)[0] == key], "after": value}
               for key, value in settings.items()}
    result = {"ok": True, "dry_run": dry_run, "config": str(config),
              "kernel": before["kernel"], "index": before["index"], "changes": changes,
              "status": "预览；尚未写入" if dry_run else "待重启后核验",
              "note": "仅默认内核启动项；不修改 /etc/default/grub，不重建 GRUB，不自动重启。"
                      "未来 grub2-mkconfig 可能覆盖此配置；参数写入不代表内核支持或已生效。"}
    if dry_run:
        return result
    if os.geteuid() != 0:
        raise BootError("修改启动项需要 root")
    if all(change["before"] == [change["after"]] for change in changes.values()):
        result["status"] = "默认启动项已是请求值；运行状态需另查"
        return result
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ-")
    backup = Path(tempfile.mkdtemp(prefix=".machconf-kvm-arm-" + stamp, dir=str(config.parent)))
    result["backup"] = str(backup)
    candidate = backup / "candidate.cfg"
    candidate_env = backup / "grubenv"
    installed = False
    candidate_bytes = None
    try:
        shutil.copy2(config, backup / "original.cfg")
        shutil.copy2(config, candidate)
        shutil.copy2(env, candidate_env)
        if (backup / "original.cfg").read_bytes() != original or candidate_env.read_bytes() != original_env:
            raise BootError("备份期间引导配置变化，拒绝继续")
        (backup / "plan.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        base = [grubby, "--grub2", "--config-file=" + str(candidate), "--env=" + str(candidate_env)]
        # grubby 8.40 removes newly added values when both options name the same key.
        # Both steps affect only the isolated candidate, never the live config.
        invoke(base + ["--update-kernel=" + before["kernel"],
                       "--remove-args=" + " ".join(settings)])
        invoke(base + ["--update-kernel=" + before["kernel"],
                       "--args=" + " ".join(k + "=" + v for k, v in settings.items())])
        invoke([checker, str(candidate)])
        verify_change(before, inspect_entries(grubby, candidate, candidate_env, invoke), settings)
        if candidate_env.read_bytes() != original_env:
            reset_candidate_env(candidate_env, original_env, before)
            verify_change(before, inspect_entries(grubby, candidate, candidate_env, invoke), settings)
        if config.read_bytes() != original or env.read_bytes() != original_env:
            raise BootError("检测到并发引导配置修改，拒绝覆盖")
        candidate_bytes = candidate.read_bytes()
        verify_config_bytes(original, candidate_bytes)
        # grubby may replace its output file; reapply the original file mode/owner.
        st = config.stat()
        if (candidate.stat().st_uid, candidate.stat().st_gid) != (st.st_uid, st.st_gid):
            os.chown(candidate, st.st_uid, st.st_gid)
        shutil.copystat(config, candidate)
        with candidate.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(candidate, config)
        installed = True
        sync_directory(config.parent)
        verify_change(before, inspect_entries(grubby, config, env, invoke), settings)
        if env.read_bytes() != original_env:
            raise BootError("回读发现 grubenv 变化")
        result["status"] = "已更新默认启动项并回读验证；待重启后核验"
    except Exception as exc:
        result.update(ok=False, error=str(exc), status="未安装候选配置")
        if installed:
            try:
                if config.read_bytes() == candidate_bytes:
                    restore = backup / "restore.cfg"
                    shutil.copy2(backup / "original.cfg", restore)
                    if (restore.stat().st_uid, restore.stat().st_gid) != (st.st_uid, st.st_gid):
                        os.chown(restore, st.st_uid, st.st_gid)
                    with restore.open("rb") as stream:
                        os.fsync(stream.fileno())
                    os.replace(restore, config)
                    sync_directory(config.parent)
                    result["status"] = "回读失败，已恢复原 GRUB 配置"
                    result["rollback_verified"] = config.read_bytes() == original
                else:
                    result["status"] = "配置发生并发变化，未覆盖回滚；请核对备份"
            except OSError as rollback_error:
                result["status"] = "回滚失败，请使用备份恢复"
                result["rollback_error"] = str(rollback_error)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        settings = json.loads(args.settings)
        if not isinstance(settings, dict):
            raise BootError("settings 必须是对象")
        if args.dry_run:
            result = change(settings, True)
        else:
            if os.geteuid() != 0:
                raise BootError("修改启动项需要 root")
            # Serialize machconf writers across config inode replacements.
            fd = os.open("/run/lock/machconf-kvm-arm.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = change(settings)
    except Exception as exc:
        result = {"ok": False, "error": str(exc), "status": "操作失败；未确认修改成功"}
    # JSON preserves rollback/backup evidence even when a change fails.
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
