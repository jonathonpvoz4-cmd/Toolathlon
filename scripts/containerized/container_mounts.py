"""Verify the model container has only its explicitly declared task mounts."""
import argparse
import json
from pathlib import Path
import sys


def validate_mounts(mounts, bindings, task_volume=None):
    expected = {target: (str(Path(source).resolve()), writable)
                for source, target, writable in bindings}
    if task_volume:
        expected['/workspace/tasks'] = (task_volume, True)
    actual = set()
    for mount in mounts:
        target = mount['Destination']
        source = mount.get('Source', '')
        if any(name in source or name in target for name in
               ('docker.sock', 'podman.sock', 'containerd.sock')):
            raise ValueError('Model container must not mount a container-runtime socket')
        if target not in expected or target in actual:
            raise ValueError(f'Unexpected model mount: {target}')
        wanted, writable = expected[target]
        if target == '/workspace/tasks' and task_volume:
            valid = mount.get('Type') == 'volume' and mount.get('Name') == wanted
        else:
            valid = mount.get('Type') == 'bind' and str(Path(source).resolve()) == wanted
        if not valid or mount.get('RW') != writable:
            raise ValueError(f'Incorrect model mount source or permissions: {target}')
        actual.add(target)
    if actual != set(expected):
        raise ValueError(f'Missing model mounts: {sorted(set(expected) - actual)}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', action='append', nargs=3, default=[], metavar=('SOURCE', 'TARGET', 'MODE'))
    parser.add_argument('--task-volume')
    args = parser.parse_args()
    bindings = [(source, target, mode == 'rw') for source, target, mode in args.bind]
    validate_mounts(json.load(sys.stdin), bindings, args.task_volume)


if __name__ == '__main__':
    main()
