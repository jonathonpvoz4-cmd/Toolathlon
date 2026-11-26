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


def test_guard_trust_is_limited_to_exact_gateways_and_clears_only_their_bans(monkeypatch):
    calls = []
    monkeypatch.setattr(module, 'wait_for_mail_ready', lambda *args: None)
    monkeypatch.setattr(module.subprocess, 'run', lambda args, **kwargs: calls.append(args))
    inspected = {'NetworkSettings': {'Networks': {
        'a': {'Gateway': '172.17.0.1', 'IPv6Gateway': 'fd00::123'},
    }}}
    module.trust_gateways('docker', 'poste', inspected)
    assert calls[0][:5] == ['docker', 'exec', 'poste', 'php', '-r']
    assert module.json.loads(calls[0][-1]) == ['172.17.0.1/32', 'fd00::123/128']
    assert calls[1] == ['docker', 'exec', 'poste', 'redis-cli', '-s', '/var/run/redis/redis.sock',
                        'DEL', 'guard|172.17.0.1/32', 'bbc|172.17.0.1/32',
                        'guard|fd00::/56', 'bbc|fd00::/56']


def test_guard_configuration_failure_stops_before_clearing_bans(monkeypatch):
    monkeypatch.setattr(module, 'wait_for_mail_ready', lambda *args: None)
    def fail(*args, **kwargs):
        raise module.subprocess.CalledProcessError(1, args[0])
    monkeypatch.setattr(module.subprocess, 'run', fail)
    with pytest.raises(module.subprocess.CalledProcessError):
        module.trust_gateways('docker', 'poste', {
            'NetworkSettings': {'Networks': {'bridge': {'Gateway': '172.17.0.1'}}}})


def test_failure_diagnostics_keep_stage_and_stderr_without_command_or_secrets(monkeypatch, capsys):
    monkeypatch.setenv('POSTE_PASSWORD', 'private-test-password')
    error = module.subprocess.CalledProcessError(
        1, ['docker', 'exec', 'sensitive-command'],
        output='inspect environment must not be printed',
        stderr='SQLSTATE: table guard missing; password=private-test-password; Authorization: Bearer unrelated-credential')
    module.report_failure('write mail database gateway whitelist', error)
    text = capsys.readouterr().err
    assert 'stage=write mail database gateway whitelist' in text
    assert 'exit_code=1' in text and 'table guard missing' in text
    assert 'private-test-password' not in text
    assert 'unrelated-credential' not in text
    assert 'sensitive-command' not in text and 'inspect environment' not in text


def test_timeout_diagnostics_preserve_stage_and_partial_stderr(capsys):
    error = module.subprocess.TimeoutExpired(['docker', 'exec'], 15, stderr=b'Redis socket unavailable')
    module.report_failure('clear gateway automatic bans in mail Redis', error)
    text = capsys.readouterr().err
    assert 'error=TimeoutExpired' in text and 'timeout_seconds=15' in text
    assert 'Redis socket unavailable' in text


def test_no_whitelist_writes_until_database_and_redis_are_ready(monkeypatch):
    from types import SimpleNamespace
    steps = []
    responses = iter([
        SimpleNamespace(returncode=75, stderr='no such table: guard'),
        SimpleNamespace(returncode=75, stderr='Mail Redis socket is not ready'),
        SimpleNamespace(returncode=0, stderr=''),
    ])
    def run(args, **kwargs):
        if args[5] == module.MAIL_READY_PHP:
            steps.append('probe')
            return next(responses)
        steps.append('write' if 'php' in args else 'clear')
    monkeypatch.setattr(module.subprocess, 'run', run)
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: None)
    module.trust_gateways('docker', 'poste', {
        'NetworkSettings': {'Networks': {'bridge': {'Gateway': '172.17.0.1'}}}})
    assert steps == ['probe', 'probe', 'probe', 'write', 'clear']


def test_readiness_timeout_keeps_last_reason_and_performs_no_writes(monkeypatch, capsys):
    from types import SimpleNamespace
    clock = [0.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=75, stderr='no such table: guard')
    monkeypatch.setattr(module.subprocess, 'run', run)
    with pytest.raises(module.subprocess.TimeoutExpired):
        module.trust_gateways('docker', 'poste', {
            'NetworkSettings': {'Networks': {'bridge': {'Gateway': '172.17.0.1'}}}})
    assert clock[0] == 120
    assert all(args[-1] == module.MAIL_READY_PHP for args in calls)
    assert 'no such table: guard' in capsys.readouterr().err


def test_unexpected_probe_error_fails_immediately(monkeypatch):
    from types import SimpleNamespace
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=255, stderr='database disk image is malformed')
    monkeypatch.setattr(module.subprocess, 'run', run)
    with pytest.raises(module.subprocess.CalledProcessError):
        module.wait_for_mail_ready('docker', 'poste')
    assert len(calls) == 1
