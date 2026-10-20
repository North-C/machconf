"""Read-only, standalone Python 3.8+ runtime evidence for Cube tuning.

Samples are not atomic. A stable PID, executable and thread inventory are
required for successful checks. Environment is launch-time evidence, not Go's
internal live state. numa_maps exposes VMA policy, not get_mempolicy(2) for each
thread or proof that existing pages moved.
"""

import os
import re

_TV_GO_KEYS = frozenset(('GOMAXPROCS', 'GOGC', 'GOMEMLIMIT', 'GOTRACEBACK', 'GODEBUG'))
_TV_MAX_READ = 8 * 1024 * 1024
_TV_MAX_THREADS = 32768


def _tv_error(exc):
    if isinstance(exc, PermissionError):
        return 'permission-denied'
    if isinstance(exc, FileNotFoundError):
        return 'not-found-or-process-exited'
    return 'invalid-or-unavailable-data'


def _tv_read(path, binary=False):
    with open(path, 'rb') as stream:
        raw = stream.read(_TV_MAX_READ + 1)
    if len(raw) > _TV_MAX_READ:
        raise ValueError('read-limit-exceeded')
    return raw if binary else raw.decode('utf-8', errors='replace')


def _tv_ids(value):
    """Parse Linux comma/range sets without accepting malformed empty parts."""
    if not isinstance(value, str) or not value or not re.fullmatch(r'[0-9,-]+', value):
        raise ValueError('invalid-id-list')
    result = set()
    for part in value.split(','):
        match = re.fullmatch(r'(\d+)(?:-(\d+))?', part)
        if not match:
            raise ValueError('invalid-id-list')
        first, last = int(match[1]), int(match[2] or match[1])
        if first > last or last > 1048575:
            raise ValueError('invalid-id-range')
        result.update(range(first, last + 1))
    return result


def _tv_starttime(directory, pid):
    raw = _tv_read(os.path.join(directory, 'stat'))
    if int(raw.split(' ', 1)[0]) != pid:
        raise ValueError('pid-mismatch')
    fields = raw[raw.rindex(')') + 2:].split()
    return int(fields[19])


def _tv_tids(directory):
    tids = sorted(int(name) for name in os.listdir(os.path.join(directory, 'task')) if name.isdigit())
    if not tids or len(tids) > _TV_MAX_THREADS:
        raise ValueError('empty-or-too-many-threads')
    return tids


def _tv_numa_maps(directory):
    result = {'status': 'unavailable', 'policies': {}, 'mapping_count': 0,
              'note': 'VMA policies only; not per-thread default policy or proof of page migration'}
    try:
        raw = _tv_read(os.path.join(directory, 'numa_maps'))
        for line in raw.splitlines():
            fields = line.split()
            if len(fields) < 2:
                raise ValueError('invalid-numa-maps')
            # Drop VMA addresses, file paths, and all non-policy fields.
            token = fields[1]
            if not re.fullmatch(r'(?:default|local|bind|interleave|prefer|preferred|preferred_many|weighted_interleave)(?:[:=][0-9,-]+)?(?:\([a-z_|]+\))?', token):
                raise ValueError('unrecognized-numa-policy')
            result['policies'][token] = result['policies'].get(token, 0) + 1
            result['mapping_count'] += 1
        result['status'] = 'ok' if result['mapping_count'] else 'unavailable'
    except (OSError, ValueError) as exc:
        result['error'] = _tv_error(exc)
    return result


def _tv_unescape_mount(value):
    return re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), value)


def _tv_contained(path, root):
    return path == root or path.startswith(root.rstrip('/') + '/')


def _tv_cgroups(directory, proc_root):
    out = {'status': 'unavailable', 'memberships': [], 'errors': []}
    try:
        membership_raw = _tv_read(os.path.join(directory, 'cgroup'))
        mounts = []
        for line in _tv_read(os.path.join(proc_root, 'self', 'mountinfo')).splitlines():
            before, sep, after = line.partition(' - ')
            fields, tail = before.split(), after.split()
            if not sep or len(fields) < 6 or len(tail) < 3:
                continue
            if tail[0] == 'cgroup2' or (tail[0] == 'cgroup' and 'cpuset' in tail[2].split(',')):
                mounts.append({'version': '2' if tail[0] == 'cgroup2' else '1',
                               'root': _tv_unescape_mount(fields[3]),
                               'point': _tv_unescape_mount(fields[4])})
        for line in membership_raw.splitlines():
            hierarchy, controllers, path = line.split(':', 2)
            if controllers and 'cpuset' not in controllers.split(','):
                continue
            version = '1' if controllers else '2'
            if not path.startswith('/') or '..' in path.split('/'):
                raise ValueError('invalid-cgroup-path')
            entry = {'version': version, 'path': path, 'levels': [], 'errors': []}
            out['memberships'].append(entry)
            candidates = [item for item in mounts if item['version'] == version and _tv_contained(path, item['root'])]
            if not candidates:
                entry['errors'].append('matching-cpuset-mount-unavailable')
                continue
            mount = min(candidates, key=lambda item: len(item['root']))
            point = os.path.normpath(mount['point'])
            target = os.path.normpath(os.path.join(point, path[len(mount['root']):].lstrip('/')))
            if not _tv_contained(target, point):
                raise ValueError('invalid-cgroup-path')
            names = ('cpuset.cpus', 'cpuset.mems', 'cpuset.cpus.effective', 'cpuset.mems.effective') if version == '2' else ('cpuset.cpus', 'cpuset.mems', 'cpuset.effective_cpus', 'cpuset.effective_mems')
            for unused in range(128):
                level = {'values': {}, 'errors': []}
                entry['levels'].append(level)
                for name in names:
                    try:
                        value = _tv_read(os.path.join(target, name)).strip()
                        if value:
                            _tv_ids(value)
                        level['values'][name] = value
                    except FileNotFoundError:
                        continue
                    except (OSError, ValueError) as exc:
                        level['errors'].append(name + ':' + _tv_error(exc))
                if target == point:
                    break
                target = os.path.dirname(target)
            else:
                entry['errors'].append('ancestor-depth-limit-exceeded')
        has_values = any(level['values'] for entry in out['memberships'] for level in entry['levels'])
        has_errors = any(entry['errors'] or any(level['errors'] for level in entry['levels']) for entry in out['memberships'])
        out['status'] = ('partial' if has_errors else 'ok') if has_values else 'unavailable'
    except (OSError, ValueError) as exc:
        out['errors'].append(_tv_error(exc))
    return out


def inspect_process(pid, proc_root='/proc'):
    """Return bounded, redacted proc/cpuset evidence; never change a process."""
    out = {'pid': pid, 'status': 'unavailable', 'identity': {'status': 'unavailable'},
           'threads': {'status': 'unavailable', 'items': []}, 'go_env': {},
           'go_env_status': 'unavailable', 'numa_maps': {'status': 'unavailable'},
           'cgroups': {'status': 'unavailable', 'memberships': []}, 'errors': []}
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        out['errors'].append('invalid-pid')
        return out
    directory = os.path.join(proc_root, str(pid))
    try:
        before = _tv_starttime(directory, pid)
        exe = os.readlink(os.path.join(directory, 'exe'))
        out['identity'].update(starttime_ticks=before, exe=exe)
        tids_before = _tv_tids(directory)
    except (OSError, ValueError, IndexError) as exc:
        out['errors'].append('initial-identity:' + _tv_error(exc))
        return out
    for tid in tids_before:
        item = {'tid': tid, 'status': 'unavailable'}
        try:
            raw = _tv_read(os.path.join(directory, 'task', str(tid), 'status'))
            values = dict(line.split(':', 1) for line in raw.splitlines() if ':' in line)
            for source, target in (('Cpus_allowed_list', 'cpus'), ('Mems_allowed_list', 'mems')):
                value = values[source].strip()
                _tv_ids(value)
                item[target] = value
            item['status'] = 'ok'
        except (OSError, ValueError, KeyError) as exc:
            item['error'] = _tv_error(exc)
        out['threads']['items'].append(item)
    try:
        entries = _tv_read(os.path.join(directory, 'environ'), binary=True).split(b'\0')
        for entry in entries:
            key, sep, value = entry.partition(b'=')
            name = key.decode('ascii', errors='replace')
            if sep and name in _TV_GO_KEYS:
                if name in out['go_env']:
                    raise ValueError('duplicate-environment-name')
                out['go_env'][name] = value.decode('utf-8', errors='replace')
        out['go_env_status'] = 'ok'
    except (OSError, ValueError) as exc:
        out['errors'].append('environ:' + _tv_error(exc))
    out['numa_maps'] = _tv_numa_maps(directory)
    out['cgroups'] = _tv_cgroups(directory, proc_root)
    try:
        stable = (_tv_starttime(directory, pid) == before and
                  os.readlink(os.path.join(directory, 'exe')) == exe)
        tids_after = _tv_tids(directory)
        same_threads = tids_after == tids_before
    except (OSError, ValueError, IndexError):
        stable, same_threads = False, False
    out['identity']['status'] = 'ok' if stable else 'partial'
    out['threads']['status'] = 'ok' if (stable and same_threads and all(item['status'] == 'ok' for item in out['threads']['items'])) else 'partial'
    if not stable:
        out['errors'].append('process-exited-execed-or-pid-reused-during-sampling')
    if not same_threads:
        out['errors'].append('thread-inventory-changed-during-sampling')
    out['status'] = 'ok' if (stable and out['threads']['status'] == 'ok' and out['go_env_status'] == 'ok' and out['numa_maps']['status'] == 'ok' and out['cgroups']['status'] == 'ok') else 'partial'
    return out


def _tv_policy_matches(token, policy, nodes):
    # Kernel preferred policy is printed as "prefer". Policy mode flags do not
    # change the requested mode, but relative nodes cannot be resolved here.
    if '(' in token:
        return False
    name, separator, value = token.partition(':')
    if not separator:
        name, separator, value = token.partition('=')
    name = {'prefer': 'preferred'}.get(name, name)
    if name != policy:
        return False
    if policy in ('default', 'local'):
        return not separator
    return bool(separator and _tv_ids(value) == _tv_ids(nodes))


def verify_process(observation, requested):
    """Validate only requested settings; missing/unreadable evidence fails closed."""
    checks = []
    stable = observation.get('identity', {}).get('status') == 'ok'
    checks.append({'check': 'process_identity', 'ok': stable,
                   'detail': 'PID/starttime/executable stable during sampling' if stable else 'Process identity unavailable or changed during sampling'})
    threads = observation.get('threads', {})
    complete = stable and threads.get('status') == 'ok' and bool(threads.get('items'))
    for key, field in (('cpu_affinity', 'cpus'), ('cpuset_cpus', 'cpus'), ('cpuset_mems', 'mems')):
        if key not in requested:
            continue
        try:
            wanted = _tv_ids(requested[key])
            ok = bool(complete and all(item.get('status') == 'ok' and _tv_ids(item.get(field)) <= wanted for item in threads['items']))
        except (ValueError, TypeError):
            ok = False
        detail = 'All sampled threads have nonempty allowed sets within requested set' if ok else 'Incomplete thread evidence or a thread allowed set exceeds the request'
        if key == 'cpuset_mems':
            detail += '; cpuset allowance does not prove page migration or NUMA memory policy'
        checks.append({'check': key, 'ok': ok, 'detail': detail})
    for key, value in requested.get('go_env', {}).items():
        ok = (stable and key in _TV_GO_KEYS and observation.get('go_env_status') == 'ok' and
              key in observation.get('go_env', {}) and observation['go_env'][key] == str(value))
        checks.append({'check': 'go_env.' + key, 'ok': bool(ok),
                       'detail': 'Launch environment matches; internal Go runtime values are not observed' if ok else 'Requested launch environment absent, unreadable, or mismatched; no default inferred'})
    if 'numa_policy' in requested:
        evidence = observation.get('numa_maps', {})
        try:
            ok = bool(stable and evidence.get('status') == 'ok' and evidence.get('policies') and
                      all(_tv_policy_matches(token, requested['numa_policy'], requested.get('numa_nodes', '')) for token in evidence['policies']))
        except (ValueError, TypeError):
            ok = False
        checks.append({'check': 'numa_policy', 'ok': ok,
                       'detail': ('All observed VMA policies match' if ok else 'VMA policy evidence unavailable or differs; application may override policy') +
                       '; only main-thread numa_maps observed, not per-thread default policy or proof of page migration'})
    return checks
