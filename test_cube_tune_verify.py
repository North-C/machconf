"""Offline proc/cgroup fixtures only; never inspect or modify live services."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

_SPEC = importlib.util.spec_from_file_location('cube_tune_verify', Path(__file__).parent / 'modules' / 'cube_tune_verify.py')
verify = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(verify)


class ProcessVerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.proc = self.root / 'proc'
        self.pid = self.proc / '123'
        self.pid.mkdir(parents=True)
        (self.pid / 'exe').symlink_to('/opt/CubeMaster/bin/cubemaster')
        (self.pid / 'stat').write_text('123 (name with ) spaces) S ' + '0 ' * 18 + '999 0 0\n')
        (self.pid / 'environ').write_bytes(b'GOGC=800\0GOMAXPROCS=4\0TOKEN=secret\0')
        (self.pid / 'numa_maps').write_text('7ffc bind:1 file=/super-secret/path N1=4\n7fff bind:1 anon=2 N1=2\n')
        for tid in (123, 124):
            directory = self.pid / 'task' / str(tid)
            directory.mkdir(parents=True)
            (directory / 'status').write_text('Cpus_allowed_list:\t4-5\nMems_allowed_list:\t0-1\n')
        (self.pid / 'cgroup').write_text('0::/tenant/svc\n')
        self.cg = self.root / 'cgroup'
        (self.cg / 'svc').mkdir(parents=True)
        (self.cg / 'svc' / 'cpuset.cpus').write_text('')
        (self.cg / 'svc' / 'cpuset.cpus.effective').write_text('4-7')
        (self.cg / 'svc' / 'cpuset.mems.effective').write_text('0-1')
        (self.cg / 'cpuset.cpus').write_text('0-7')
        (self.proc / 'self').mkdir()
        (self.proc / 'self' / 'mountinfo').write_text('42 1 0:1 /tenant %s rw - cgroup2 cgroup rw\n' % self.cg)

    def inspect(self):
        return verify.inspect_process(123, str(self.proc))

    @staticmethod
    def by_name(checks):
        return {item['check']: item for item in checks}

    def test_stable_snapshot_redacts_non_go_env_and_map_addresses_paths(self):
        obs = self.inspect()
        self.assertEqual(obs['status'], 'ok')
        self.assertEqual(obs['identity']['starttime_ticks'], 999)
        self.assertEqual(obs['go_env'], {'GOGC': '800', 'GOMAXPROCS': '4'})
        self.assertEqual(obs['numa_maps']['policies'], {'bind:1': 2})
        serialized = json.dumps(obs)
        for forbidden in ('secret', '7ffc', 'TOKEN'):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(len(obs['threads']['items']), 2)

    def test_all_threads_cpu_subset_and_go_launch_environment(self):
        checks = self.by_name(verify.verify_process(self.inspect(), {'cpu_affinity': '4-7', 'go_env': {'GOGC': '800'}}))
        self.assertTrue(checks['cpu_affinity']['ok'])
        self.assertTrue(checks['go_env.GOGC']['ok'])
        checks = self.by_name(verify.verify_process(self.inspect(), {'cpu_affinity': '4', 'go_env': {'GOGC': '100'}}))
        self.assertFalse(checks['cpu_affinity']['ok'])
        self.assertFalse(checks['go_env.GOGC']['ok'])

    def test_missing_environment_not_inferred_from_go_defaults(self):
        checks = self.by_name(verify.verify_process(self.inspect(), {'go_env': {'GOMEMLIMIT': 'off', 'GOTRACEBACK': 'single'}}))
        self.assertFalse(checks['go_env.GOMEMLIMIT']['ok'])
        self.assertFalse(checks['go_env.GOTRACEBACK']['ok'])

    def test_single_thread_outside_request_fails(self):
        (self.pid / 'task' / '124' / 'status').write_text('Cpus_allowed_list: 9\nMems_allowed_list: 0-1\n')
        checks = self.by_name(verify.verify_process(self.inspect(), {'cpu_affinity': '4-7'}))
        self.assertFalse(checks['cpu_affinity']['ok'])

    def test_mems_allowance_does_not_substitute_for_numa_policy(self):
        checks = self.by_name(verify.verify_process(self.inspect(), {'numa_policy': 'bind', 'numa_nodes': '1'}))
        self.assertTrue(checks['numa_policy']['ok'])
        (self.pid / 'numa_maps').write_text('abc default N1=4\n')
        checks = self.by_name(verify.verify_process(self.inspect(), {'numa_policy': 'bind', 'numa_nodes': '1'}))
        self.assertFalse(checks['numa_policy']['ok'])

    def test_application_policy_override_or_mixed_policy_does_not_pass(self):
        (self.pid / 'numa_maps').write_text('abc bind:1 N1=4\ndef default N1=1\n')
        checks = self.by_name(verify.verify_process(self.inspect(), {'numa_policy': 'bind', 'numa_nodes': '1'}))
        self.assertFalse(checks['numa_policy']['ok'])
        self.assertIn('application may override', checks['numa_policy']['detail'])

    def test_container_sets_are_subsets_with_page_migration_caveat(self):
        checks = self.by_name(verify.verify_process(self.inspect(), {'cpuset_cpus': '0-7', 'cpuset_mems': '0-1'}))
        self.assertTrue(checks['cpuset_cpus']['ok'])
        self.assertTrue(checks['cpuset_mems']['ok'])
        self.assertIn('does not prove page migration', checks['cpuset_mems']['detail'])
        checks = self.by_name(verify.verify_process(self.inspect(), {'cpuset_mems': '1'}))
        self.assertFalse(checks['cpuset_mems']['ok'])

    def test_missing_thread_file_is_partial_and_fails_cpu_check(self):
        (self.pid / 'task' / '124' / 'status').unlink()
        obs = self.inspect()
        self.assertEqual(obs['threads']['status'], 'partial')
        self.assertFalse(self.by_name(verify.verify_process(obs, {'cpu_affinity': '4-7'}))['cpu_affinity']['ok'])

    def test_permission_denial_is_not_success(self):
        reader = verify._tv_read
        def read(path, binary=False):
            if path.endswith('/environ'):
                raise PermissionError()
            return reader(path, binary)
        with patch.object(verify, '_tv_read', side_effect=read):
            obs = self.inspect()
        self.assertEqual(obs['go_env_status'], 'unavailable')
        self.assertIn('environ:permission-denied', obs['errors'])
        self.assertFalse(self.by_name(verify.verify_process(obs, {'go_env': {'GOGC': '800'}}))['go_env.GOGC']['ok'])

    def test_changed_thread_inventory_is_partial(self):
        with patch.object(verify, '_tv_tids', side_effect=[[123, 124], [123, 125]]):
            obs = self.inspect()
        self.assertEqual(obs['threads']['status'], 'partial')
        self.assertFalse(self.by_name(verify.verify_process(obs, {'cpu_affinity': '4-7'}))['cpu_affinity']['ok'])

    def test_pid_reuse_invalidates_every_requested_check(self):
        with patch.object(verify, '_tv_starttime', side_effect=[999, 1000]):
            obs = self.inspect()
        checks = verify.verify_process(obs, {'cpu_affinity': '4-7', 'go_env': {'GOGC': '800'}, 'numa_policy': 'bind', 'numa_nodes': '1'})
        self.assertTrue(all(not check['ok'] for check in checks))
        self.assertEqual(obs['status'], 'partial')

    def test_process_exit_and_unreadable_executable_fail_identity(self):
        (self.pid / 'exe').unlink()
        obs = self.inspect()
        self.assertFalse(verify.verify_process(obs, {})[0]['ok'])
        self.assertEqual(obs['status'], 'unavailable')

    def test_cgroup_v2_effective_sets_and_visible_parent_evidence(self):
        cgroups = self.inspect()['cgroups']
        self.assertEqual(cgroups['status'], 'ok')
        levels = cgroups['memberships'][0]['levels']
        self.assertEqual(levels[0]['values']['cpuset.cpus'], '')
        self.assertEqual(levels[0]['values']['cpuset.cpus.effective'], '4-7')
        self.assertEqual(levels[1]['values']['cpuset.cpus'], '0-7')

    def test_cgroup_v1_cpuset_mount(self):
        (self.pid / 'cgroup').write_text('3:cpu,cpuacct:/unrelated\n4:cpuset:/svc\n')
        (self.proc / 'self' / 'mountinfo').write_text('42 1 0:1 / %s rw - cgroup cgroup rw,cpuset\n' % self.cg)
        (self.cg / 'svc' / 'cpuset.effective_cpus').write_text('4-5')
        cgroups = self.inspect()['cgroups']
        self.assertEqual(cgroups['status'], 'ok')
        self.assertEqual(len(cgroups['memberships']), 1)
        self.assertEqual(cgroups['memberships'][0]['levels'][0]['values']['cpuset.effective_cpus'], '4-5')

    def test_cgroup_outside_mount_is_unavailable(self):
        (self.pid / 'cgroup').write_text('0::/different/svc\n')
        self.assertEqual(self.inspect()['cgroups']['status'], 'unavailable')

    def test_empty_affinity_and_malformed_ranges_are_never_success(self):
        for value in ('', '4-', '5-4', '1,,2', '-1', '1-999999999'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    verify._tv_ids(value)
        (self.pid / 'task' / '124' / 'status').write_text('Cpus_allowed_list:\nMems_allowed_list: 1\n')
        self.assertEqual(self.inspect()['threads']['status'], 'partial')

    def test_numa_preferred_alias_and_policy_flags_are_conservative(self):
        self.assertTrue(verify._tv_policy_matches('prefer:1', 'preferred', '1'))
        self.assertTrue(verify._tv_policy_matches('interleave:0-1', 'interleave', '0,1'))
        self.assertTrue(verify._tv_policy_matches('local', 'local', ''))
        self.assertFalse(verify._tv_policy_matches('bind:1(relative)', 'bind', '1'))

    def test_duplicate_go_environment_is_ambiguous(self):
        (self.pid / 'environ').write_bytes(b'GOGC=800\0GOGC=100\0')
        obs = self.inspect()
        self.assertEqual(obs['go_env_status'], 'unavailable')
        self.assertFalse(self.by_name(verify.verify_process(obs, {'go_env': {'GOGC': '800'}}))['go_env.GOGC']['ok'])


if __name__ == '__main__':
    unittest.main()
