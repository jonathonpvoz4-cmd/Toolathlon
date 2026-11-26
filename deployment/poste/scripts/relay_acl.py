"""Build Poste's local benchmark relay ACL from its actual container network."""
import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys
import time


# Preserve the benchmark's existing private-network policy.
DEFAULT_SOURCES = ('127.0.0.0/8', '192.168.0.0/16', '172.16.0.0/12', '10.0.0.0/8')


def report_failure(stage, error):
    # Never print argv or stdout: inspect output contains the container env.
    detail = error.stderr if isinstance(error, subprocess.SubprocessError) else str(error)
    if isinstance(detail, bytes):
        detail = detail.decode(errors='replace')
    detail = detail or ''
    for name, value in os.environ.items():
        if len(value) >= 4 and re.search(r'(?i)password|secret|token|key', name):
            detail = detail.replace(value, '[REDACTED]')
    detail = re.sub(r'(?i)(https?://)[^\s/@]+:[^\s/@]+@', r'\1[REDACTED]@', detail)
    detail = re.sub(
        r'(?i)(authorization\s*[:=]\s*(?:bearer\s+)?|bearer\s+|(?:password|api[_-]?key|token|secret)\s*[:=]\s*)[^\s,;]+',
        r'\1[REDACTED]', detail)
    detail = ' '.join(detail.split())[-1600:]
    fields = f'SMTP relay ACL failed: stage={stage}; error={type(error).__name__}'
    if isinstance(error, subprocess.CalledProcessError):
        fields += f'; exit_code={error.returncode}'
    if isinstance(error, subprocess.TimeoutExpired):
        fields += f'; timeout_seconds={error.timeout}'
    if detail:
        fields += f'; detail={detail}'
    print(fields, file=sys.stderr, flush=True)


def run_step(stage, args, timeout):
    try:
        return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        report_failure(stage, error)
        raise


def gateway_sources(inspected):
    sources = []
    # Host-network tasks access the published SMTP port through this gateway.
    # Trust that precise address, not every container or a guessed 172.x subnet.
    networks = inspected.get('NetworkSettings', {}).get('Networks', {})
    for network in networks.values():
        for field in ('Gateway', 'IPv6Gateway'):
            value = network.get(field)
            if value:
                address = ipaddress.ip_address(value)
                sources.append(f'{address}/{address.max_prefixlen}')
    return list(dict.fromkeys(sources))


def relay_sources(inspected, extra=''):
    sources = list(DEFAULT_SOURCES) + gateway_sources(inspected)
    for value in filter(None, re.split(r'[,\s]+', extra.strip())):
        network = ipaddress.ip_network(value, strict=False)
        if network.prefixlen == 0:
            raise ValueError('SMTP trusted sources must not allow every address')
        sources.append(str(network))
    return list(dict.fromkeys(sources))


# Poste's relay ACL does not exempt clients from block_bad_connections.
# Use its native guard whitelist for the precise local bridge gateways only.
MAIL_READY_PHP = r'''
if (!is_file('/data/users.db')) {
    fwrite(STDERR, "Mail database has not been created\n");
    exit(75);
}
try {
    $db = new PDO('sqlite:/data/users.db', null, null, [
        PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
        PDO::SQLITE_ATTR_OPEN_FLAGS => PDO::SQLITE_OPEN_READONLY,
    ]);
    $db->query('SELECT identifier, block, priority, start, "end", comment FROM guard LIMIT 0');
} catch (PDOException $error) {
    if (strpos($error->getMessage(), 'no such table: guard') !== false ||
        strpos($error->getMessage(), 'database is locked') !== false) {
        fwrite(STDERR, "Mail guard table is not ready: ".$error->getMessage()."\n");
        exit(75);
    }
    throw $error;
}
$redis = @stream_socket_client('unix:///var/run/redis/redis.sock', $errno, $errstr, 1);
if (!$redis) {
    fwrite(STDERR, "Mail Redis socket is not ready\n");
    exit(75);
}
stream_set_timeout($redis, 1);
fwrite($redis, "*1\r\n$4\r\nPING\r\n");
$pong = fgets($redis);
fclose($redis);
if ($pong !== "+PONG\r\n") {
    fwrite(STDERR, "Mail Redis did not return PONG\n");
    exit(75);
}
'''


def wait_for_mail_ready(runtime, container):
    stage = 'wait for mail guard table and Redis'
    args = [runtime, 'exec', container, 'php', '-r', MAIL_READY_PHP]
    deadline = time.monotonic() + 120
    last_error = subprocess.TimeoutExpired(args, 120)
    print('Waiting for mail guard table and Redis readiness (up to 120s)', file=sys.stderr, flush=True)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            result = subprocess.run(args, capture_output=True, text=True,
                                    timeout=min(10, remaining))
            if result.returncode == 0:
                return
            last_error = subprocess.CalledProcessError(result.returncode, args, stderr=result.stderr)
            if result.returncode != 75:
                report_failure(stage, last_error)
                raise last_error
        except subprocess.TimeoutExpired as error:
            last_error = error
        except OSError as error:
            report_failure(stage, error)
            raise
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    error = subprocess.TimeoutExpired(args, 120, stderr=last_error.stderr)
    report_failure(stage, error)
    raise error


TRUST_GATEWAY_PHP = r'''
$db = new PDO('sqlite:/data/users.db');
$db->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
$db->exec('PRAGMA busy_timeout=5000');
$db->beginTransaction();
try {
    foreach (json_decode($argv[1], true, 512, JSON_THROW_ON_ERROR) as $source) {
        $ip = explode('/', $source)[0];
        $packed = inet_pton($ip);
        if ($packed === false) throw new RuntimeException('Invalid gateway address');
        $query = $db->prepare('SELECT block FROM guard WHERE identifier = ?');
        $query->execute([$source]);
        $row = $query->fetch(PDO::FETCH_ASSOC);
        if ($row && $row['block']) throw new RuntimeException('Gateway is explicitly blocked: '.$source);
        if (!$row) {
            $insert = $db->prepare('INSERT INTO guard (identifier, block, priority, start, "end", comment) VALUES (?, 0, 1000, ?, ?, ?)');
            $insert->bindValue(1, $source);
            $insert->bindValue(2, $packed, PDO::PARAM_LOB);
            $insert->bindValue(3, $packed, PDO::PARAM_LOB);
            $insert->bindValue(4, 'Toolathlon local bridge gateway');
            $insert->execute();
        }
    }
    $db->commit();
} catch (Throwable $error) {
    $db->rollBack();
    throw $error;
}
'''


def trust_gateways(runtime, container, inspected):
    sources = gateway_sources(inspected)
    if not sources:
        return
    wait_for_mail_ready(runtime, container)
    run_step('write mail database gateway whitelist',
             [runtime, 'exec', container, 'php', '-r', TRUST_GATEWAY_PHP, json.dumps(sources)], 30)
    # The native whitelist takes precedence over automatic bans. Remove only
    # this gateway's previous automatic ban/counter; leave other clients alone.
    keys = []
    for source in sources:
        address = ipaddress.ip_interface(source).ip
        subnet = (str(ipaddress.ip_network(f'{address}/56', strict=False))
                  if address.version == 6 else source)
        keys.extend((f'guard|{subnet}', f'bbc|{subnet}'))
    run_step('clear gateway automatic bans in mail Redis',
             [runtime, 'exec', container, 'redis-cli', '-s', '/var/run/redis/redis.sock', 'DEL', *keys], 15)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', choices=['docker', 'podman'], required=True)
    parser.add_argument('--container', required=True)
    parser.add_argument('--trust-gateways', action='store_true')
    args = parser.parse_args()
    stage = 'parse mail container network'
    try:
        result = run_step('inspect mail container',
                          [args.runtime, 'inspect', '--', args.container], 15)
        inspected = json.loads(result.stdout)
        if not isinstance(inspected, list) or len(inspected) != 1:
            raise ValueError('Expected one mail container')
        stage = 'validate SMTP trusted sources'
        sources = relay_sources(inspected[0], os.getenv('POSTE_SMTP_TRUSTED_SOURCES', ''))
        if args.trust_gateways:
            stage = 'parse gateway whitelist'
            trust_gateways(args.runtime, args.container, inspected[0])
    except (ValueError, TypeError, AttributeError) as error:
        report_failure(stage, error)
        parser.exit(1, 'Cannot build SMTP relay ACL; check the container network and POSTE_SMTP_TRUSTED_SOURCES.\n')
    except (OSError, subprocess.SubprocessError):
        parser.exit(1, 'Cannot build SMTP relay ACL; check the container network and POSTE_SMTP_TRUSTED_SOURCES.\n')
    print('\n'.join(sources))


if __name__ == '__main__':
    main()
