"""Resolve an optional full Serper forwarding URL without leaking provider keys."""
import os
from urllib.parse import urlsplit


def resolve_serper_endpoint(default_url: str, default_key: str) -> tuple[str, str]:
    url = os.getenv('SERPER_API_URL', '').strip()
    if not url and os.getenv('MCP_TOOL_TRANSPORT', 'direct').strip().lower() == 'hub':
        root = os.getenv('MCPHUB_URL', '').strip().rstrip('/')
        if not root:
            raise ValueError('Hub search requires MCPHUB_URL')
        url = root + '/http/serper/search'
    if not url:
        return default_url, default_key
    parsed = urlsplit(url)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('SERPER_API_URL must be a complete HTTP(S) endpoint')
    key = os.getenv('SERPER_API_KEY', '').strip() or os.getenv('MCPHUB_TOKEN', '').strip()
    if not key:
        raise ValueError('SERPER_API_URL requires SERPER_API_KEY or MCPHUB_TOKEN')
    return url, key
