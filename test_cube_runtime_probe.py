"""Deterministic tests for the read-only Linux probe; no remote access."""

import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "cube_runtime_probe", Path(__file__).parent / "modules" / "cube_runtime_probe.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class RuntimeProbeTests(unittest.TestCase):
    def test_environment_keeps_resources_and_omits_other_names(self):
        resources = {"GOMAXPROCS": "16", "GOGC": "100", "GOMEMLIMIT": "8GiB",
                     "GODEBUG": "gctrace=1", "WORKER_THREADS": "16",
                     "TOKIO_WORKER_THREADS": "8"}
        unrelated = ("RUST_LOG", "CUBE_API_BIND", "RATE_LIMIT_PER_SEC",
                     "DATABASE_URL", "CUBE_MASTER_ADDR", "CUBE_API_SANDBOX_DOMAIN",
                     "SHELL", "HOME", "PATH", "LOG_LEVEL", "RUST_BACKTRACE",
                     "GOTRACEBACK", "LOG_DIR", "CUBE_MASTER_CONFIG_PATH")
        result = probe._environment(
            ["%s=%s" % item for item in resources.items()] +
            [name + "=irrelevant-secret" for name in unrelated])
        self.assertEqual(result, resources)
        output = json.dumps(result)
        for name in unrelated:
            self.assertNotIn(name, output)
        self.assertNotIn("irrelevant-secret", output)
        self.assertNotIn("<redacted>", output)

    def test_systemd_escaping_redaction_and_unset(self):
        raw = (b'LoadState=loaded\nActiveState=active\n'
               b'Environment="GOMAXPROCS=16" "GOGC=400" "TOKEN=some\\x20secret" '
               b'"GODEBUG=gctrace=1,scheddetail=1"\n'
               b'UnsetEnvironment="TOKEN=some-secret" HOME GOGC "GODEBUG=some-secret"\n'
               b'PassEnvironment=PATH SHELL GOMAXPROCS "GOMEMLIMIT=some-secret"\n')
        with patch.object(probe.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=raw)):
            result = probe._systemd("test.service")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["environment"]["GOMAXPROCS"], "16")
        self.assertNotIn("TOKEN", result["environment"])
        self.assertEqual(result["properties"]["UnsetEnvironment"], ["GOGC", "GODEBUG=<redacted>"])
        self.assertEqual(result["properties"]["PassEnvironment"], ["GOMAXPROCS", "GOMEMLIMIT=<redacted>"])
        for value in ("secret", "TOKEN", "HOME", "PATH", "SHELL"):
            self.assertNotIn(value, json.dumps(result))
        self.assertEqual(probe._words(r'''"TOKEN=a\"b\\c" GOGC=400'''), ['TOKEN=a"b\\c', 'GOGC=400'])

    def test_systemd_requests_only_resource_properties_and_identity(self):
        wanted = {"LoadState": "loaded", "ActiveState": "active", "SubState": "running",
                  "MainPID": "123", "EnvironmentFiles": "/etc/cube-go.env (ignore_errors=no)",
                  "CPUAffinity": "0-7", "AllowedCPUs": "0-7", "EffectiveCPUs": "0-3",
                  "AllowedMemoryNodes": "0", "EffectiveMemoryNodes": "0",
                  "NUMAPolicy": "bind", "NUMAMask": "0", "CPUQuotaPerSecUSec": "2s",
                  "MemoryMax": "8589934592", "LimitNOFILE": "1048576",
                  "ControlGroup": "/system.slice/test.service", "NeedDaemonReload": "no"}
        removed = {"NRestarts": "5", "FragmentPath": "/etc/unrelated.service",
                   "DropInPaths": "/etc/unrelated.conf", "ExecStart": "/bin/sh",
                   "IPAccounting": "no"}
        raw = "".join("%s=%s\n" % item for item in dict(wanted, **removed).items()).encode()
        with patch.object(probe.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=raw)) as run:
            result = probe._systemd("test.service")
        self.assertEqual(result["properties"], wanted)
        requested = next(arg for arg in run.call_args.args[0] if arg.startswith("--property="))
        for key in removed:
            self.assertNotIn(key, requested)
            self.assertNotIn(key, json.dumps(result))

    def test_systemd_bad_quotes_never_echo_secrets(self):
        raw = b'LoadState=loaded\nEnvironment="TOKEN=some-secret\n'
        with patch.object(probe.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=raw)):
            result = probe._systemd("test.service")
        self.assertEqual(result["environment_status"], "unavailable")
        self.assertEqual(result["environment"], {})
        self.assertNotIn("some-secret", json.dumps(result))

    def test_systemd_absent_and_empty_are_distinct(self):
        with patch.object(probe.subprocess, "run", side_effect=FileNotFoundError()):
            result = probe._systemd("test.service")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["environment_status"], "unavailable")
        for state, expected in (("loaded", "ok"), ("not-found", "not-found")):
            raw = ("LoadState=%s\nEnvironment=\n" % state).encode()
            with patch.object(probe.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=raw)):
                result = probe._systemd("test.service")
            self.assertEqual(result["status"], expected)
            self.assertEqual(result["environment_status"], "empty")

    def test_config_numeric_filter_sections_and_units(self):
        with tempfile.TemporaryDirectory() as directory:
            yaml = Path(directory) / "CubeMaster/conf.yaml"
            yaml.parent.mkdir()
            yaml.write_text('metrics:\n  concurrent: 32 # note\ncreate:\n  create_concurrent_limit: "100"\npassword: secret\n  pool_workers: secret\napp_snapshot_timeout_insec: 300\nport: 8080\n')
            toml = Path(directory) / "Cubelet/config/config.toml"
            toml.parent.mkdir(parents=True)
            toml.write_text('[plugins."io.cubelet.flows.v1".create]\ncreation_concurrent_num = 16\nvm_memory_overhead_base = "42Mi"\npassword = "secret"\n')
            results = probe._config_files(directory)
        self.assertEqual(results[0]["items"], [
            {"line": 2, "section": "metrics", "key": "concurrent", "value": "32"},
            {"line": 4, "section": "create", "key": "create_concurrent_limit", "value": "100"}])
        self.assertEqual(results[1]["items"][1]["value"], "42Mi")
        self.assertEqual(results[1]["items"][0]["section"], 'plugins."io.cubelet.flows.v1".create')
        self.assertEqual(results[2]["status"], "missing")
        self.assertNotIn("secret", json.dumps(results))
        self.assertNotIn("app_snapshot_timeout_insec", json.dumps(results))
        self.assertNotIn('"port"', json.dumps(results))

    @staticmethod
    def fake_reader(files):
        def read(path, binary=False):
            if path not in files:
                raise FileNotFoundError()
            return files[path]
        return read

    def test_v1_cpuset_leaf_and_ancestors_remain_separate(self):
        mounts = [{"version": "1", "root": "/", "point": "/sys/fs/cgroup/cpuset", "controllers": ["cpuset"]}]
        files = {"/proc/123/cgroup": "2:cpuset:/svc\n",
                 "/sys/fs/cgroup/cpuset/svc/cpuset.cpus": "96-191",
                 "/sys/fs/cgroup/cpuset/cpuset.cpus": "0-383"}
        with patch.object(probe, "_read", side_effect=self.fake_reader(files)):
            result = probe._cgroups(123, mounts)
        self.assertEqual(result["status"], "ok")
        self.assertEqual([level["values"]["cpuset.cpus"] for level in result["memberships"][0]["levels"]], ["96-191", "0-383"])

    def test_v1_only_resource_memberships_and_controllers(self):
        mounts = [{"version": "1", "root": "/", "point": "/sys/fs/cgroup/cpu",
                   "controllers": ["cpu", "cpuacct"]}]
        files = {"/proc/123/cgroup":
                 "8:net_cls,net_prio:/network\n7:devices:/devices\n6:freezer:/freezer\n"
                 "5:name=systemd:/systemd\n4:cpu,cpuacct:/svc\n",
                 "/sys/fs/cgroup/cpu/svc/cpu.cfs_quota_us": "200000",
                 "/sys/fs/cgroup/cpu/cpu.cfs_quota_us": "100000",
                 "/sys/fs/cgroup/cpu/svc/cpuacct.usage": "123"}
        with patch.object(probe, "_read", side_effect=self.fake_reader(files)) as read:
            result = probe._cgroups(123, mounts)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["memberships"]), 1)
        self.assertEqual(result["memberships"][0]["controllers"], ["cpu"])
        self.assertEqual([level["values"]["cpu.cfs_quota_us"]
                          for level in result["memberships"][0]["levels"]],
                         ["200000", "100000"])
        for value in ("net_cls", "net_prio", "devices", "freezer", "name=systemd", "cpuacct"):
            self.assertNotIn(value, json.dumps(result))
        self.assertFalse(any("cpuacct.usage" in call.args[0] for call in read.call_args_list))

    def test_v2_mount_root_mapping_and_ancestor_cpu_limits(self):
        mounts = [{"version": "2", "root": "/tenant", "point": "/sys/fs/cgroup", "controllers": []}]
        files = {"/proc/123/cgroup": "0::/tenant/svc\n",
                 "/sys/fs/cgroup/svc/cpu.max": "max 100000",
                 "/sys/fs/cgroup/cpu.max": "200000 100000",
                 "/sys/fs/cgroup/svc/cpuset.cpus.effective": "0-3",
                 "/sys/fs/cgroup/memory.max": "104857600"}
        with patch.object(probe, "_read", side_effect=self.fake_reader(files)):
            result = probe._cgroups(123, mounts)
        self.assertEqual(result["status"], "ok")
        levels = result["memberships"][0]["levels"]
        self.assertEqual([level["values"]["cpu.max"] for level in levels], ["max 100000", "200000 100000"])
        self.assertEqual(result["memberships"][0]["mount_root"], "/tenant")

    def test_cgroup_unmapped_namespace_and_permission_denied(self):
        with patch.object(probe, "_read", return_value="0::/../../unreachable\n"):
            result = probe._cgroups(123, [])
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["memberships"][0]["levels"], [])
        with patch.object(probe, "_read", side_effect=PermissionError()):
            result = probe._cgroups(123, [])
        self.assertEqual(result["errors"], ["permission-denied"])

    def process_context(self, starttimes, exe="/bin/cubemaster", read=b"GOGC=400\0TOKEN=secret\0"):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch.object(probe, "_starttime", side_effect=starttimes))
        stack.enter_context(patch.object(probe.os, "readlink", return_value=exe))
        stack.enter_context(patch.object(probe, "_read", return_value=read))
        stack.enter_context(patch.object(probe, "_threads", return_value={"status": "ok"}))
        stack.enter_context(patch.object(probe, "_cgroups", return_value={"status": "ok"}))
        return stack

    def test_pid_reuse_discards_process_snapshot(self):
        with self.process_context([1, 2]):
            result = probe._process(123, {"systemd-mainpid"}, "cubemaster", "/bin/cubemaster", [])
        self.assertEqual(result["identity_status"], "raced")
        self.assertEqual(result["environment"], {})
        self.assertEqual(result["threads"]["status"], "raced")
        self.assertEqual(result["cgroups"]["status"], "raced")

    def test_same_name_alternate_path_and_env_redaction(self):
        with self.process_context([1, 1]):
            result = probe._process(123, {"proc-exe-scan"}, "cubemaster", "/deployment/bin/cubemaster", [])
        self.assertEqual(result["identity_status"], "match")
        self.assertEqual(result["path_status"], "alternate-path")
        self.assertEqual(result["environment"]["GOGC"], "400")
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("TOKEN", json.dumps(result))

    def test_process_missing_or_permission_denied(self):
        for exception, expected in ((FileNotFoundError(), "not-found-or-process-exited"), (PermissionError(), "permission-denied")):
            with patch.object(probe, "_starttime", side_effect=exception):
                result = probe._process(123, {"systemd-mainpid"}, "cubemaster", "/bin/cubemaster", [])
            self.assertEqual(result["identity_status"], "unknown")
            self.assertEqual(result["environment_status"], "unavailable")
            self.assertEqual(result["errors"], ["process-start:" + expected])

    def test_thread_affinity_counts_and_unreadable_threads(self):
        status = "Name:\tcubemaster\nCpus_allowed_list:\t0-383\nMems_allowed_list:\t0-3\n"
        with patch.object(probe.os, "listdir", return_value=["1", "2", "3"]), patch.object(probe, "_read", side_effect=[status, status, PermissionError()]):
            result = probe._threads(123)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["unreadable_count"], 1)
        self.assertEqual(result["affinity_groups"], [{"cpus_allowed_list": "0-383", "mems_allowed_list": "0-3", "count": 2}])


if __name__ == "__main__":
    unittest.main()
