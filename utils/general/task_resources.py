"""Resolve task resources against the account and instance used by this run."""

from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def instance_suffix(config_path=None):
    path = Path(config_path) if config_path is not None else PROJECT_ROOT / "configs/ports_config.yaml"
    if not path.exists():
        return ""
    config = yaml.safe_load(path.read_text()) or {}
    if not isinstance(config, dict):
        raise ValueError("ports_config.yaml must contain a mapping")
    suffix = config.get("instance_suffix") or ""
    if not isinstance(suffix, str):
        raise ValueError("instance_suffix must be a string")
    return suffix


def task_kubeconfig_path(task_dir, cluster_name):
    filename = f"{cluster_name}{instance_suffix()}-config.yaml"
    return str(Path(task_dir).resolve() / "k8s_configs" / filename)


def default_kubeconfig_path():
    filename = f"cluster{instance_suffix()}1-config.yaml"
    return str(PROJECT_ROOT / "deployment/k8s/configs" / filename)


def task_woocommerce_url(site_path, *, account_url=None):
    if account_url is None:
        from configs.token_key_session import all_token_key_session

        account_url = all_token_key_session.woocommerce_site_url
    endpoint = urlsplit(account_url)
    if endpoint.scheme not in {"http", "https"} or not endpoint.hostname:
        raise ValueError("woocommerce_site_url must be an HTTP(S) URL")
    if not site_path.startswith("/") or site_path.startswith("//"):
        raise ValueError("A task must specify its WooCommerce site path")
    return urlunsplit((endpoint.scheme, endpoint.netloc, site_path, "", ""))
