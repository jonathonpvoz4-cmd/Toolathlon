import unittest
from scripts.containerized.container_mounts import validate_mounts


class ModelMountTests(unittest.TestCase):
    def setUp(self):
        self.bindings = [('/tmp/task-output', '/workspace/dumps', True),
                         ('/tmp/notion.json', '/workspace/notion.json', False)]
        self.mounts = [dict(Type='bind', Source=source, Destination=target, RW=rw)
                       for source, target, rw in self.bindings]

    def test_expected_mounts_and_private_task_volume(self):
        validate_mounts(self.mounts, self.bindings)
        volume = dict(Type='volume', Source='/var/lib/docker/volumes/tasks/_data',
                      Name='tasks', Destination='/workspace/tasks', RW=True)
        validate_mounts([*self.mounts, volume], self.bindings, 'tasks')

    def test_socket_unexpected_target_wrong_source_and_writable_overlay_rejected(self):
        for extra in [
            dict(Type='bind', Source='/var/run/docker.sock', Destination='/hidden', RW=True),
            dict(Type='bind', Source='/tmp/task-output', Destination='/other', RW=True),
        ]:
            with self.assertRaises(ValueError):
                validate_mounts([*self.mounts, extra], self.bindings)
        for key, value in [('Source', '/tmp/other'), ('RW', True)]:
            altered = [dict(m) for m in self.mounts]
            altered[1][key] = value
            with self.assertRaises(ValueError):
                validate_mounts(altered, self.bindings)
        with self.assertRaises(ValueError):
            validate_mounts(self.mounts[:1], self.bindings)
