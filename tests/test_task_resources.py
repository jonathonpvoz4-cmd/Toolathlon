import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.general import task_resources


class TaskResourcesTests(unittest.TestCase):
    def test_task_and_default_clusters_follow_current_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "configs").mkdir()
            config = root / "configs/ports_config.yaml"
            config.write_text('instance_suffix: "-inst-alpha"\n')
            with patch.object(task_resources, "PROJECT_ROOT", root):
                for cluster in ["cluster-cleanup", "cluster-mysql", "cluster-pr-preview",
                                "cluster-redis-helm", "cluster-safety-audit"]:
                    actual = task_resources.task_kubeconfig_path(root / "task", cluster)
                    self.assertEqual(Path(actual).name, f"{cluster}-inst-alpha-config.yaml")
                self.assertEqual(Path(task_resources.default_kubeconfig_path()).name,
                                 "cluster-inst-alpha1-config.yaml")
                config.write_text('instance_suffix: "-account-b"\n')
                self.assertEqual(Path(task_resources.task_kubeconfig_path(root, "cluster-mysql")).name,
                                 "cluster-mysql-account-b-config.yaml")

    def test_no_instance_config_keeps_original_names(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(task_resources, "PROJECT_ROOT", Path(directory)):
                self.assertEqual(Path(task_resources.task_kubeconfig_path(directory, "cluster-mysql")).name,
                                 "cluster-mysql-config.yaml")
                self.assertEqual(Path(task_resources.default_kubeconfig_path()).name,
                                 "cluster1-config.yaml")

    def test_woocommerce_preserves_task_site_on_current_account_endpoint(self):
        for site in ["/store81", "/store97"]:
            self.assertEqual(task_resources.task_woocommerce_url(
                site, account_url="http://localhost:11003/store100"),
                f"http://localhost:11003{site}")
            self.assertEqual(task_resources.task_woocommerce_url(
                site, account_url="https://account-b.example:8443/store100"),
                f"https://account-b.example:8443{site}")

    def test_invalid_instance_config_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "ports.yaml"
            for content in ["- unexpected-list\n", "instance_suffix: 42\n"]:
                config.write_text(content)
                with self.assertRaises(ValueError):
                    task_resources.instance_suffix(config)


if __name__ == "__main__":
    unittest.main()
