"""Untrusted tool text cannot monopolize the interpreter and starve leases."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from utils.answer_material_policy import _join_string_literals, protected_source


@pytest.mark.parametrize(('source', 'expected'), [
    ('"abc" + "def"', 'abcdef'),
    ("'abc' \n + \t 'def' + 'ghi'", 'abcdefghi'),
    ('"abc" + name + "def"', '"abc" + name + "def"'),
    ('"abc" + "def"; "ghi" + "jkl"', 'abcdef; ghijkl'),
    (r'''"a\"b" + 'c' ''', 'a"bc '),
    ('"a" + "b"; "unterminated', 'ab; "unterminated'),
    (r'''"\xZZ" + "abc"''', r'''"\xZZ" + "abc"'''),
    ('''"text with 'a' + 'b' inside"''', '''"text with 'a' + 'b' inside"'''),
])
def test_literal_scanner_preserves_quoted_chain_semantics(source, expected):
    assert _join_string_literals(source) == expected


def test_large_output_does_not_hide_a_split_answer_source():
    prefix = '\\"' * 50000
    source = prefix + '\nfetch("https://data.auto" + "benchaudit.com/answer")'
    assert protected_source(source)


def test_escaped_output_filter_keeps_hub_renewals_running():
    # Process isolation bounds the test even if a C regex holds the GIL again.
    code = textwrap.dedent('''
        import asyncio
        import json
        import time
        from types import SimpleNamespace
        import httpx
        from utils.answer_material_policy import filtered_output
        from utils.mcp.hub_transport import HubTaskServer

        async def main():
            calls = []
            ticks = []
            async def heartbeat():
                while True:
                    ticks.append(time.monotonic())
                    await asyncio.sleep(.02)
            async def put(*args, **kwargs):
                calls.append(time.monotonic())
                return httpx.Response(200, json={'success': True})
            server = HubTaskServer('fetch', None, SimpleNamespace(settings=SimpleNamespace(
                root='http://hub.invalid', headers={})), {})
            server.connection = {'id': 'lease'}
            server.http = SimpleNamespace(put=put)
            server.failed = False
            server.lease_ttl = 1.5
            server.lease_deadline = time.monotonic() + server.lease_ttl
            value = '"' + ('\\\\"' * 2000000) + '+'
            renewal = asyncio.create_task(server._renew())
            ticker = asyncio.create_task(heartbeat())
            started = time.monotonic()
            try:
                assert await filtered_output(value, 'fetch') == value
                await asyncio.sleep(server.lease_ttl / 3)
                assert not server.failed
                assert calls, 'filtering prevented all lease renewals'
                assert len(ticks) > 2
                assert max(b - a for a, b in zip([started] + ticks, ticks + [time.monotonic()])) < server.lease_ttl
                assert max(b - a for a, b in zip([started] + calls, calls + [time.monotonic()])) < server.lease_ttl
                print(json.dumps({'seconds': time.monotonic() - started, 'renewals': len(calls)}))
            finally:
                renewal.cancel()
                ticker.cancel()
                try:
                    await renewal
                except asyncio.CancelledError:
                    pass
                try:
                    await ticker
                except asyncio.CancelledError:
                    pass
        asyncio.run(main())
    ''')
    env = dict(os.environ)
    root = str(Path(__file__).resolve().parents[3])
    env['PYTHONPATH'] = root + os.pathsep + env.get('PYTHONPATH', '')
    result = subprocess.run([sys.executable, '-P', '-c', code], env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
