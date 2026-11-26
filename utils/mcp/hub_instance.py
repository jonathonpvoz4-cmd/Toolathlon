"""Apply declared local service addresses to a Hub run copy, never task semantics."""
import json
from pathlib import Path
import re


def reconcile_mail_ports(root):
    import yaml
    root = Path(root)
    configuration = root/'configs/ports_config.yaml'
    mapping = (yaml.safe_load(configuration.read_text()) or {}).get('port_mappings', {}) if configuration.exists() else {}
    mapping = {int(old): int(new) for old, new in mapping.items()}
    # The optional instance file is absent in clean images. Use the actual
    # Poste deployment for task configs that still contain original mail ports.
    deployment = root/'deployment/poste/scripts/setup.sh'
    if deployment.is_file():
        source = deployment.read_text()
        for old, name in ((1143, 'IMAP_PORT'), (2525, 'SMTP_PORT'), (1587, 'SUBMISSION_PORT')):
            match = re.search(rf'^{name}=(\d+)\b', source, re.M)
            if not match:
                raise ValueError(f'Missing deployed mail port: {name}')
            mapping[old] = int(match[1])
    if any(not 1 <= port <= 65535 for pair in mapping.items() for port in pair):
        raise ValueError('Invalid declared instance port')
    changed = 0

    def update(value):
        nonlocal changed
        if isinstance(value, list):
            for child in value:
                update(child)
        elif isinstance(value, dict):
            for protocol in ('imap', 'smtp', 'pop3'):
                key = protocol+'_port'
                # External mail servers have their own port policy.
                if value.get(protocol+'_server') in {'localhost', '127.0.0.1', '::1'}:
                    port = value.get(key)
                    if type(port) is int and port in mapping and mapping[port] != port:
                        value[key] = mapping[port]
                        changed += 1
            for child in value.values():
                update(child)

    for path in (root/'tasks').glob('*/*/**/*.json'):
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            continue
        before = changed
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError, UnicodeError):
            continue
        update(value)
        if changed != before:
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
    return changed
