"""Offline transaction tests using a temporary filesystem and fake OS adapter."""

import copy
import json
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

from modules import cube_tune_backend as backend


class FakeHost(backend.Host):
    def __init__(self, root):
        super().__init__(root)
        self.calls = []
        self.process_calls = []
        self.fail_once = None
        self.machine = {"machine_id": "offline-test-machine", "hostname": "offline"}
        self.fragment = self.path("/usr/lib/systemd/system/CubeMaster.service")
        self.fragment.parent.mkdir(parents=True)
        self.fragment.write_text("[Service]\nExecStart=/usr/bin/example\n")
        self.original = {
            "Id": "CubeMaster.service", "LoadState": "loaded", "ActiveState": "active",
            "SubState": "running", "MainPID": "101", "FragmentPath": str(self.fragment),
            "DropInPaths": "", "NeedDaemonReload": "no", "CPUAffinity": "0-7",
            "NUMAPolicy": "default", "NUMAMask": "", "Environment": "GOGC=100",
            "EnvironmentFiles": "", "UnsetEnvironment": "",
        }
        self.loaded = dict(self.original)
        self.runtime = dict(self.loaded)
        self.runtime_env = self._effective_env(self.runtime)
        self.containers = {"cube-proxy": {
            "id": "a" * 64, "image": "sha256:example", "running": True,
            "pid": 201, "cpuset_cpus": "", "cpuset_mems": "",
        }}

    def identity(self):
        return dict(self.machine)

    def topology(self):
        return {"cpus": set(range(8)), "nodes": {0, 1}}

    def service(self, unit):
        result = dict(self.loaded)
        if unit != result["Id"]:
            result.update(Id=unit, LoadState="not-found")
        return result

    def container(self, name):
        return dict(self.containers[name])

    def _effective_env(self, properties):
        keys = ("GOGC", "GOMAXPROCS", "GOMEMLIMIT", "GODEBUG", "GOTRACEBACK")
        env = backend.env_values(properties["Environment"], keys)
        # Fixture subset of systemctl's EnvironmentFiles representation. Each
        # file overrides Environment= and earlier files, just as systemd does.
        for token in shlex.split(properties.get("EnvironmentFiles", "")):
            if token.startswith("(ignore_errors="):
                continue
            optional = token.startswith("-")
            logical = token.lstrip("-")
            path = Path(logical) if logical.startswith(str(self.root) + "/") else self.path(logical)
            if optional and not path.exists():
                continue
            for line in path.read_text().splitlines():
                if line.strip() and not line.lstrip().startswith("#"):
                    env.update(backend.env_values(line, keys))
        return env

    def _reload(self):
        self.loaded = dict(self.original)
        path = backend.dropin_path(self, {"unit": self.original["Id"]})
        if not path.exists():
            return
        self.loaded["DropInPaths"] = str(path)
        env = backend.env_values(self.loaded["Environment"], ("GOGC", "GOMAXPROCS", "GOMEMLIMIT", "GODEBUG", "GOTRACEBACK"))
        for line in path.read_text().splitlines():
            key, sep, value = line.partition("=")
            if not sep:
                continue
            if key == "Environment":
                env.update(backend.env_values(value, ("GOGC", "GOMAXPROCS", "GOMEMLIMIT", "GODEBUG", "GOTRACEBACK")))
            elif key == "EnvironmentFile":
                if value:
                    entry = shlex.quote(value) + " (ignore_errors=no)"
                    self.loaded["EnvironmentFiles"] = (self.loaded["EnvironmentFiles"] + " " + entry).strip()
                else:
                    self.loaded["EnvironmentFiles"] = ""
            elif key in ("CPUAffinity", "NUMAPolicy", "NUMAMask"):
                self.loaded[key] = value
        self.loaded["Environment"] = " ".join(shlex.quote(key + "=" + value) for key, value in env.items())

    def run(self, args):
        self.calls.append(list(args))
        if self.fail_once and tuple(args[:len(self.fail_once)]) == self.fail_once:
            self.fail_once = None
            raise RuntimeError("injected command failure")
        if args == ["systemctl", "daemon-reload"]:
            self._reload()
        elif args[:2] == ["systemctl", "restart"]:
            self.runtime = dict(self.loaded)
            self.runtime_env = self._effective_env(self.runtime)
        elif args[:2] == ["docker", "update"]:
            current = next(item for item in self.containers.values() if item["id"] == args[-1])
            for index in range(2, len(args) - 1, 2):
                current[args[index][2:].replace("-", "_")] = args[index + 1]
        else:
            raise AssertionError("unexpected host command: {!r}".format(args))
        return ""

    def process(self, pid):
        self.process_calls.append(int(pid))
        if int(pid) == 101:
            cpus = self.runtime["CPUAffinity"].replace(" ", ",")
            mems = "0-1"
            env = dict(self.runtime_env)
            policy = self.runtime["NUMAPolicy"]
            if self.runtime["NUMAMask"]:
                policy += ":" + self.runtime["NUMAMask"].replace(" ", ",")
        else:
            current = next(item for item in self.containers.values() if item["pid"] == int(pid))
            cpus, mems = current["cpuset_cpus"] or "0-7", current["cpuset_mems"] or "0-1"
            env, policy = {}, "default"
        return {"identity": {"status": "ok"},
                "threads": {"status": "ok", "items": [{"status": "ok", "cpus": cpus, "mems": mems}]},
                "go_env_status": "ok", "go_env": env,
                "numa_maps": {"status": "ok", "policies": {policy: 1}}}


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.host = FakeHost(self.root)
        self.state_dir = self.root / "snapshot"

    def config(self, restart=False, containers=False):
        result = {"version": 1, "services": [{
            "unit": "CubeMaster.service", "runtime": "go", "go_env": {"GOGC": "400"},
            "cpu_affinity": "2-3", "numa_policy": "bind", "numa_nodes": "1", "restart": restart,
        }]}
        if containers:
            result["containers"] = [{"name": "cube-proxy", "cpuset_cpus": "4-5", "cpuset_mems": "1"}]
        return result

    def apply(self, config=None):
        with patch.object(backend.os, "geteuid", return_value=0):
            return backend.apply(config or self.config(), self.state_dir, self.host)

    def state(self):
        return json.loads((self.state_dir / "state.json").read_text())

    def save(self, value):
        (self.state_dir / "state.json").write_text(json.dumps(value))

    def managed_env_path(self):
        return self.host.path("/etc/systemd/system/CubeMaster.service.d/zzzz-machconf-tune.env")

    def install_external_env(self):
        path = self.host.path("/etc/cubesandbox/master.env")
        path.parent.mkdir(parents=True)
        path.write_text("GOGC=175\nGOMAXPROCS=8\nTOKEN=keep-private\n")
        self.host.original["EnvironmentFiles"] = str(path) + " (ignore_errors=no)"
        self.host._reload()
        self.host.runtime = dict(self.host.loaded)
        self.host.runtime_env = self.host._effective_env(self.host.runtime)
        return path

    def test_owned_env_overrides_external_file_and_restore_restores_effective_environment(self):
        external = self.install_external_env()
        original = external.read_bytes()
        self.assertEqual(self.host.process(101)["go_env"]["GOGC"], "175")
        result = self.apply(self.config(restart=True))
        self.assertTrue(result["ok"], result)
        managed = self.managed_env_path()
        self.assertEqual(self.host.process(101)["go_env"]["GOGC"], "400")
        self.assertEqual(self.host.process(101)["go_env"]["GOMAXPROCS"], "8")
        self.assertIn("GOGC=400", managed.read_text())
        self.assertIn("zzzz-machconf-tune.env", self.host.loaded["EnvironmentFiles"])
        self.assertEqual(external.read_bytes(), original)
        restored = backend.restore(self.state_dir, self.host)
        self.assertTrue(restored["ok"], restored)
        self.assertFalse(managed.exists())
        self.assertEqual(self.host.process(101)["go_env"]["GOGC"], "175")
        self.assertEqual(external.read_bytes(), original)

    def test_removing_go_env_clears_owned_env_and_drops_reference(self):
        self.install_external_env()
        self.assertTrue(self.apply(self.config(restart=True))["ok"])
        before = self.managed_env_path().read_bytes()
        previous_snapshot = self.state_dir
        self.state_dir = self.root / "snapshot-second"
        config = self.config(restart=True)
        del config["services"][0]["go_env"]
        result = self.apply(config)
        self.assertTrue(result["ok"], result)
        cleared = self.managed_env_path().read_text()
        self.assertTrue(cleared.startswith(backend.MARKER))
        self.assertNotIn("GOGC=", cleared)
        self.assertNotIn("zzzz-machconf-tune.env", self.host.loaded["EnvironmentFiles"])
        self.assertEqual(self.host.process(101)["go_env"]["GOGC"], "175")
        self.assertTrue(backend.restore(self.state_dir, self.host)["ok"])
        self.assertEqual(self.managed_env_path().read_bytes(), before)
        self.assertEqual(self.host.process(101)["go_env"]["GOGC"], "400")
        self.assertTrue(backend.restore(previous_snapshot, self.host)["ok"])
        self.assertEqual(self.host.process(101)["go_env"]["GOGC"], "175")

    def test_owned_env_drift_aborts_restore_without_restarting_service(self):
        self.assertTrue(self.apply(self.config(restart=True))["ok"])
        path = self.managed_env_path()
        external_edit = "# edited independently\nGOGC=900\n"
        path.write_text(external_edit)
        self.host.calls.clear()
        result = backend.restore(self.state_dir, self.host)
        self.assertFalse(result["ok"])
        self.assertEqual(path.read_text(), external_edit)
        self.assertNotIn(["systemctl", "restart", "CubeMaster.service"], self.host.calls)
        self.assertEqual(self.host.process(101)["go_env"]["GOGC"], "400")

    def test_owned_env_drift_between_plan_and_apply_is_not_overwritten(self):
        self.assertTrue(self.apply()["ok"])
        self.state_dir = self.root / "snapshot-second"
        real_plan = backend.make_plan
        external_edit = self.managed_env_path().read_text() + "# concurrent change\n"
        def plan_then_change(config, host):
            plan = real_plan(config, host)
            self.managed_env_path().write_text(external_edit)
            return plan
        self.host.calls.clear()
        with patch.object(backend, "make_plan", side_effect=plan_then_change):
            result = self.apply()
        self.assertFalse(result["ok"])
        self.assertEqual(self.managed_env_path().read_text(), external_edit)
        self.assertEqual(self.host.calls, [])

    def test_failure_restores_both_owned_files_and_external_effective_environment(self):
        external = self.install_external_env()
        original = external.read_bytes()
        self.assertTrue(self.apply(self.config(restart=True))["ok"])
        dropin = backend.dropin_path(self.host, {"unit": "CubeMaster.service"})
        env = self.managed_env_path()
        old_dropin, old_env = backend.file_state(dropin), backend.file_state(env)
        self.state_dir = self.root / "snapshot-second"
        config = self.config(restart=True, containers=True)
        config["services"][0]["go_env"]["GOGC"] = "800"
        self.host.fail_once = ("docker", "update")
        result = self.apply(config)
        self.assertFalse(result["ok"])
        self.assertTrue(result["recovery"]["ok"], result)
        self.assertEqual(backend.file_state(dropin), old_dropin)
        self.assertEqual(backend.file_state(env), old_env)
        self.assertEqual(self.host.process(101)["go_env"]["GOGC"], "400")
        self.assertEqual(external.read_bytes(), original)

    def test_plan_is_read_only(self):
        before = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        plan = backend.make_plan(self.config(containers=True), self.host)
        after = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(self.host.calls, [])
        self.assertFalse(self.state_dir.exists())
        self.assertIn('+Environment="GOGC=400"', plan["services"][0]["diff"])

    def test_apply_without_restart_is_pending_and_does_not_inspect_stale_process(self):
        result = self.apply()
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["verification"]["targets"][0]["pending_restart"])
        self.assertEqual(self.host.calls, [["systemctl", "daemon-reload"]])
        self.assertEqual(self.host.process_calls, [])
        self.assertEqual(self.host.runtime["Environment"], "GOGC=100")
        self.assertEqual(self.state()["status"], "applied")
        self.assertEqual(self.state_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.state_dir / "state.json").stat().st_mode & 0o777, 0o600)

    def test_explicit_restart_and_runtime_verification(self):
        result = self.apply(self.config(restart=True))
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.host.calls, [["systemctl", "daemon-reload"], ["systemctl", "restart", "CubeMaster.service"]])
        self.assertEqual(self.host.process_calls, [101])
        self.assertFalse(result["verification"]["targets"][0]["pending_restart"])

    def test_docker_update_and_restore_preserve_unspecified_fields(self):
        original = self.host.container("cube-proxy")
        result = self.apply({"version": 1, "containers": [{"name": "cube-proxy", "cpuset_cpus": "4-5"}]})
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.host.containers["cube-proxy"]["cpuset_mems"], "")
        self.assertEqual(self.host.calls, [["docker", "update", "--cpuset-cpus", "4-5", original["id"]]])
        restored = backend.restore(self.state_dir, self.host)
        self.assertTrue(restored["ok"], restored)
        self.assertEqual(self.host.container("cube-proxy"), original)

    def test_failure_after_service_restart_restores_files_runtime_and_containers(self):
        self.host.fail_once = ("docker", "update")
        original = self.host.container("cube-proxy")
        result = self.apply(self.config(restart=True, containers=True))
        self.assertFalse(result["ok"])
        self.assertTrue(result["recovery"]["ok"], result)
        self.assertFalse(backend.dropin_path(self.host, {"unit": "CubeMaster.service"}).exists())
        self.assertFalse(self.managed_env_path().exists())
        self.assertEqual(self.host.runtime["Environment"], "GOGC=100")
        self.assertEqual(self.host.container("cube-proxy"), original)
        self.assertEqual(self.state()["status"], "restored")
        self.assertEqual(self.host.calls.count(["systemctl", "restart", "CubeMaster.service"]), 2)

    def test_daemon_reload_failure_restores_created_file(self):
        self.host.fail_once = ("systemctl", "daemon-reload")
        result = self.apply()
        self.assertFalse(result["ok"])
        self.assertTrue(result["recovery"]["ok"], result)
        self.assertFalse(backend.dropin_path(self.host, {"unit": "CubeMaster.service"}).exists())

    def test_restore_recovers_previous_owned_dropin_bytes_and_mode(self):
        path = backend.dropin_path(self.host, {"unit": "CubeMaster.service"})
        path.parent.mkdir(parents=True)
        path.write_text(backend.MARKER + "\n[Service]\nCPUAffinity=\nCPUAffinity=0-1\n")
        path.chmod(0o600)
        original = backend.file_state(path)
        self.host._reload()
        self.assertTrue(self.apply()["ok"])
        self.assertNotEqual(backend.file_state(path), original)
        restored = backend.restore(self.state_dir, self.host)
        self.assertTrue(restored["ok"], restored)
        self.assertEqual(backend.file_state(path), original)

    def test_source_drift_between_plan_and_apply_aborts_before_configuration_write(self):
        real_plan = backend.make_plan

        def plan_then_change(config, host):
            plan = real_plan(config, host)
            self.host.fragment.write_text("# independently changed unit\n")
            return plan

        with patch.object(backend, "make_plan", side_effect=plan_then_change):
            result = self.apply()
        self.assertFalse(result["ok"])
        self.assertTrue(result["recovery"]["ok"], result)
        self.assertFalse(backend.dropin_path(self.host, {"unit": "CubeMaster.service"}).exists())
        self.assertEqual(self.host.calls, [])
        self.assertEqual(self.host.fragment.read_text(), "# independently changed unit\n")

    def test_restore_daemon_reload_failure_skips_restart(self):
        self.assertTrue(self.apply(self.config(restart=True))["ok"])
        self.host.calls.clear()
        self.host.fail_once = ("systemctl", "daemon-reload")
        result = backend.restore(self.state_dir, self.host)
        self.assertFalse(result["ok"])
        self.assertNotIn(["systemctl", "restart", "CubeMaster.service"], self.host.calls)

    def test_plan_rejects_restart_of_inactive_service_or_stopped_container(self):
        self.host.loaded["ActiveState"] = "inactive"
        with self.assertRaises(ValueError):
            backend.make_plan(self.config(restart=True), self.host)
        self.host.loaded["ActiveState"] = "active"
        self.host.containers["cube-proxy"]["running"] = False
        with self.assertRaises(ValueError):
            backend.make_plan(self.config(containers=True), self.host)
        self.assertEqual(self.host.calls, [])

    def test_restore_refuses_external_file_change_and_does_not_restart(self):
        self.assertTrue(self.apply(self.config(restart=True))["ok"])
        path = backend.dropin_path(self.host, {"unit": "CubeMaster.service"})
        environment = self.host.path(backend.environment_path(self.config()["services"][0]))
        environment_before = backend.file_state(environment)
        external = path.read_text() + "# independently modified\n"
        path.write_text(external)
        self.host.calls.clear()
        result = backend.restore(self.state_dir, self.host)
        self.assertFalse(result["ok"])
        self.assertEqual(path.read_text(), external)
        self.assertEqual(backend.file_state(environment), environment_before)
        self.assertNotIn(["systemctl", "restart", "CubeMaster.service"], self.host.calls)
        self.assertEqual(self.state()["status"], "restore_failed")

    def test_restore_refuses_container_replacement_or_external_change(self):
        self.assertTrue(self.apply({"version": 1, "containers": [{"name": "cube-proxy", "cpuset_cpus": "4-5"}]})["ok"])
        state = self.state()
        original = self.host.container("cube-proxy")
        for key, changed in (("id", "b" * 64), ("image", "sha256:replacement"), ("cpuset_cpus", "0-1")):
            with self.subTest(field=key):
                self.save(copy.deepcopy(state))
                self.host.containers["cube-proxy"] = dict(original, **{key: changed})
                self.host.calls.clear()
                result = backend.restore(self.state_dir, self.host)
                self.assertFalse(result["ok"])
                self.assertEqual(self.host.calls, [])
                self.assertEqual(self.host.containers["cube-proxy"][key], changed)

    def test_plan_refuses_missing_cpu_alias_unloaded_and_unmanaged_dropin(self):
        invalid = self.config()
        invalid["services"][0]["cpu_affinity"] = "99"
        with self.assertRaises(ValueError):
            backend.make_plan(invalid, self.host)
        for property_name, value in (("Id", "Alias.service"), ("LoadState", "not-found"), ("NeedDaemonReload", "yes")):
            with self.subTest(property_name=property_name):
                saved = self.host.loaded[property_name]
                self.host.loaded[property_name] = value
                with self.assertRaises(ValueError):
                    backend.make_plan(self.config(), self.host)
                self.host.loaded[property_name] = saved
        path = backend.dropin_path(self.host, {"unit": "CubeMaster.service"})
        path.parent.mkdir(parents=True)
        path.write_text("# somebody else's file\n")
        with self.assertRaises(ValueError):
            backend.make_plan(self.config(), self.host)
        self.assertEqual(self.host.calls, [])

    def test_snapshot_rejects_machine_path_content_and_config_tampering(self):
        self.assertTrue(self.apply()["ok"])
        original = self.state()
        mutations = [lambda s: s.update(version=2),
                     lambda s: s["plan"]["identity"].update(machine_id="other"),
                     lambda s: s["plan"]["services"][0].update(path="/tmp/arbitrary"),
                     lambda s: s["plan"]["services"][0].update(content="bad"),
                     lambda s: s["plan"]["config"].update(unknown=True)]
        for mutate in mutations:
            state = copy.deepcopy(original)
            mutate(state)
            self.save(state)
            with self.subTest(state=state), self.assertRaises(ValueError):
                backend.load_state(self.state_dir, self.host)

    def test_snapshot_rejects_unknown_fields_bool_version_and_foreign_started_targets(self):
        self.assertTrue(self.apply()["ok"])
        original = self.state()
        mutations = [lambda s: s.update(version=True), lambda s: s.update(unknown="field"),
                     lambda s: s.update(status="invented"),
                     lambda s: s.update(restarts_started=["foreign.service"]),
                     lambda s: s.update(files_started=["/tmp/foreign"]),
                     lambda s: s.update(containers_started=["foreign-container"])]
        for mutate in mutations:
            state = copy.deepcopy(original)
            mutate(state)
            self.save(state)
            with self.subTest(state=state), self.assertRaises(ValueError):
                backend.load_state(self.state_dir, self.host)

    def test_snapshot_and_output_symlinks_rejected(self):
        real = self.root / "real"
        real.mkdir()
        link = self.root / "link"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(ValueError):
            backend.atomic_write(link / "file", b"bad")
        self.assertFalse((real / "file").exists())
        self.assertTrue(self.apply()["ok"])
        self.state_dir.chmod(0o755)
        with self.assertRaises(ValueError):
            backend.load_state(self.state_dir, self.host)


if __name__ == "__main__":
    unittest.main()
