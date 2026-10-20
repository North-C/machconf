"""Runtime inspection regression tests; no SSH or running Cube deployment needed."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch

import cube_runtime
from modules.cube_runtime import CubeRuntimeModule, decode_report, report_items


def report():
    return {"schema_version": 1, "collected_at": "2026-10-16T00:00:00Z", "errors": [], "services": [{
        "label": "CubeMaster", "name": "cube-sandbox-cubemaster.service", "errors": [],
        "systemd": {"status": "ok", "environment_status": "ok",
                    "environment": {"GOGC": "400", "GOMAXPROCS": "16"},
                    "properties": {"AllowedCPUs": "96-191", "EnvironmentFiles": "/etc/cube.env"}, "errors": []},
        "processes": [{"pid": 123, "exe": "/opt/cube/CubeMaster/bin/cubemaster", "identity_status": "match",
                       "source": ["systemd-mainpid"], "environment_status": "ok", "environment": {"GOGC": "100"},
                       "threads": {"status": "ok", "observed_count": 2, "affinity_groups": [
                           {"cpus_allowed_list": "0-383", "mems_allowed_list": "0-3", "count": 2}]},
                       "cgroups": {"status": "ok", "memberships": [{"version": 1, "controllers": ["cpuset"],
                           "levels": [{"path": "/", "values": {"cpuset.effective_cpus": "0-383"}, "errors": []}]}]},
                       "errors": []}]}]}


class PresentationTests(unittest.TestCase):
    def test_configuration_is_distinct_from_effective_affinity(self):
        items = report_items(report())
        by_key = {x.key: x for x in items}
        self.assertEqual(by_key['CubeMaster systemd.AllowedCPUs'].value, '96-191')
        self.assertIn('0-383', by_key['CubeMaster pid=123 线程亲和性'].value)
        self.assertEqual(by_key['CubeMaster pid=123 env对照.GOGC'].value, '不同')
        self.assertEqual(by_key['CubeMaster pid=123 env对照.GOMAXPROCS'].value, '不同')

    def test_unreadable_environment_is_not_called_a_mismatch(self):
        d = report(); d['services'][0]['processes'][0]['environment_status'] = 'unavailable'
        self.assertFalse(any('env对照' in x.key for x in report_items(d)))

    def test_other_installation_is_not_compared_to_service_environment(self):
        d = report()
        d['services'][0]['processes'][0]['source'] = ['proc-exe-scan']
        d['services'][0]['processes'][0]['path_status'] = 'alternate-path'
        self.assertFalse(any('env对照' in x.key for x in report_items(d)))

    def test_absent_process_is_explicit(self):
        d = report(); d['services'][0]['processes'] = []
        self.assertTrue(any(x.value == '未观测到' for x in report_items(d)))

    def test_bad_probe_output_is_not_echoed(self):
        data = decode_report('invalid output with password=SUPER_SECRET')
        self.assertTrue(data['errors'])
        self.assertNotIn('SUPER_SECRET', json.dumps(data))
        self.assertTrue(decode_report('[]')['errors'])

    def test_nested_errors_are_visible_in_human_output(self):
        d = report()
        d['services'][0]['processes'][0]['cgroups']['memberships'][0]['errors'] = ['matching-cgroup-mount-unavailable']
        d['config_files'] = [{'path': '/opt/cube/conf.yaml', 'status': 'unavailable', 'error': 'permission-denied'}]
        notes = [x.note for x in report_items(d)]
        self.assertIn('matching-cgroup-mount-unavailable', notes)
        self.assertIn('permission-denied', notes)

    def test_module_parses_same_report(self):
        items = CubeRuntimeModule().parse({'cube_runtime': json.dumps(report())})
        self.assertTrue(any(x.key == 'CubeMaster pid=123 env.GOGC' and x.value == '100' for x in items))

    def test_only_resource_fields_survive_presentation_of_older_reports(self):
        data = report()
        service = data["services"][0]
        noise = {"SHELL": "/bin/bash", "HOME": "/root", "PATH": "/bin",
                 "CUBE_API_BIND": "0.0.0.0:8080", "CUBE_API_SANDBOX_DOMAIN": "example.invalid",
                 "RUST_LOG": "info", "GOTRACEBACK": "all", "DATABASE_URL": "<redacted>"}
        service["systemd"]["environment"].update(noise)
        service["processes"][0]["environment"].update(noise)
        service["systemd"]["environment"]["WORKER_THREADS"] = "16"
        service["systemd"]["properties"].update(
            NRestarts="3", FragmentPath="/unrelated/unit", DropInPaths="/unrelated/dropin",
            PassEnvironment=["SHELL", "GOMAXPROCS"],
            UnsetEnvironment=["TOKEN=secret", "GOGC=secret"])
        data["config_files"] = [{"path": "/opt/cube/config", "status": "ok", "items": [
            {"key": "app_snapshot_timeout_insec", "value": "20"},
            {"key": "pool_workers", "value": "32"}]}]
        items = report_items(data)
        rendered = json.dumps([vars(item) for item in items])
        for key in list(noise) + ["NRestarts", "FragmentPath", "DropInPaths", "app_snapshot_timeout_insec", "TOKEN", "secret"]:
            self.assertNotIn(key, rendered)
        self.assertIn("WORKER_THREADS", rendered)
        self.assertIn("pool_workers", rendered)
        self.assertIn("GOGC=<redacted>", rendered)

    def test_cgroup_rows_compact_each_level_without_losing_parent_constraints(self):
        data = report()
        memberships = data["services"][0]["processes"][0]["cgroups"]["memberships"]
        memberships[:] = [{"version": "2", "controllers": [], "levels": [
            {"path": "/cg/service", "values": {"cpu.max": "max 100000", "memory.max": "max"}},
            {"path": "/cg", "values": {"cpu.max": "200000 100000", "memory.max": "104857600"}}]},
            {"version": "1", "controllers": ["net_cls"], "levels": [
                {"path": "/cg/net", "values": {"net_cls.classid": "123"}}]}]
        items = [item for item in report_items(data) if item.key.endswith(" 资源")]
        self.assertEqual(len(items), 2)
        self.assertEqual(json.loads(items[0].value)["cpu.max"], "max 100000")
        self.assertEqual(json.loads(items[1].value)["memory.max"], "104857600")
        self.assertNotIn("net_cls", json.dumps([vars(item) for item in report_items(data)]))

    def test_service_state_is_retained_in_one_summary(self):
        data = report()
        data["services"][0]["systemd"]["properties"].update(
            LoadState="loaded", ActiveState="active", SubState="running", MainPID="123")
        items = report_items(data)
        summary = next(item for item in items if item.key == "CubeMaster systemd")
        self.assertIn("ActiveState=active", summary.note)
        self.assertIn("MainPID=123", summary.note)
        self.assertFalse(any(item.key.endswith("systemd.MainPID") for item in items))


class CliTests(unittest.TestCase):
    def invoke(self, args, data=None):
        buf = io.StringIO()
        with patch.object(cube_runtime, 'collect_runtime', return_value=data or report()) as collect:
            with contextlib.redirect_stdout(buf):
                rc = cube_runtime.main(args)
        return rc, buf.getvalue(), collect

    def test_json_retains_structured_evidence(self):
        rc, out, collect = self.invoke(['-H', '192.0.2.1,192.0.2.2', '-f', 'json'])
        self.assertEqual(rc, 0)
        self.assertEqual(collect.call_count, 2)
        self.assertEqual(json.loads(out)['192.0.2.1']['services'][0]['processes'][0]['pid'], 123)

    def test_markdown_has_source_labels(self):
        rc, out, _ = self.invoke(['--local', '-f', 'markdown'])
        self.assertEqual(rc, 0)
        self.assertIn('systemd.AllowedCPUs', out)
        self.assertIn('env对照.GOGC', out)

    def test_probe_failure_is_nonzero(self):
        rc, _, _ = self.invoke(['--local', '-f', 'json'], {'services': [], 'errors': ['python3 unavailable']})
        self.assertEqual(rc, 1)

    def test_rejects_empty_hosts_and_local_password(self):
        for args in [['-H', ','], ['--local', '--ask-pass'], ['--local', '-H', 'x']]:
            with self.assertRaises(SystemExit):
                self.invoke(args)


if __name__ == '__main__':
    unittest.main()
