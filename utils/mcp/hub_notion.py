"""Notion SDK adapter for preparation/grading; OAuth login remains unchanged."""
import httpx
import os
from notion_client import Client as DirectClient

from utils.mcp.workspace_http import hub_enabled, notion_endpoint


class NotionTransport(httpx.BaseTransport):
    def __init__(self, transport=None):
        self.transport = transport or httpx.HTTPTransport()

    def handle_request(self, request):
        client, url = notion_endpoint(request.url, request.headers)
        headers = {name: value for name, value in request.headers.items()
                   if name.lower() in {'content-type', 'accept', 'notion-version'}}
        headers.update({'Authorization': 'Bearer ' + client.key, 'X-MCPHub-Run-Token': client.token})
        if os.getenv('MCPHUB_TASK_ID'):
            headers['X-MCPHub-Task-Id'] = os.environ['MCPHUB_TASK_ID']
        mapped = httpx.Request(request.method, url, headers=headers, content=request.read(),
                               extensions=request.extensions)
        response = self.transport.handle_request(mapped)
        response.request = request
        return response

    def close(self):
        self.transport.close()


class Client(DirectClient):
    def __init__(self, options=None, client=None, **kwargs):
        self._hub_routed = hub_enabled()
        if self._hub_routed:
            if client is not None:
                raise ValueError('Use the Hub Notion transport for account-scoped SDK requests')
            client = httpx.Client(transport=NotionTransport())
        super().__init__(options=options, client=client, **kwargs)

    def __enter__(self):
        if self._hub_routed:
            # notion-client's normal __enter__ replaces its HTTP client, losing custom transports.
            self.client.__enter__()
            return self
        return super().__enter__()
