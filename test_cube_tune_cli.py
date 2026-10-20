"""Offline CLI/transport checks; never contact SSH or write host configuration."""

import ast
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import cube_tune


class CubeTuneCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / 'settings.json'
        self.config.write_text(json.dumps({
            'version': 1,
            'services': [{'unit': 'cube-sandbox-cubemaster.service', 'runtime': 'go',
                          'go_env': {'GOMAXPROCS': '4'}, 'cpu_affinity': '0-3'}],
        }))

    @staticmethod
    def call(args):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            status = cube_tune.main(args)
        return status, json.loads(output.getvalue())

    @staticmethod
    def transport_code(payload):
        lines = cube_tune.build_script(payload).splitlines()
        if lines[0] != "python3 - <<'MACHCONF_CUBE_TUNE_PY'" or lines[-1] != 'MACHCONF_CUBE_TUNE_PY':
            raise AssertionError('unexpected transport envelope')
        return '\n'.join(lines[1:-1])

    def test_argument_conflicts_and_missing_targets_do_not_create_runner(self):
        cases = [
            ['--config', str(self.config)],
            ['--local', '-H', 'host', '--config', str(self.config)],
            ['--local', '--ask-pass', '--config', str(self.config)],
            ['apply', '--local'],
            ['plan', '--local', '--config', str(self.config), '--state-dir', '/tmp/snapshot'],
            ['restore', '--local'],
            ['restore', '--local', '--state-dir', '/tmp/snapshot', '--config', str(self.config)],
            ['verify', '--local'],
            ['verify', '--local', '--state-dir', '/tmp/snapshot', '--config', str(self.config)],
            ['verify', '--local', '--state-dir', 'relative'],
            ['restore', '--local', '--state-dir', '/tmp/../snapshot'],
            ['-H', 'host-a,host-b', '--config', str(self.config)],
            ['-H', cube_tune.LOCAL, '--config', str(self.config)],
        ]
        for args in cases:
            with self.subTest(args=args), patch.object(cube_tune, 'make_runner') as factory:
                with self.assertRaises(SystemExit) as exc:
                    self.call(args)
                self.assertEqual(exc.exception.code, 2)
                factory.assert_not_called()

    def test_default_action_is_plan_and_config_is_validated_before_transport(self):
        runner = Mock()
        runner.run_script.return_value = '{"ok": true, "status": "preview"}'
        with patch.object(cube_tune, 'make_runner', return_value=runner) as factory, patch.object(cube_tune, 'build_script', return_value='SCRIPT') as build:
            status, result = self.call(['-H', 'root@host', '--ask-pass', '--config', str(self.config)])
        self.assertEqual(status, 0)
        self.assertEqual(result['status'], 'preview')
        factory.assert_called_once_with('host', ask_pass=True, command_timeout=1800, user='root')
        payload = build.call_args.args[0]
        self.assertEqual(payload['action'], 'plan')
        self.assertFalse(payload['config']['services'][0]['restart'])
        runner.run_script.assert_called_once_with('SCRIPT')

    def test_invalid_and_duplicate_json_never_contact_target(self):
        cases = [
            '{"version": 1, "version": 1, "containers": [{"name":"test","cpuset_cpus":"0"}]}',
            '{"version": 1, "services": [{"unit":"test.service","go_env":{"GOGC":"100","GOGC":"800"}}]}',
            '{broken json',
            '[]',
            '{"version": 1, "services": []}',
            '{"version": 1, "containers": [{"name":"test","cpuset_cpus":"9-2"}]}',
            '{"version": 1, "services": [{"unit":"test.service","runtime":"rust","go_env":{"GOGC":"100"}}]}',
        ]
        for raw in cases:
            with self.subTest(raw=raw), patch.object(cube_tune, 'make_runner') as factory:
                self.config.write_text(raw)
                status, result = self.call(['-H', 'host', '--config', str(self.config)])
                self.assertEqual(status, 1)
                self.assertFalse(result['ok'])
                factory.assert_not_called()

    def test_snapshot_verify_and_restore_dispatch_without_config_reads(self):
        for action in ('verify', 'restore'):
            with self.subTest(action=action):
                runner = Mock()
                runner.run_script.return_value = '{"ok": true}'
                with patch.object(cube_tune, 'make_runner', return_value=runner), patch.object(cube_tune, 'read_config') as read, patch.object(cube_tune, 'build_script', return_value='SCRIPT') as build:
                    status, unused = self.call([action, '--local', '--state-dir', '/tmp/private-snapshot'])
                self.assertEqual(status, 0)
                read.assert_not_called()
                build.assert_called_once_with({'action': action, 'state_dir': '/tmp/private-snapshot'})

    def test_remote_failures_connection_errors_and_invalid_json_exit_one(self):
        for response in ('{"ok":false,"error":"verification failed"}', 'not-json', RuntimeError('connection lost')):
            with self.subTest(response=response):
                runner = Mock()
                if isinstance(response, Exception):
                    runner.run_script.side_effect = response
                else:
                    runner.run_script.return_value = response
                with patch.object(cube_tune, 'make_runner', return_value=runner):
                    status, result = self.call(['-H', 'host', '--config', str(self.config)])
                self.assertEqual(status, 1)
                self.assertFalse(result['ok'])

    def test_non_object_remote_json_returns_controlled_failure(self):
        for response in ('null', '[]', 'true', '"unexpected"'):
            with self.subTest(response=response):
                runner = Mock()
                runner.run_script.return_value = response
                with patch.object(cube_tune, 'make_runner', return_value=runner):
                    status, result = self.call(['-H', 'host', '--config', str(self.config)])
                self.assertEqual(status, 1)
                self.assertFalse(result['ok'])

    def test_transport_loads_isolated_modules_without_repository_imports(self):
        # Unsupported action constructs only Host, then raises before any reads
        # or writes. -I and an empty cwd prevent imports from the checkout.
        script = cube_tune.build_script({'action': 'not-an-operation'})
        subprocess.run(['bash', '-n'], input=script, capture_output=True, text=True,
                       timeout=10, check=True)
        code = self.transport_code({'action': 'not-an-operation'})
        ast.parse(code, feature_version=(3, 8))
        result = subprocess.run([sys.executable, '-I', '-c', code], cwd=self.tmp.name,
                                capture_output=True, text=True, timeout=10, check=True)
        self.assertEqual(result.stderr, '')
        output = json.loads(result.stdout)
        self.assertFalse(output['ok'])
        self.assertEqual(output['error_type'], 'ValueError')
        self.assertIn('不支持的操作', output['error'])
        self.assertEqual(sorted(p.name for p in Path(self.tmp.name).iterdir()), ['settings.json'])

    def test_transport_rejects_python_before_38_before_loading_sources(self):
        code = 'import sys\nsys.version_info = (3, 7, 17)\n' + self.transport_code({'action': 'not-an-operation'})
        result = subprocess.run([sys.executable, '-I', '-c', code], cwd=self.tmp.name,
                                capture_output=True, text=True, timeout=10, check=True)
        output = json.loads(result.stdout)
        self.assertFalse(output['ok'])
        self.assertEqual(output['error_type'], 'RuntimeError')
        self.assertIn('3.8', output['error'])

    def test_embedded_sources_use_python_38_syntax(self):
        for name in ('cube_tune_config', 'cube_tune_verify', 'cube_tune_backend'):
            with self.subTest(module=name):
                source = (Path(cube_tune.__file__).parent / 'modules' / (name + '.py')).read_text()
                ast.parse(source, feature_version=(3, 8))


if __name__ == '__main__':
    unittest.main()
