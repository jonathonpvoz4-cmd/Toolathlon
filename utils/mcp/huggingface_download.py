"""Finite, replay-safe download handling for the Hugging Face Hub SDK backend."""
import time
import requests

from utils.mcp.workspace_http import AccountHTTPAdapter, AssetHTTPAdapter, hub_enabled
from utils.mcp.hub_timeouts import upstream_seconds

DOWNLOAD_READ_SECONDS = 120
DOWNLOAD_ATTEMPTS = 3


def download_timeout(timeout):
    """Leave longer caller budgets intact; retain a separate connect deadline."""
    if isinstance(timeout, (tuple, list)):
        connect, read = timeout
    elif timeout is None or isinstance(timeout, (int, float)):
        connect = read = timeout
    else:
        # urllib3 Timeout is accepted by requests as well.
        connect, read = timeout.connect_timeout, upstream_seconds(timeout)
    return (connect or 15, max(read or DOWNLOAD_READ_SECONDS, DOWNLOAD_READ_SECONDS))


def recover_incomplete_stream(response):
    """Classify a truncated Hub stream so the pinned SDK resumes with Range.

    No bytes are replayed here. The SDK owns the file offset and response retry.
    """
    original = response.iter_content
    def iter_content(*args, **kwargs):
        try:
            yield from original(*args, **kwargs)
        except requests.exceptions.ChunkedEncodingError:
            response.close()
            raise requests.ConnectionError('Hugging Face download stream interrupted') from None
    response.iter_content = iter_content


class DownloadRecovery:
    """Retry before response delivery only; the SDK still owns body resume/ranges."""
    def send(self, request, **kwargs):
        if not hub_enabled() or request.method not in {'GET', 'HEAD'}:
            return super().send(request, **kwargs)
        kwargs['timeout'] = download_timeout(kwargs.get('timeout'))
        for attempt in range(DOWNLOAD_ATTEMPTS):
            try:
                response = super().send(request, **kwargs)
            except (requests.Timeout, requests.ConnectionError) as error:
                if isinstance(error, requests.exceptions.SSLError) or attempt == DOWNLOAD_ATTEMPTS - 1:
                    raise
                reason = type(error).__name__
            else:
                if response.status_code not in {502, 503, 504} or attempt == DOWNLOAD_ATTEMPTS - 1:
                    if request.method == 'GET' and kwargs.get('stream'):
                        recover_incomplete_stream(response)
                    return response
                reason = f'HTTP {response.status_code}'
                response.close()
            # No URLs, signed query parameters, account keys or run capabilities in logs.
            print(f'[huggingface] retrying download after {reason} '
                  f'({attempt + 2}/{DOWNLOAD_ATTEMPTS})', flush=True)
            time.sleep(0.5 * (attempt + 1))


class HuggingFaceAccountAdapter(DownloadRecovery, AccountHTTPAdapter):
    pass


class HuggingFaceAssetAdapter(DownloadRecovery, AssetHTTPAdapter):
    pass
