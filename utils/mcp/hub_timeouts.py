"""Separate upstream inactivity budgets from finite Hub admission/transport waits."""
from __future__ import annotations

import math
import os
import httpx

HUB_CONNECTION_TIMEOUT_SECONDS = 15.0
HUB_CONTROL_TIMEOUT_SECONDS = 15.0
HUB_STARTUP_TIMEOUT_SECONDS = 300.0
UPSTREAM_TIMEOUT_HEADER = 'X-MCPHub-Upstream-Timeout-Ms'


def _positive(value, name):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f'{name} must be positive and finite')
    return value


def queue_allowance_seconds():
    # An outer safety net, not a new Hub rate/queue policy.
    return _positive(os.getenv('MCPHUB_QUEUE_WAIT_SECONDS') or 600, 'MCPHUB_QUEUE_WAIT_SECONDS')


def upstream_seconds(timeout=None):
    """Keep an SDK's read timeout; only absent/unlimited reads use the fallback.

    Requests scalars/tuples, urllib3 Timeout, HTTPX objects and request extensions
    are supported without interpreting a connect-only value as the read budget.
    """
    if isinstance(timeout, dict):
        value = timeout.get('read')
    elif isinstance(timeout, (tuple, list)):
        value = timeout[1]
    elif isinstance(timeout, httpx.Timeout):
        value = timeout.read
    elif timeout is not None and hasattr(type(timeout), 'read_timeout'):
        try:
            value = timeout.read_timeout
        except Exception as error:
            # urllib3 Timeout(total=...) has no read duration until connect starts.
            if type(error).__name__ != 'TimeoutStateError':
                raise
            value = timeout.total
    else:
        value = timeout
    if value is None:
        value = os.getenv('MCPHUB_UPSTREAM_TIMEOUT_SECONDS') or 300
    return _positive(value, 'Upstream read timeout')


def upstream_timeout_headers(timeout=None):
    return {UPSTREAM_TIMEOUT_HEADER: str(max(1, math.ceil(upstream_seconds(timeout) * 1000)))}


def transport_wait_seconds(timeout=None):
    return upstream_seconds(timeout) + queue_allowance_seconds() + HUB_CONNECTION_TIMEOUT_SECONDS


def hub_http_timeout(timeout=None) -> httpx.Timeout:
    wait = transport_wait_seconds(timeout)
    return httpx.Timeout(connect=HUB_CONNECTION_TIMEOUT_SECONDS, read=wait,
                         write=wait, pool=HUB_CONNECTION_TIMEOUT_SECONDS)


def hub_requests_timeout(timeout=None) -> tuple[float, float]:
    return HUB_CONNECTION_TIMEOUT_SECONDS, transport_wait_seconds(timeout)


def discovery_timeout(config):
    return _positive(config.get('client_session_timeout_seconds') or 5, 'MCP client timeout')
