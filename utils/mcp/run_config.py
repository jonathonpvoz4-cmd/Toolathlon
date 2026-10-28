"""Resolve run-private files for regular and PEP 420 namespace config packages."""
from pathlib import Path

from utils.mcp.workspace_credentials import CredentialError


def run_config_file(name: str) -> Path:
    if name not in {'hub-http-routes.json', 'hub-account-values.json'}:
        raise CredentialError('Unsupported Hub run configuration file')
    import configs
    origin = getattr(configs, '__file__', None)
    roots = [Path(origin).parent] if origin else [Path(path) for path in getattr(configs, '__path__', ())]
    candidates = { (root/name).resolve() for root in roots if (root/name).is_file() }
    if len(candidates) != 1:
        raise CredentialError('Hub run configuration is missing or ambiguous; start a new workspace terminal')
    return candidates.pop()
