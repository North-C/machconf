"""CubeSandbox 组件版本对比: 目标机上的部署 vs 社区 v0.7.1 离线包.

基准清单在 refs/ 下, 原样取自 cube-perf-bench (`cpb.py release-ref` 生成的
release/v0.7.1-<arch>.json, 及 pins.json 中的容器镜像 ID), 判定口径与
`cpb.py verify-deploy` 一致:
  binary         ELF / 内核 / guest 镜像等, 不一致即部署了不同构建, 计入结论
  script/config  安装器会渲染或修改部分配置, 差异仅列出供参考
  进程           /proc/<pid>/exe 的 SHA256, 可发现启动后磁盘文件被替换
  容器           docker image ID (config digest), 与拉取的仓库/标签无关
"""

import json
import shlex
from pathlib import Path, PurePosixPath

from .base import Item, Module

REFS_DIR = Path(__file__).resolve().parent.parent / "refs"
INDEX_FILE = "cubesandbox-v0.7.1.json"
ARCH_ALIASES = {"aarch64": "aarch64", "arm64": "aarch64", "x86_64": "x86_64", "amd64": "x86_64"}
HEREDOC_END = "@@MCEOF@@"
LIST_LIMIT = 5

# 目标机上逐个路径输出: F|路径|sha256  L|路径|链接目标  M|路径| (不存在)  E|路径| (不可读)
_FILES_CMD = """\
( if cd %(root)s 2>/dev/null; then
  while IFS= read -r p; do
    if [ -L "$p" ]; then printf 'L|%%s|%%s\\n' "$p" "$(readlink "$p")"
    elif [ ! -f "$p" ]; then printf 'M|%%s|\\n' "$p"
    elif s=$(sha256sum < "$p" 2>/dev/null); then printf 'F|%%s|%%s\\n' "$p" "${s%%%% *}"
    else printf 'E|%%s|\\n' "$p"; fi
  done <<'%(end)s'
%(paths)s
%(end)s
else echo NOROOT; fi ) 2>/dev/null || true"""

# 运行中的基准二进制: pid|sha256|exe; 同一 exe 只算一次 (sandbox 多时 shim 进程很多),
# 已删除的 exe 每个进程单独计算
_PROCS_CMD = """\
if [ -d /proc/self ]; then
  declare -A mc_sha
  for d in /proc/[0-9]*; do
    t=$(readlink "$d/exe" 2>/dev/null) || continue
    b=${t%%" (deleted)"}; b=${b##*/}
    case "$b" in %(names)s) ;; *) continue ;; esac
    k=$t; case "$t" in *" (deleted)") k="$t ${d#/proc/}" ;; esac
    if [ -z "${mc_sha[$k]+x}" ]; then
      s=$(sha256sum < "$d/exe" 2>/dev/null) || s=""
      mc_sha[$k]=${s%%%% *}
    fi
    [ -n "${mc_sha[$k]}" ] && printf '%%s|%%s|%%s\\n' "${d#/proc/}" "${mc_sha[$k]}" "$t"
  done 2>/dev/null
fi
true"""

# C|/名称|image ID|镜像引用|状态  I|仓库|标签|digest|image ID; 无 docker 输出 NODOCKER
_DOCKER_CMD = """\
T=""; command -v timeout >/dev/null 2>&1 && T="timeout 20"
if command -v docker >/dev/null 2>&1 && $T docker info --format '{{.ID}}' >/dev/null 2>&1; then
  $T docker inspect --type container \
    --format 'C|{{.Name}}|{{.Image}}|{{.Config.Image}}|{{.State.Status}}' %(names)s 2>/dev/null
  $T docker images --no-trunc --digests \
    --format 'I|{{.Repository}}|{{.Tag}}|{{.Digest}}|{{.ID}}' 2>/dev/null
else echo NODOCKER; fi
true"""


def _short(digest, n=19):
    return (digest or "?")[:n] + "…"


def _pids(pids):
    if len(pids) <= 3:
        return "pid " + ",".join(map(str, pids))
    return "pid %s… 共 %d 个" % (",".join(map(str, pids[:3])), len(pids))


def _listing(paths):
    head = ", ".join(paths[:LIST_LIMIT])
    return head + (" 等 %d 项" % len(paths) if len(paths) > LIST_LIMIT else "")


def component_of(path):
    """顶层目录, 如 Cubelet/bin/cubelet -> Cubelet."""
    parts = PurePosixPath(path).parts
    return parts[0] if len(parts) > 1 else "(root)"


class CubeSandboxModule(Module):
    name = "cube"
    title = "CubeSandbox 组件版本"
    group = "CubeSandbox 组件 (对比社区 v0.7.1)"
    timeout = 180  # 需对约 700MB 安装文件计算 SHA256

    def __init__(self, root=None, refs_dir=REFS_DIR):
        self.root = root  # None: 用基准清单中的安装目录
        self.refs_dir = Path(refs_dir)
        self._index = None
        self._refs = {}

    # ---------- 基准清单 ----------

    def index(self):
        if self._index is None:
            self._index = json.loads((self.refs_dir / INDEX_FILE).read_text())
        return self._index

    def reference(self, arch):
        if arch not in self._refs:
            name = self.index()["arch_references"][arch]
            self._refs[arch] = json.loads((self.refs_dir / name).read_text())
        return self._refs[arch]

    def references(self):
        return [self.reference(arch) for arch in sorted(self.index()["arch_references"])]

    def install_root(self):
        return (self.root or self.references()[0]["install_root"]).rstrip("/") or "/"

    # ---------- 探测 ----------

    def probes(self):
        refs = self.references()
        paths = sorted({p for ref in refs for p in list(ref["files"]) + list(ref["symlinks"])})
        names = sorted({PurePosixPath(p).name for ref in refs
                        for p, f in ref["files"].items() if f["kind"] == "binary"})
        root = shlex.quote(self.install_root())
        return [
            ("cube_arch", "uname -m 2>/dev/null || true"),
            ("cube_uid", "id -u 2>/dev/null || true"),
            ("cube_files", _FILES_CMD % {"root": root, "paths": "\n".join(paths),
                                         "end": HEREDOC_END}),
            ("cube_version", "cat %s/VERSION.txt 2>/dev/null || true" % root),
            ("cube_manifest", "cat %s/release-manifest.json 2>/dev/null || true" % root),
            ("cube_procs", _PROCS_CMD % {"names": "|".join(names)}),
            ("cube_docker", _DOCKER_CMD % {
                "names": " ".join(sorted(self.index()["containers"]))}),
        ]

    # ---------- 对比 ----------

    def parse(self, sections):
        arch_raw = sections.get("cube_arch", "").strip()
        arch = ARCH_ALIASES.get(arch_raw)
        if arch not in self.index()["arch_references"]:
            return [Item("CubeSandbox 结论", "N.A.",
                         "社区基准无 %s 架构 (支持 aarch64 / x86_64)" % (arch_raw or "未知"))]
        ref = self.reference(arch)
        root = self.install_root()
        observed = sections.get("cube_files", "").strip()
        if not observed or observed == "NOROOT":
            return [Item("CubeSandbox 结论", "N.A.", "未部署 CubeSandbox (%s 不存在)" % root)]

        files = compare_files(ref, parse_files(observed))
        processes = compare_processes(ref, root, files, sections.get("cube_procs", ""))
        containers = compare_containers(self.index(), arch,
                                        parse_docker(sections.get("cube_docker", "")))
        is_root = sections.get("cube_uid", "").strip() == "0"

        items = [verdict_item(self.index(), files, processes, containers),
                 Item("CubeSandbox 对比基准",
                      "社区 %s (%s)" % (self.index()["release_version"], arch),
                      "%s, commit %s" % (ref["bundle"]["name"], ref["git_commit"][:12])),
                 Item("CubeSandbox 安装目录", root),
                 version_item(ref, sections.get("cube_manifest", ""),
                              sections.get("cube_version", ""))]
        items += component_items(files)
        items += process_items(processes, is_root)
        items += container_items(containers)
        return items


def parse_files(text):
    """{路径: (类型 F/L/M/E, 值)}."""
    out = {}
    for line in text.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3 and parts[0] in ("F", "L", "M", "E"):
            out[parts[1]] = (parts[0], parts[2])
    return out


def compare_files(ref, observed):
    """对每个基准路径给出 status: match / differ / missing / unreadable."""
    results = []
    for rel, want in ref["files"].items():
        kind, value = observed.get(rel, ("M", ""))
        status = {"F": "match" if value == want["sha256"] else "differ",
                  "E": "unreadable"}.get(kind, "missing")  # 软链接占位视为缺失
        results.append({"path": rel, "component": component_of(rel), "kind": want["kind"],
                        "status": status, "want": want["sha256"], "have": value or None})
    for rel, target in ref["symlinks"].items():
        kind, value = observed.get(rel, ("M", ""))
        status = ("match" if value == target else "differ") if kind == "L" else \
            ("differ" if kind in ("F", "E") else "missing")
        results.append({"path": rel, "component": component_of(rel), "kind": "symlink",
                        "status": status, "want": target, "have": value or None})
    return results


def compare_processes(ref, root, files, text):
    """{基准二进制路径: [{exe, sha256, deleted, pids, matches_release}]}.

    exe 在安装目录下按相对路径对应; 装在别处时按文件名对应 (文件名在基准中唯一时).
    """
    binaries = {r["path"]: r for r in files if r["kind"] == "binary"}
    by_name = {}
    for rel in binaries:
        by_name.setdefault(PurePosixPath(rel).name, []).append(rel)
    grouped = {}
    for line in text.splitlines():
        parts = line.split("|", 2)
        if len(parts) != 3 or not parts[0].isdigit():
            continue
        pid, sha, target = int(parts[0]), parts[1], parts[2]
        deleted = target.endswith(" (deleted)")
        exe = target[:-len(" (deleted)")] if deleted else target
        rel = None
        if exe.startswith(root.rstrip("/") + "/"):
            rel = exe[len(root.rstrip("/")) + 1:]
        elif len(by_name.get(PurePosixPath(exe).name, [])) == 1:
            rel = by_name[PurePosixPath(exe).name][0]
        if rel not in binaries:
            continue
        grouped.setdefault(rel, {}).setdefault((exe, sha, deleted), []).append(pid)
    return {rel: [{"exe": exe, "sha256": sha, "deleted": deleted, "pids": sorted(pids),
                   "matches_release": sha == binaries[rel]["want"]}
                  for (exe, sha, deleted), pids in sorted(entries.items())]
            for rel, entries in sorted(grouped.items())}


def parse_docker(text):
    """{"containers": {名称: {...}}, "images": [...]}; 无 docker 返回 None."""
    if not text.strip() or text.strip() == "NODOCKER":
        return None
    containers, images = {}, []
    for line in text.splitlines():
        parts = line.split("|")
        if parts[0] == "C" and len(parts) == 5:
            containers[parts[1].lstrip("/")] = {"image_id": parts[2], "image": parts[3],
                                                "state": parts[4]}
        elif parts[0] == "I" and len(parts) == 5:
            images.append({"repository": parts[1], "tag": parts[2], "digest": parts[3],
                           "image_id": parts[4]})
    return {"containers": containers, "images": images}


def compare_containers(index, arch, state):
    """按 image ID 对比各容器组件 (status 同 cpb.py verify-deploy)."""
    out = []
    for name, spec in sorted(index["containers"].items()):
        want = spec["image_id"].get(arch)
        entry = {"component": name, "want_image_id": want,
                 "reference": "%s/%s:%s" % (index["registries"][0], spec["repository"],
                                            index["tag"])}
        if state is None:
            out.append(dict(entry, status="no-docker"))
            continue
        ctr = state["containers"].get(name)
        local = [i for i in state["images"]
                 if i["repository"].endswith("/" + spec["repository"]) or i["image_id"] == want]
        entry["local_images"] = local
        if ctr:
            entry["container"] = ctr
            status = "match" if ctr["image_id"] == want else "differ"
        elif any(i["image_id"] == want for i in local):
            status = "image-only"
        elif local:
            status = "image-differ"
        else:
            status = "not-found"
        out.append(dict(entry, status=status))
    return out


# ---------- 结果条目 ----------

def verdict_item(index, files, processes, containers):
    version = index["release_version"]
    bad_bin = [r for r in files if r["kind"] == "binary" and r["status"] != "match"]
    bad_proc = [e for es in processes.values() for e in es if not e["matches_release"]]
    bad_ctr = [c for c in containers if c["status"] == "differ"]
    others = [r for r in files if r["kind"] != "binary" and r["status"] != "match"]
    notes = []
    if bad_bin or bad_proc or bad_ctr:
        value = "不一致"
        notes.append("与社区 %s 不同: 二进制 %d 个, 运行进程 %d 个, 容器 %d 个"
                     % (version, len(bad_bin), len(bad_proc), len(bad_ctr)))
    else:
        value = "一致"
        notes.append("二进制、运行进程与容器镜像均与社区 %s 相同" % version)
    if any(c["status"] == "no-docker" for c in containers):
        notes.append("docker 不可用, 容器未核对")
    if others:
        notes.append("脚本/配置 %d 项不同 (安装器会渲染部分配置, 不计入结论)" % len(others))
    return Item("CubeSandbox 结论", value, "; ".join(notes))


def version_item(ref, manifest_text, version_text):
    version_lines = [line.strip() for line in version_text.splitlines() if line.strip()]
    try:
        manifest = json.loads(manifest_text)
    except ValueError:
        manifest = None
    if not isinstance(manifest, dict):
        if version_lines:
            return Item("CubeSandbox 部署版本", version_lines[0],
                        "release-manifest.json 缺失或无法解析")
        return Item("CubeSandbox 部署版本", "N.A.", "VERSION.txt / release-manifest.json 缺失")
    version, commit = manifest.get("release_version") or "?", manifest.get("git_commit") or "?"
    same = version == ref["release_version"] and commit == ref["git_commit"]
    note = "与基准相同" if same else "基准为 %s (commit %s)" % (
        ref["release_version"], ref["git_commit"][:12])
    if version_lines:
        note += "; VERSION.txt: " + " / ".join(version_lines)
    return Item("CubeSandbox 部署版本", "%s (commit %s)" % (version, commit[:12]), note)


def component_items(files):
    by_comp = {}
    for r in files:
        by_comp.setdefault(r["component"], []).append(r)
    items = []
    for comp, rows in sorted(by_comp.items()):
        if comp == "(root)":  # VERSION.txt 等, 已在 CubeSandbox 部署版本 中体现
            continue
        binaries = [r for r in rows if r["kind"] == "binary"]
        others = [r for r in rows if r["kind"] != "binary"]
        bad_other = [r for r in others if r["status"] != "match"]

        def local(r):
            return r["path"][len(comp) + 1:]

        notes = []
        if binaries:
            ok = sum(r["status"] == "match" for r in binaries)
            if ok == len(binaries):
                value = "一致 (二进制 %d/%d)" % (ok, len(binaries))
            else:
                value = "不一致 (二进制 %d/%d 相同)" % (ok, len(binaries))
                for status, label in (("differ", "不同"), ("missing", "缺失"),
                                      ("unreadable", "无法读取")):
                    hit = [local(r) for r in binaries if r["status"] == status]
                    if hit:
                        notes.append("%s: %s" % (label, _listing(hit)))
        elif all(r["status"] == "missing" for r in others):
            value = "未部署 (%d 项均缺失)" % len(others)
            bad_other = []
        else:
            ok = len(others) - len(bad_other)
            value = ("一致" if not bad_other else "有差异") + " (脚本/配置 %d/%d)" % (
                ok, len(others))
        if bad_other:
            notes.append("脚本/配置 %d 项不同, 仅供参考: %s"
                         % (len(bad_other), _listing([local(r) for r in bad_other])))
        items.append(Item("组件 %s" % comp, value, "; ".join(notes)))
    return items


def process_items(processes, is_root):
    if not processes:
        if is_root:
            return [Item("CubeSandbox 运行进程", "无", "未发现运行中的基准二进制")]
        return [Item("CubeSandbox 运行进程", "N.A.",
                     "非 root 无法读取其它用户进程的 /proc/<pid>/exe")]
    items = []
    for rel, entries in processes.items():
        pids = sorted(p for e in entries for p in e["pids"])
        bad = [e for e in entries if not e["matches_release"]]
        notes = []
        for e in bad:
            notes.append("%s %s 运行 sha256 %s%s" % (
                _pids(e["pids"]), e["exe"], _short(e["sha256"], 16),
                " (磁盘文件已删除)" if e["deleted"] else ""))
        if not bad:
            exes = sorted({e["exe"] for e in entries})
            notes.append("exe " + ", ".join(exes))
            if any(e["deleted"] for e in entries):
                notes.append("磁盘文件已删除")
        value = ("一致" if not bad else "不一致") + " (%s)" % _pids(pids)
        items.append(Item("进程 %s" % rel, value, "; ".join(notes)))
    return items


def container_items(containers):
    items = []
    for c in containers:
        want = _short(c["want_image_id"])
        status = c["status"]
        if status in ("match", "differ"):
            ctr = c["container"]
            value = ("一致" if status == "match" else "不一致") + " (容器 %s)" % ctr["state"]
            note = "%s, image %s" % (ctr["image"], _short(ctr["image_id"]))
            if status == "differ":
                note += ", 基准 %s" % want
            extra = [i for i in c["local_images"] if i["image_id"] != ctr["image_id"]]
            if extra:
                note += "; 本地另有 %d 个其它构建的镜像" % len(extra)
        elif status == "image-only":
            value, note = "无容器", "本地有基准镜像 %s" % want
        elif status == "image-differ":
            value = "无容器"
            note = "本地镜像与基准 %s 不同: %s" % (want, _listing(
                ["%s:%s" % (i["repository"], i["tag"]) for i in c["local_images"]]))
        elif status == "not-found":
            value, note = "未部署", "期望 %s (image %s)" % (c["reference"], want)
        else:
            value, note = "N.A.", "docker 不可用; 期望 %s (image %s)" % (c["reference"], want)
        items.append(Item("容器 %s" % c["component"], value, note))
    return items
