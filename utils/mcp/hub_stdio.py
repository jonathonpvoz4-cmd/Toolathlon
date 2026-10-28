"""Preserve MCP wire messages while avoiding quadratic buffering of large lines.

Installed only by the Hub adapter; upstream MCP packages and tool results are
unchanged. The pinned MCP 1.9 reader otherwise scans/copies the entire unfinished
JSON line for every small pipe chunk.
"""


def install_linear_stdio_reader():
    import mcp.client.stdio as stdio

    original = stdio.TextReceiveStream
    if getattr(original, '_hub_linear', False):
        return

    async def lines(*args, **kwargs):
        pieces = []
        async for chunk in original(*args, **kwargs):
            if '\n' not in chunk:
                pieces.append(chunk)
                continue
            head, tail = chunk.rsplit('\n', 1)
            pieces.append(head)
            yield ''.join(pieces) + '\n'
            pieces = [tail] if tail else []
        if pieces:
            yield ''.join(pieces)

    lines._hub_linear = True
    stdio.TextReceiveStream = lines
