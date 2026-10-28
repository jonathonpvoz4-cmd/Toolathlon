"""Opt-in HTTP routing for the Hub's declared arXiv Relay adapter.

Loaded by task MCP interpreters, not the benchmark/model process. Upstream MCP
entrypoints, schemas, filenames and returned content are left unchanged.
"""
import os
import sys

if os.getenv('MCPHUB_RELAY_ADAPTER') == 'fetch-v1' and os.getenv('MCP_RELAY_URL'):
    try:
        from arxiv_mcp_compat import install_httpx_relay, install_requests_relay
        install_httpx_relay()
        install_requests_relay()
    except Exception:
        print('Hub arXiv Relay adapter initialization failed; refusing direct fallback.', file=sys.stderr)
        os._exit(78)
