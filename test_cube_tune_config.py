"""Pure tests: no systemd, Docker, remote hosts, or host mutations."""

import copy
import unittest

from modules.cube_tune_config import (
    ConfigError, MARKER, environment_path, parse_id_list, render_dropin,
    render_environment, validate_config,
)


class ConfigTests(unittest.TestCase):
    def service(self, **kwargs):
        return {"version": 1, "services": [{"unit": "CubeMaster.service", **kwargs}]}

    def test_configuration_is_canonical_fresh_and_has_no_recipe_defaults(self):
        source = self.service(runtime="go", go_env={"GOGC": "350"}, cpu_affinity="8,2,0-1,2")
        before = copy.deepcopy(source)
        actual = validate_config(source)
        self.assertEqual(source, before)
        self.assertEqual(actual["services"][0], {
            "unit": "CubeMaster.service", "runtime": "go", "restart": False,
            "go_env": {"GOGC": "350"}, "cpu_affinity": "0-2,8",
        })
        self.assertEqual(actual["containers"], [])
        actual["services"][0]["go_env"]["GOGC"] = "off"
        self.assertEqual(source, before)

    def test_multiple_targets_allow_distinct_runtime_and_affinity(self):
        data = {"version": 1, "services": [
            {"unit": "Cubelet.service", "runtime": "rust", "cpu_affinity": "0-7"},
            {"unit": "CubeMaster.service", "runtime": "go", "go_env": {"GOMAXPROCS": "8"}},
            {"unit": "CubeAPI@production.service", "numa_policy": "bind", "numa_nodes": "1"},
        ], "containers": [{"name": "cube-proxy", "cpuset_cpus": "8-11", "cpuset_mems": "1"}]}
        self.assertEqual(validate_config(data)["containers"], data["containers"])

    def test_strict_structure_and_target_requirements(self):
        invalid = [None, [], {}, {"version": True}, {"version": 2},
                   {"version": 1}, {"version": 1, "services": {}},
                   {"version": 1, "containers": None},
                   {"version": 1, "surprise": "value"},
                   self.service(), self.service(restart=True),
                   self.service(cpu_affinity="0", restart="yes"),
                   self.service(cpu_affinity="0", runtime="python"),
                   self.service(cpu_affinity="0", extra="field"),
                   {"version": 1, "containers": [{"name": "proxy"}]},
                   {"version": 1, "containers": [{"name": "proxy", "cpuset_cpus": "0", "restart": True}]}]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ConfigError):
                validate_config(data)

    def test_duplicate_targets_rejected(self):
        service = {"unit": "CubeMaster.service", "cpu_affinity": "0"}
        container = {"name": "proxy", "cpuset_mems": "0"}
        for key, target in (("services", service), ("containers", container)):
            with self.subTest(key=key), self.assertRaises(ConfigError):
                validate_config({"version": 1, key: [target, target]})

    def test_unsafe_units_and_container_names_rejected(self):
        for name in ("../x.service", "/x.service", "-x.service", "x.timer", "x*.service",
                     "x%p.service", "x\n.service", "x@.service", "x@y@z.service", "x" * 256 + ".service"):
            with self.subTest(unit=name), self.assertRaises(ConfigError):
                validate_config({"version": 1, "services": [{"unit": name, "cpu_affinity": "0"}]})
        for name in ("-proxy", "/proxy", "proxy*", "proxy;id", "proxy\n", "", "x" * 256):
            with self.subTest(container=name), self.assertRaises(ConfigError):
                validate_config({"version": 1, "containers": [{"name": name, "cpuset_cpus": "0"}]})

    def test_go_settings_require_declared_go_runtime(self):
        for runtime in ("rust", "other"):
            with self.subTest(runtime=runtime), self.assertRaises(ConfigError):
                validate_config(self.service(runtime=runtime, go_env={"GOGC": "200"}))
        for env in ({}, {"WORKER_THREADS": "8"}, {"GOGC": 200}, None):
            with self.subTest(env=env), self.assertRaises(ConfigError):
                validate_config(self.service(runtime="go", go_env=env))

    def test_valid_go_values(self):
        valid = {"GOMAXPROCS": ["1", "128"], "GOGC": ["off", "0", "800"],
                 "GOMEMLIMIT": ["off", "0", "1073741824", "1024B", "64KiB", "512MiB", "8GiB", "1TiB"],
                 "GOTRACEBACK": ["none", "single", "all", "system", "crash", "wer", "0", "1", "2"],
                 "GODEBUG": ["gctrace=1", "gctrace=1,schedtrace=1000", "#http2client=0"]}
        for key, values in valid.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.assertEqual(validate_config(self.service(runtime="go", go_env={key: value}))
                                     ["services"][0]["go_env"][key], value)

    def test_invalid_go_values_and_injection_rejected(self):
        invalid = {"GOMAXPROCS": ["0", "-1", "1.5", "4294967296"],
                   "GOGC": ["-1", "1.5", "true", "9999999999"],
                   "GOMEMLIMIT": ["8GB", "1.5GiB", "-1", "9223372036854775808", "9999999999999999999TiB"],
                   "GOTRACEBACK": ["unknown", "3"],
                   "GODEBUG": ["trace", "x=1,", "x=", 'x="1"', "x=1\\n", "x=$(id)"]}
        for key in ("GOMAXPROCS", "GOGC", "GOMEMLIMIT", "GOTRACEBACK", "GODEBUG"):
            invalid[key].extend(["", "x=1\nExecStart=/bin/evil", "x=%n", "x=1\x00", "x=1\t", "x=1\x7f"])
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ConfigError):
                    validate_config(self.service(runtime="go", go_env={key: value}))

    def test_numa_policy_constraints(self):
        for policy in ("default", "local"):
            self.assertEqual(validate_config(self.service(numa_policy=policy))["services"][0]["numa_policy"], policy)
            with self.assertRaises(ConfigError):
                validate_config(self.service(numa_policy=policy, numa_nodes="0"))
        for policy in ("bind", "interleave", "preferred"):
            validate_config(self.service(numa_policy=policy, numa_nodes="1"))
            with self.assertRaises(ConfigError):
                validate_config(self.service(numa_policy=policy))
        for settings in ({"numa_policy": "preferred", "numa_nodes": "0-1"},
                         {"numa_nodes": "0"}, {"numa_policy": "invalid"}):
            with self.subTest(settings=settings), self.assertRaises(ConfigError):
                validate_config(self.service(**settings))

    def test_id_list_parsing_and_limits(self):
        self.assertEqual(parse_id_list("0-3,8,2"), {0, 1, 2, 3, 8})
        self.assertEqual(parse_id_list("1048575"), {1048575})
        for value in (None, 0, "", "-1", "3-1", "0-", "1,", ",1", "0 1", " 1", "1\n",
                      "1,,2", "1--2", "1-2-3", "1:4", "all", "1048576", "0-65536",
                      "0-65535,100000", "999999999999999999", ",".join(["0-65535"] * 5)):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                parse_id_list(value)


class RenderTests(unittest.TestCase):
    def test_only_requested_fields_are_rendered(self):
        self.assertEqual(render_dropin({"unit": "CubeMaster.service", "runtime": "go", "go_env": {"GOGC": "300"}}),
                         MARKER + '\n[Service]\nEnvironment="GOGC=300"\n'
                         'EnvironmentFile=/etc/systemd/system/CubeMaster.service.d/zzzz-machconf-tune.env\n')

    def test_affinity_and_mask_are_reset_before_setting(self):
        rendered = render_dropin({"unit": "Cubelet.service", "cpu_affinity": "0-3,8",
                                  "numa_policy": "bind", "numa_nodes": "0,2"})
        self.assertEqual(rendered, MARKER + "\n[Service]\nCPUAffinity=\nCPUAffinity=0-3 8\n"
                         "NUMAPolicy=bind\nNUMAMask=\nNUMAMask=0 2\n")
        self.assertNotIn("AllowedCPUs", rendered)

    def test_default_local_policy_clear_inherited_mask(self):
        for policy in ("default", "local"):
            self.assertEqual(render_dropin({"unit": "CubeAPI.service", "numa_policy": policy}),
                             MARKER + "\n[Service]\nNUMAPolicy=" + policy + "\nNUMAMask=\n")

    def test_environment_order_is_stable_and_values_stay_single_directive(self):
        actual = render_dropin({"unit": "CubeMaster.service", "runtime": "go", "go_env": {
            "GODEBUG": "gctrace=1,schedtrace=1000", "GOMEMLIMIT": "8GiB", "GOMAXPROCS": "32"}})
        self.assertEqual(actual.splitlines()[2:], ['Environment="GOMAXPROCS=32"',
                         'Environment="GOMEMLIMIT=8GiB"', 'Environment="GODEBUG=gctrace=1,schedtrace=1000"',
                         'EnvironmentFile=/etc/systemd/system/CubeMaster.service.d/zzzz-machconf-tune.env'])

    def test_owned_environment_file_is_safe_and_stably_ordered(self):
        service = {"unit": "CubeMaster.service", "runtime": "go", "go_env": {
            "GODEBUG": "gctrace=1,schedtrace=1000", "GOGC": "400", "GOMAXPROCS": "8"}}
        self.assertEqual(environment_path(service),
                         "/etc/systemd/system/CubeMaster.service.d/zzzz-machconf-tune.env")
        self.assertEqual(render_environment(service),
                         MARKER + "\nGOMAXPROCS=8\nGOGC=400\nGODEBUG=gctrace=1,schedtrace=1000\n")

    def test_absent_go_settings_render_empty_owned_environment_without_reference(self):
        service = {"unit": "Cubelet.service", "cpu_affinity": "0-3"}
        self.assertEqual(render_environment(service), MARKER + "\n")
        self.assertNotIn("EnvironmentFile=", render_dropin(service))

    def test_owned_environment_revalidates_unsafe_unit_and_value(self):
        for service in ({"unit": "../bad.service", "cpu_affinity": "0"},
                        {"unit": "CubeMaster.service", "runtime": "go", "go_env": {"GOGC": "1\nBAD=1"}}):
            for render in (environment_path, render_environment):
                with self.subTest(render=render.__name__, service=service), self.assertRaises(ConfigError):
                    render(service)

    def test_render_revalidates_input(self):
        with self.assertRaises(ConfigError):
            render_dropin({"unit": "CubeMaster.service", "cpu_affinity": "0\nExecStart=/bin/evil"})


if __name__ == "__main__":
    unittest.main()
