"""Configuration-driven systemd/Docker tuning with private recovery snapshots."""

import base64
import datetime
import difflib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import subprocess
import tempfile
import time
import uuid

from .cube_tune_config import validate_config, render_dropin, parse_id_list, render_environment, environment_path, RCU_KEYS
from .cube_tune_verify import inspect_process, verify_process


DROPIN = "zzzz-machconf-tune.conf"
MARKER = "# Managed by machconf cube-tune."
PROPERTIES = ("Id", "LoadState", "ActiveState", "SubState", "MainPID", "FragmentPath",
              "DropInPaths", "NeedDaemonReload", "CPUAffinity", "NUMAPolicy", "NUMAMask",
              "Environment", "EnvironmentFiles", "UnsetEnvironment")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(path):
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("需要无 .. 的绝对路径")
    for p in (path,) + tuple(path.parents):
        if p.is_symlink():
            raise ValueError("拒绝写入符号链接或其子目录: " + str(p))


def file_state(path):
    safe_path(path)
    if not path.exists():
        return {"exists": False}
    st = path.stat()
    if not stat.S_ISREG(st.st_mode):
        raise ValueError("不是常规文件: " + str(path))
    data = path.read_bytes()
    return {"exists": True, "sha256": sha(data), "data": base64.b64encode(data).decode(),
            "mode": stat.S_IMODE(st.st_mode), "uid": st.st_uid, "gid": st.st_gid}


def atomic_write(path, data, mode=0o600, owner=None):
    safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".machconf-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            if owner is not None:
                os.fchown(stream.fileno(), *owner)
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save_state(directory, state):
    atomic_write(directory / "state.json", (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode())


class Host:
    """Injectable OS adapter; root is used only by offline tests."""
    def __init__(self, root="/"):
        self.root = Path(root)

    def path(self, absolute):
        return self.root / absolute.lstrip("/")

    def run(self, args):
        proc = subprocess.run(args, text=True, capture_output=True, timeout=90)
        if proc.returncode:
            # Do not echo complete stderr: systemctl/docker may expose configuration.
            raise RuntimeError("%s %s 执行失败 rc=%s" % (args[0], args[1], proc.returncode))
        return proc.stdout.strip()

    def identity(self):
        return {"machine_id": self.path("/etc/machine-id").read_text().strip(),
                "hostname": socket.gethostname()}

    def topology(self):
        node_path = self.path("/sys/devices/system/node/online")
        return {"cpus": parse_id_list(self.path("/sys/devices/system/cpu/online").read_text().strip()),
                "nodes": parse_id_list(node_path.read_text().strip()) if node_path.exists() else set()}

    def service(self, unit):
        text = self.run(["systemctl", "show", unit, "--property=" + ",".join(PROPERTIES)])
        return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)

    def container(self, name):
        data = json.loads(self.run(["docker", "inspect", "--type=container", name]))
        if not isinstance(data, list) or len(data) != 1:
            raise RuntimeError("容器 inspect 未返回唯一对象")
        item = data[0]
        return {"id": item["Id"], "image": item["Image"], "running": item["State"]["Running"],
                "pid": item["State"]["Pid"], "cpuset_cpus": item["HostConfig"]["CpusetCpus"],
                "cpuset_mems": item["HostConfig"]["CpusetMems"]}

    def process(self, pid):
        return inspect_process(int(pid), proc_root=str(self.path("/proc")))

    def rcu_values(self):
        values = {}
        for key in RCU_KEYS:
            path = self.path("/sys/kernel/" + key)
            safe_path(path)
            text = path.read_text().strip()
            if text not in ("0", "1"):
                raise ValueError("RCU 当前值不是可管理的 0/1，拒绝推断: " + key)
            values[key] = int(text)
        return values

    def rcu_boot_id(self):
        value = self.path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", value):
            raise ValueError("无法确认 RCU 所属启动会话")
        return value

    def rcu_writable(self):
        for key in RCU_KEYS:
            path = self.path("/sys/kernel/" + key)
            safe_path(path)
            if not path.stat().st_mode & 0o222 or not os.access(str(path), os.W_OK):
                raise ValueError("RCU 接口不可写: " + key)

    def write_rcu(self, key, value):
        if key not in RCU_KEYS or type(value) is not int or value not in (0, 1):
            raise ValueError("非法 RCU 写入请求")
        path = self.path("/sys/kernel/" + key)
        safe_path(path)
        # sysfs attributes must be written in place, never replaced/created.
        fd = os.open(str(path), os.O_WRONLY | os.O_NOFOLLOW)
        try:
            if os.write(fd, ("%d\n" % value).encode()) != 2:
                raise RuntimeError("RCU 写入不完整: " + key)
        finally:
            os.close(fd)
        if self.rcu_values()[key] != value:
            raise RuntimeError("RCU 写入回读不匹配: " + key)


def dropin_path(host, service):
    return host.path("/etc/systemd/system/%s.d/%s" % (service["unit"], DROPIN))


def service_files(saved):
    # Write the environment file first, before installing its reference.
    return ([saved["environment_file"]] if saved.get("environment_file") else []) + [saved]


def file_changes(plan):
    return [item for saved in plan["services"] for item in service_files(saved)]


def plan_file(path, content, mode):
    previous = file_state(path)
    old = base64.b64decode(previous["data"]).decode() if previous["exists"] else ""
    if previous["exists"] and not old.startswith(MARKER):
        raise ValueError("同名文件非本工具管理，拒绝覆盖: " + str(path))
    return {"path": str(path), "before": previous, "content": content, "mode": mode,
            "diff": "".join(difflib.unified_diff(old.splitlines(True), content.splitlines(True),
                       fromfile=str(path) + ":before", tofile=str(path) + ":after"))}


def source_hashes(properties):
    paths = ([properties["FragmentPath"]] if properties.get("FragmentPath") else [])
    paths += shlex.split(properties.get("DropInPaths", ""))
    return {p: sha(Path(p).read_bytes()) for p in paths}


def check_sets(config, topology):
    for section, fields in (("services", {"cpu_affinity": "cpus", "numa_nodes": "nodes"}),
                            ("containers", {"cpuset_cpus": "cpus", "cpuset_mems": "nodes"})):
        for item in config[section]:
            for field, available in fields.items():
                if field in item and not parse_id_list(item[field]) <= topology[available]:
                    raise ValueError("%s=%s 包含离线/不存在的 %s" % (field, item[field], available))


def env_values(text, keys):
    return {key: value for token in shlex.split(text) for key, sep, value in [token.partition("=")]
            if sep and key in keys}


def manager_checks(properties, requested):
    checks = []
    for key, prop in (("cpu_affinity", "CPUAffinity"), ("numa_nodes", "NUMAMask")):
        if key in requested:
            value = properties.get(prop, "").replace(" ", ",")
            try:
                matches = parse_id_list(value) == parse_id_list(requested[key])
            except ValueError:
                matches = False
            checks.append({"check": "systemd." + prop, "ok": matches, "detail": value})
    if "numa_policy" in requested:
        checks.append({"check": "systemd.NUMAPolicy", "ok": properties.get("NUMAPolicy") == requested["numa_policy"],
                       "detail": properties.get("NUMAPolicy", "unavailable")})
        if requested["numa_policy"] in ("local", "default"):
            checks.append({"check": "systemd.NUMAMask reset", "ok": not properties.get("NUMAMask"),
                           "detail": properties.get("NUMAMask", "")})
    if requested.get("go_env"):
        files = properties.get("EnvironmentFiles", "")
        last_owned = bool(re.search(re.escape(environment_path(requested)) + r" \(ignore_errors=(?:yes|no)\)\s*$", files))
        checks.append({"check": "systemd.EnvironmentFile precedence", "ok": last_owned,
                       "detail": "受管 Go 环境文件必须最后加载"})
        values = env_values(properties.get("Environment", ""), requested["go_env"])
        for key, value in requested["go_env"].items():
            checks.append({"check": "systemd.env." + key, "ok": values.get(key) == value,
                           "detail": values.get(key, "未设置")})
    return checks


def make_plan(config, host=None):
    host = host or Host()
    config = validate_config(config)
    if config["services"] or config["containers"]:
        check_sets(config, host.topology())
    plan = {"config": config, "identity": host.identity(), "services": [], "containers": [],
            "warnings": ["NUMA policy 是内存分配策略，cpuset.mems 是允许节点；均不保证已有页迁移。",
                         "Go 环境变量不是程序内部实时参数；应用可能通过 runtime/debug API 覆盖。"]}
    for service in config["services"]:
        props = host.service(service["unit"])
        if props.get("LoadState") != "loaded" or props.get("Id") != service["unit"]:
            raise ValueError("服务不存在或名称是别名: " + service["unit"])
        if props.get("NeedDaemonReload") != "no":
            raise ValueError("服务配置与 systemd manager 不一致；先处理 daemon-reload: " + service["unit"])
        if service["restart"] and props.get("ActiveState") != "active":
            raise ValueError("restart=true 仅允许原本 active 的服务: " + service["unit"])
        unset = shlex.split(props.get("UnsetEnvironment", ""))
        if any(token.split("=", 1)[0] in service.get("go_env", {}) for token in unset):
            raise ValueError("UnsetEnvironment 与请求的 Go 参数冲突: " + service["unit"])
        saved = plan_file(dropin_path(host, service), render_dropin(service), 0o644)
        saved.update(request=service, sources=source_hashes(props),
                     before_state={k: props.get(k, "") for k in ("ActiveState", "SubState", "MainPID")})
        env_path = host.path(environment_path(service))
        safe_path(env_path)
        if service.get("go_env") or env_path.exists():
            saved["environment_file"] = plan_file(env_path, render_environment(service), 0o600)
        plan["services"].append(saved)
        if props.get("EnvironmentFiles") or props.get("UnsetEnvironment"):
            plan["warnings"].append(service["unit"] + ": EnvironmentFiles/UnsetEnvironment 可能影响最终进程环境；需要重启后验证。")
        if not service["restart"]:
            plan["warnings"].append(service["unit"] + ": 仅配置并 daemon-reload，待服务后续重启生效。")
    for container in config["containers"]:
        observed = host.container(container["name"])
        if not observed["running"]:
            raise ValueError("容器必须正在运行: " + container["name"])
        plan["containers"].append({"request": container, "before": observed})
    if config["containers"]:
        plan["warnings"].append("Docker update 立即修改现有容器；容器重建后需重新应用配置文件，不修改 Compose/镜像或容器环境变量。")
    if "rcu" in config:
        plan["rcu"] = {"request": config["rcu"], "before": host.rcu_values(), "boot_id": host.rcu_boot_id()}
        plan["warnings"].append("RCU 仅修改本次启动的 sysfs，立即生效于后续相关操作；不写 GRUB/tmpfiles，不重启。normal 优先于 expedited；两者0不表示禁用RCU。")
    return plan


def public_plan(plan):
    result = {"identity": plan["identity"], "warnings": plan["warnings"],
            "services": [{"unit": x["request"]["unit"], "restart": x["request"]["restart"],
                          "path": x["path"], "diff": x["diff"],
                          "files": [{k: f[k] for k in ("path", "diff", "mode")} for f in service_files(x)]}
                         for x in plan["services"]],
            "containers": [{"request": x["request"], "before": x["before"]} for x in plan["containers"]]}
    if "rcu" in plan:
        result["rcu"] = plan["rcu"]
    return result


def rcu_write_order(values):
    # Disable the opposite mode first for supported requests. A saved 1/1
    # baseline is restored with normal first so expedited stays overridden.
    return ("rcu_normal", "rcu_expedited") if values["rcu_expedited"] else ("rcu_expedited", "rcu_normal")


def docker_update(host, identity, settings):
    command = ["docker", "update"]
    for key in ("cpuset_cpus", "cpuset_mems"):
        if key in settings:
            command += ["--" + key.replace("_", "-"), settings[key]]
    host.run(command + [identity])


def inspect_targets(config, host, pending=False, baseline=None):
    reports = []
    if "rcu" in config:
        current = host.rcu_values()
        checks = [{"check": k, "ok": current[k] == config["rcu"][k], "detail": str(current[k])} for k in RCU_KEYS]
        if baseline and "rcu" in baseline:
            checks.append({"check": "RCU boot identity", "ok": host.rcu_boot_id() == baseline["rcu"]["boot_id"],
                           "detail": "RCU 快照仅适用于同一次启动"})
        reports.append({"target": "kernel RCU", "checks": checks, "ok": all(c["ok"] for c in checks)})
    for service in config["services"]:
        props = host.service(service["unit"])
        checks = manager_checks(props, service)
        is_pending = pending and not service["restart"]
        if not is_pending:
            checks.append({"check": "service active", "ok": props.get("ActiveState") == "active", "detail": props.get("ActiveState", "")})
            checks += verify_process(host.process(props.get("MainPID", "0")), service)
            after = host.service(service["unit"])
            checks.append({"check": "systemd process stable", "ok": after.get("MainPID") == props.get("MainPID") and after.get("ActiveState") == "active",
                           "detail": "MainPID/ActiveState checked before and after sampling"})
        reports.append({"target": service["unit"], "pending_restart": is_pending, "checks": checks,
                        "ok": all(c["ok"] for c in checks)})
    for container in config["containers"]:
        current = host.container(container["name"])
        checks = [{"check": "container running", "ok": current["running"], "detail": current["id"]}]
        if baseline is not None:
            old = next(x["before"] for x in baseline["containers"] if x["request"]["name"] == container["name"])
            identity_keys = ("id", "image", "pid") if pending else ("id", "image")
            checks.append({"check": "container identity", "ok": all(current[k] == old[k] for k in identity_keys),
                           "detail": current["id"]})
        for key in ("cpuset_cpus", "cpuset_mems"):
            if key in container:
                try:
                    matches = parse_id_list(current[key]) == parse_id_list(container[key])
                except ValueError:
                    matches = False
                checks.append({"check": "Docker." + key, "ok": matches, "detail": current[key]})
        checks += verify_process(host.process(current["pid"]), container)
        after = host.container(container["name"])
        checks.append({"check": "container process stable", "ok": after["running"] and all(after[k] == current[k] for k in ("id", "image", "pid", "cpuset_cpus", "cpuset_mems")),
                       "detail": "Container identity/PID/config checked before and after sampling"})
        reports.append({"target": container["name"], "checks": checks, "ok": all(c["ok"] for c in checks)})
    return {"ok": all(r["ok"] for r in reports), "targets": reports}


def load_state(directory, host):
    safe_path(directory)
    st = directory.stat()
    if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) & 0o077:
        raise ValueError("状态目录必须归当前用户所有且权限0700")
    state_file = directory / "state.json"
    safe_path(state_file)
    st = state_file.stat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) & 0o077:
        raise ValueError("状态文件必须是当前用户所有的私有常规文件")
    state = json.loads(state_file.read_text())
    allowed = {"version", "status", "plan", "files_started", "containers_started", "restarts_started",
               "verification", "apply_error", "restore_errors", "rcu_started"}
    if not isinstance(state, dict) or set(state) - allowed:
        raise ValueError("快照存在未知字段")
    if type(state.get("version")) is not int or state["version"] != 1 or state["plan"]["identity"]["machine_id"] != host.identity()["machine_id"]:
        raise ValueError("快照版本或机器身份不匹配")
    if state.get("status") not in ("applying", "applied", "restoring", "restored", "restore_failed"):
        raise ValueError("未知快照状态")
    config = validate_config(state["plan"]["config"])
    rcu = state["plan"].get("rcu")
    started = state.get("rcu_started", [])
    if "rcu" in config:
        if not isinstance(rcu, dict) or set(rcu) != {"request", "before", "boot_id"} or rcu["request"] != config["rcu"]:
            raise ValueError("快照 RCU 请求无效")
        if not isinstance(rcu["before"], dict) or set(rcu["before"]) != set(RCU_KEYS) or any(type(v) is not int or v not in (0, 1) for v in rcu["before"].values()):
            raise ValueError("快照 RCU 旧值无效")
        if rcu["boot_id"] != host.rcu_boot_id():
            raise ValueError("RCU 快照所属启动会话已变化，拒绝跨重启恢复/验证")
        if not isinstance(started, list) or any(k not in RCU_KEYS for k in started) or len(started) != len(set(started)):
            raise ValueError("快照 RCU 已开始字段无效")
    elif rcu is not None or started:
        raise ValueError("快照存在未请求的 RCU 操作")
    if len(config["services"]) != len(state["plan"]["services"]) or len(config["containers"]) != len(state["plan"]["containers"]):
        raise ValueError("快照目标列表不一致")
    for service, saved in zip(config["services"], state["plan"]["services"]):
        if saved["path"] != str(dropin_path(host, service)) or saved["content"] != render_dropin(service) or saved["request"] != service or saved.get("mode") != 0o644:
            raise ValueError("快照计划校验不匹配")
        env_file = saved.get("environment_file")
        if service.get("go_env") and not env_file:
            raise ValueError("快照缺少 Go 环境文件")
        if env_file and (env_file["path"] != str(host.path(environment_path(service))) or
                         env_file["content"] != render_environment(service) or env_file.get("mode") != 0o600):
            raise ValueError("快照环境文件校验不匹配")
    for saved in file_changes(state["plan"]):
        old = saved["before"]
        if not isinstance(old, dict) or type(old.get("exists")) is not bool:
            raise ValueError("无效文件备份")
        if old["exists"]:
            if set(old) != {"exists", "sha256", "data", "mode", "uid", "gid"}:
                raise ValueError("无效文件备份字段")
            data = base64.b64decode(old["data"], validate=True)
            if sha(data) != old["sha256"] or not data.decode().startswith(MARKER):
                raise ValueError("备份摘要/归属校验失败")
            if any(type(old[k]) is not int or old[k] < 0 for k in ("mode", "uid", "gid")) or old["mode"] > 0o777:
                raise ValueError("无效文件备份权限")
        elif set(old) != {"exists"}:
            raise ValueError("无效空备份")
    for container, saved in zip(config["containers"], state["plan"]["containers"]):
        if saved["request"] != container:
            raise ValueError("快照容器校验不匹配")
        old = saved["before"]
        for key in ("cpuset_cpus", "cpuset_mems"):
            if not isinstance(old.get(key), str):
                raise ValueError("快照旧容器 cpuset 无效")
            if old[key]:
                parse_id_list(old[key])
        if not isinstance(old.get("id"), str) or not old["id"] or not old["id"].isalnum():
            raise ValueError("快照容器身份无效")
    permitted = {"files_started": {x["path"] for x in file_changes(state["plan"])},
                 "restarts_started": {x["unit"] for x in config["services"] if x["restart"]},
                 "containers_started": {x["before"]["id"] for x in state["plan"]["containers"]}}
    for key, values in permitted.items():
        started = state.get(key)
        if not isinstance(started, list) or any(not isinstance(v, str) for v in started) or len(started) != len(set(started)) or not set(started) <= values:
            raise ValueError("快照已开始目标无效: " + key)
    return state


def check_restore_file(saved, directory):
    current, old = file_state(Path(saved["path"])), saved["before"]
    if current == old:
        return current
    if not current["exists"] or current["sha256"] != sha(saved["content"].encode()):
        raise ValueError("检测到外部文件修改，拒绝覆盖: " + saved["path"])
    if current["mode"] != saved["mode"] or current["uid"] != directory.stat().st_uid:
        raise ValueError("检测到外部文件权限/所有者修改，拒绝覆盖: " + saved["path"])
    return current


def restore(directory, host=None, internal_state=None):
    host = host or Host()
    directory = Path(directory)
    state = internal_state or load_state(directory, host)
    if state["status"] not in ("applying", "applied", "restore_failed", "restoring"):
        raise ValueError("当前快照状态不可恢复: " + state["status"])
    restoring_from = state["status"]
    errors = []
    state["status"] = "restoring"
    save_state(directory, state)
    if state.get("rcu_started"):
        try:
            rcu = state["plan"]["rcu"]
            if host.rcu_boot_id() != rcu["boot_id"]:
                raise ValueError("RCU 启动会话已变化，拒绝恢复")
            current = host.rcu_values()
            host.rcu_writable()
            if restoring_from == "applied" and current not in (rcu["before"], rcu["request"]):
                raise ValueError("已应用的 RCU 组合存在外部修改，拒绝覆盖")
            for key in RCU_KEYS:
                permitted = (rcu["before"][key], rcu["request"][key]) if key in state["rcu_started"] else (rcu["before"][key],)
                if current[key] not in permitted:
                    raise ValueError("RCU 存在外部修改，拒绝覆盖: " + key)
            for key in rcu_write_order(rcu["before"]):
                if key in state["rcu_started"] and current[key] != rcu["before"][key]:
                    host.write_rcu(key, rcu["before"][key])
            if host.rcu_values() != rcu["before"]:
                raise ValueError("RCU 恢复回读失败")
        except Exception as exc:
            errors.append(str(exc))
    restored_files = set()
    candidates = []
    for service in state["plan"]["services"]:
        try:
            # A unit and its referenced environment file are one recovery group.
            # Do not delete an env file still referenced by an externally edited unit.
            for saved in service_files(service):
                if saved["path"] in state["files_started"]:
                    check_restore_file(saved, directory)
            candidates.extend(service_files(service))
        except Exception as exc:
            errors.append(str(exc))
    for saved in candidates:
        if saved["path"] not in state["files_started"]:
            restored_files.add(saved["path"])
            continue
        try:
            path = Path(saved["path"])
            current, old = check_restore_file(saved, directory), saved["before"]
            if current == old:
                restored_files.add(saved["path"])
                continue
            if old["exists"]:
                data = base64.b64decode(old["data"], validate=True)
                if sha(data) != old["sha256"]:
                    raise ValueError("备份摘要校验失败")
                atomic_write(path, data, old["mode"], (old["uid"], old["gid"]))
            else:
                path.unlink()
            if file_state(path) != old:
                raise ValueError("恢复文件回读失败")
            restored_files.add(saved["path"])
        except Exception as exc:
            errors.append(str(exc))
    restored_units = {s["request"]["unit"] for s in state["plan"]["services"]
                      if all(f["path"] in restored_files for f in service_files(s))}
    reload_ok = True
    if state["files_started"]:
        try:
            host.run(["systemctl", "daemon-reload"])
        except Exception as exc:
            errors.append(str(exc))
            reload_ok = False
    for unit in state["restarts_started"]:
        if unit not in restored_units or not reload_ok:
            errors.append("未恢复配置，跳过重启: " + unit)
            continue
        try:
            host.run(["systemctl", "restart", unit])
            if host.service(unit).get("ActiveState") != "active":
                raise RuntimeError("恢复后服务不活跃: " + unit)
        except Exception as exc:
            errors.append(str(exc))
    for saved in state["plan"]["containers"]:
        requested, old = saved["request"], saved["before"]
        if old["id"] not in state["containers_started"]:
            continue
        try:
            current = host.container(requested["name"])
            if current["id"] != old["id"] or current["image"] != old["image"]:
                raise ValueError("容器已被替换，拒绝恢复到新容器: " + requested["name"])
            settings = {key: old[key] for key in ("cpuset_cpus", "cpuset_mems") if key in requested}
            for key in settings:
                if current[key] not in (old[key], requested[key]):
                    raise ValueError("容器配置有外部修改，拒绝覆盖: " + requested["name"])
            docker_update(host, old["id"], settings)
            after = host.container(requested["name"])
            if after["id"] != old["id"] or any(after[k] != v for k, v in settings.items()):
                raise ValueError("容器恢复回读失败")
        except Exception as exc:
            errors.append(str(exc))
    state["status"] = "restore_failed" if errors else "restored"
    state["restore_errors"] = errors
    save_state(directory, state)
    return {"ok": not errors, "status": state["status"], "state_dir": str(directory), "errors": errors,
            "note": "恢复配置、请求的 RCU 运行值及本工具更新的容器字段；未重启的服务仍需重启，既有内存布局不作恢复承诺。"}


def apply(config, state_dir=None, host=None):
    host = host or Host()
    if os.geteuid() != 0:
        raise ValueError("apply 需要目标机 root")
    plan = make_plan(config, host)
    if "rcu" in plan:
        host.rcu_writable()
    directory = Path(state_dir) if state_dir else host.path("/var/lib/machconf/cube-tune/" +
                datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:10])
    safe_path(directory)
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.mkdir(mode=0o700)
    state = {"version": 1, "status": "applying", "plan": plan, "files_started": [],
             "containers_started": [], "restarts_started": [], "rcu_started": []}
    save_state(directory, state)
    try:
        # Complete all drift checks before the first configuration write.
        for saved in file_changes(plan):
            if file_state(Path(saved["path"])) != saved["before"]:
                raise ValueError("drop-in 在规划后变化")
        for saved in plan["services"]:
            props = host.service(saved["request"]["unit"])
            if (props.get("NeedDaemonReload") != "no" or
                    {k: props.get(k, "") for k in saved["before_state"]} != saved["before_state"] or
                    source_hashes(props) != saved["sources"]):
                raise ValueError("服务源配置在规划后变化")
        for saved in plan["containers"]:
            if host.container(saved["request"]["name"]) != saved["before"]:
                raise ValueError("容器在规划后变化")
        if "rcu" in plan:
            rcu = plan["rcu"]
            if host.rcu_boot_id() != rcu["boot_id"] or host.rcu_values() != rcu["before"]:
                raise ValueError("RCU 在规划后变化，拒绝写入")
            for key in rcu_write_order(rcu["request"]):
                if rcu["before"][key] != rcu["request"][key]:
                    state["rcu_started"].append(key)
                    save_state(directory, state)
                    host.write_rcu(key, rcu["request"][key])
        for saved in file_changes(plan):
            state["files_started"].append(saved["path"])
            save_state(directory, state)
            atomic_write(Path(saved["path"]), saved["content"].encode(), saved["mode"])
        if plan["services"]:
            host.run(["systemctl", "daemon-reload"])
        for saved in plan["services"]:
            requested = saved["request"]
            checks = manager_checks(host.service(requested["unit"]), requested)
            if not all(c["ok"] for c in checks):
                raise ValueError("systemd 加载值与请求不符 (可能有高优先级覆盖): " + requested["unit"])
            if requested["restart"]:
                state["restarts_started"].append(requested["unit"])
                save_state(directory, state)
                host.run(["systemctl", "restart", requested["unit"]])
        for saved in plan["containers"]:
            requested, old = saved["request"], saved["before"]
            if host.container(requested["name"]) != old:
                raise ValueError("容器在配置期间发生变化，拒绝更新")
            state["containers_started"].append(old["id"])
            save_state(directory, state)
            docker_update(host, old["id"], requested)
        # Newly started Go programs may still be creating threads. Retry only
        # read-only sampling, never the mutation, for a bounded settling period.
        for attempt in range(5):
            result = inspect_targets(plan["config"], host, pending=True, baseline=plan)
            if result["ok"]:
                break
            if attempt < 4:
                time.sleep(0.25)
        if not result["ok"]:
            state["verification"] = result
            save_state(directory, state)
            raise ValueError("应用后验证失败，见快照 verification")
        state.update(status="applied", verification=result)
        save_state(directory, state)
        return {"ok": True, "status": "已配置；部分服务待重启" if any(not s["restart"] for s in plan["config"]["services"]) else "已应用并验证",
                "state_dir": str(directory), "plan": public_plan(plan), "verification": result}
    except Exception as exc:
        state["apply_error"] = str(exc)
        save_state(directory, state)
        try:
            recovery = restore(directory, host, state)
        except Exception as recovery_error:
            recovery = {"ok": False, "error": str(recovery_error)}
        return {"ok": False, "status": "应用失败，已尝试恢复", "error": str(exc),
                "state_dir": str(directory), "recovery": recovery}


def _execute(payload, host):
    action = payload["action"]
    if action == "plan":
        return {"ok": True, "status": "只读预览，未写入", "plan": public_plan(make_plan(payload["config"], host))}
    if action == "apply":
        return apply(payload["config"], payload.get("state_dir"), host)
    if action == "verify":
        config = payload.get("config")
        baseline = None
        if config is None:
            baseline = load_state(Path(payload["state_dir"]), host)["plan"]
            config = baseline["config"]
        return inspect_targets(validate_config(config), host, baseline=baseline)
    if action == "restore":
        if os.geteuid() != 0:
            raise ValueError("restore 需要目标机 root")
        return restore(payload["state_dir"], host)
    raise ValueError("不支持的操作")


def execute(payload):
    host = Host()
    if payload["action"] not in ("apply", "restore"):
        return _execute(payload, host)
    if os.geteuid() != 0:
        raise ValueError("apply/restore 需要目标机 root")
    lock = host.path("/run/machconf-cube-tune.lock")
    safe_path(lock)
    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("另一 cube-tune apply/restore 正在运行")
        return _execute(payload, host)
    finally:
        os.close(fd)
