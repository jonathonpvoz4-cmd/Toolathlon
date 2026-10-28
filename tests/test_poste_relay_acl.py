import importlib.util
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / 'deployment/poste/scripts/relay_acl.py'
spec = importlib.util.spec_from_file_location('poste_relay_acl', PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize('gateway', ['172.80.0.1', '172.91.2.1', '10.88.0.1'])
def test_actual_gateway_is_trusted_without_trusting_the_entire_subnet(gateway):
    inspected = {'NetworkSettings': {'Networks': {'arbitrary-name': {'Gateway': gateway}}}}
    sources = module.relay_sources(inspected)
    assert f'{gateway}/32' in sources
    assert f'{gateway.rsplit(".", 1)[0]}.0/24' not in sources
    assert all(source in sources for source in module.DEFAULT_SOURCES)


def test_multiple_networks_ipv6_and_explicit_remote_host():
    inspected = {'NetworkSettings': {'Networks': {
        'a': {'Gateway': '172.80.0.1', 'IPv6Gateway': 'fd00::1'},
        'b': {'Gateway': '172.80.0.1'},
    }}}
    sources = module.relay_sources(inspected, '203.0.113.7, 192.0.2.0/28\n203.0.113.7')
    assert sources.count('172.80.0.1/32') == 1
    assert sources.count('203.0.113.7/32') == 1
    assert 'fd00::1/128' in sources
    assert '192.0.2.0/28' in sources


@pytest.mark.parametrize('extra', ['0.0.0.0/0', '::/0', 'not-an-ip', '192.0.2.1; touch file'])
def test_invalid_or_open_relay_sources_are_rejected(extra):
    with pytest.raises(ValueError):
        module.relay_sources({}, extra)


def test_host_network_or_empty_inspect_does_not_guess_a_bridge():
    assert module.relay_sources({'NetworkSettings': {'Networks': {}}}) == list(module.DEFAULT_SOURCES)
