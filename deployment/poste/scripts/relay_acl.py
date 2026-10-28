"""Build Poste's local benchmark relay ACL from its actual container network."""
import argparse
import ipaddress
import json
import os
import re
import subprocess


# Preserve the benchmark's existing private-network policy.
DEFAULT_SOURCES = ('127.0.0.0/8', '192.168.0.0/16', '172.16.0.0/12', '10.0.0.0/8')


def relay_sources(inspected, extra=''):
    sources = list(DEFAULT_SOURCES)
    # Host-network tasks access the published SMTP port through this gateway.
    # Trust that precise address, not every container or a guessed 172.x subnet.
    networks = inspected.get('NetworkSettings', {}).get('Networks', {})
    for network in networks.values():
        for field in ('Gateway', 'IPv6Gateway'):
            value = network.get(field)
            if value:
                address = ipaddress.ip_address(value)
                sources.append(f'{address}/{address.max_prefixlen}')
    for value in filter(None, re.split(r'[,\s]+', extra.strip())):
        network = ipaddress.ip_network(value, strict=False)
        if network.prefixlen == 0:
            raise ValueError('SMTP trusted sources must not allow every address')
        sources.append(str(network))
    return list(dict.fromkeys(sources))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', choices=['docker', 'podman'], required=True)
    parser.add_argument('--container', required=True)
    args = parser.parse_args()
    try:
        result = subprocess.run([args.runtime, 'inspect', '--', args.container],
                                check=True, capture_output=True, text=True, timeout=15)
        inspected = json.loads(result.stdout)
        if not isinstance(inspected, list) or len(inspected) != 1:
            raise ValueError('Expected one mail container')
        sources = relay_sources(inspected[0], os.getenv('POSTE_SMTP_TRUSTED_SOURCES', ''))
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
        parser.exit(1, 'Cannot build SMTP relay ACL; check the container network and POSTE_SMTP_TRUSTED_SOURCES.\n')
    print('\n'.join(sources))


if __name__ == '__main__':
    main()
